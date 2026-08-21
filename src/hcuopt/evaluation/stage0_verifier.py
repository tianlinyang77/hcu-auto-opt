from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import stat
from pathlib import Path, PurePosixPath
from statistics import fmean
from typing import Any, Literal
from urllib.parse import unquote, urlparse
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import (
    SHA256_PATTERN,
    AdapterProvenance,
    ArtifactManifest,
    ExecutionRequest,
    ExecutionResult,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    LeaseScope,
    ProfilerCapability,
    Stage0ProbeType,
    Stage0RunMode,
)
from hcuopt.domain.models import Stage0Evidence
from hcuopt.evaluation.stage0_protocol import (
    MEASUREMENT_EVIDENCE_SCHEMA_VERSION,
    LoadedStage0Protocol,
    Stage0ProtocolError,
    load_registered_stage0_protocol,
)
from hcuopt.evaluation.stage0_statistics import (
    ClockCalibrationStatistics,
    NoiseStatistics,
    SignalStatistics,
    Stage0StatisticsError,
    compute_abba_effect,
    compute_noise_statistics,
    known_signal_passes,
    normalize_device_samples,
    null_signal_passes,
    recompute_clock_calibration,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.models import (
    DynamicObservationV2,
    MeasurementEvidenceV2,
    ProcessLifecycleRecordV2,
    RawEvidenceFileV2,
    Stage0AdapterProvenance,
    Stage0EvidenceBinding,
    StrictMeasurementModel,
)

MAX_RAW_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_RAW_JSON_DEPTH = 64
_WINDOWS_DRIVE_PATH = re.compile(r"^/[A-Za-z]:/")


class Stage0EvidenceError(ValueError):
    """Raw evidence is corrupt, unsafe, or not bound to the requested run."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _VerifierModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Stage0VerificationContext(_VerifierModel):
    task_id: UUID
    stage0_run_id: UUID
    target_snapshot_id: UUID
    target: TargetSpec
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    workload_id: str = Field(min_length=1, max_length=200)
    adapter_profile: str = Field(min_length=1, max_length=200)
    expected_resource_id: str = Field(min_length=1, max_length=200)
    mode: Literal[Stage0RunMode.FORMAL] = Stage0RunMode.FORMAL

    @model_validator(mode="after")
    def verify_target_fingerprint(self) -> Stage0VerificationContext:
        if self.target_fingerprint != target_fingerprint(self.target):
            raise ValueError("target_fingerprint does not match TargetSpec")
        return self


class Stage0ProbeEvidenceReference(_VerifierModel):
    probe_record_id: UUID
    probe_type: Stage0ProbeType
    raw_evidence_uri: str = Field(min_length=1, max_length=4000)
    raw_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    adapter_provenance: tuple[AdapterProvenance, ...] = Field(min_length=1)
    synthetic: Literal[False] = False
    lease_id: UUID
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    cleanup_evidence: dict[str, Any]

    @field_validator("adapter_provenance", mode="before")
    @classmethod
    def freeze_provenance(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def require_formal_origin(self) -> Stage0ProbeEvidenceReference:
        if any(item.implementation_kind != "real" for item in self.adapter_provenance):
            raise ValueError("formal evidence references require real provenance")
        if not cleanup_is_healthy(self.cleanup_evidence):
            raise ValueError("formal evidence references require healthy fenced cleanup")
        fence = self.cleanup_evidence["fence"]
        health = self.cleanup_evidence["health"]
        if (
            fence.get("resource_id") != self.resource_id
            or fence.get("fencing_token") != self.fencing_token
            or health.get("resource_id") != self.resource_id
        ):
            raise ValueError("formal cleanup evidence does not match resource and fencing token")
        return self


class _BoundRawEvidence(StrictMeasurementModel):
    schema_version: Literal["measurement-evidence-v2"] = MEASUREMENT_EVIDENCE_SCHEMA_VERSION
    binding: Stage0EvidenceBinding
    observations: tuple[DynamicObservationV2, ...] = Field(min_length=2)
    adapter_provenance: tuple[Stage0AdapterProvenance, ...] = Field(min_length=1)
    synthetic: Literal[False] = False

    @field_validator("observations", "adapter_provenance", mode="before")
    @classmethod
    def freeze_provenance(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def require_formal_observations(self) -> _BoundRawEvidence:
        phases = [observation.phase for observation in self.observations]
        if phases != ["before_run", "after_run"]:
            raise ValueError(
                "formal capability evidence requires exactly one before_run and after_run"
            )
        if self.observations[1].captured_monotonic_ns <= self.observations[0].captured_monotonic_ns:
            raise ValueError("formal capability telemetry must be strictly time ordered")
        return self


class FingerprintEvidenceV2(_BoundRawEvidence):
    hardware_fingerprint: str = Field(pattern=SHA256_PATTERN)
    software_fingerprint: str = Field(pattern=SHA256_PATTERN)


class ProfilerKernelRecordV2(StrictMeasurementModel):
    kernel_name: str = Field(min_length=1, max_length=1000)
    duration_ns: float = Field(gt=0)
    call_count: int = Field(ge=1)
    shapes: tuple[str, ...] = ()
    dtypes: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    python_source: str | None = Field(default=None, min_length=1, max_length=2000)
    hip_symbol: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("shapes", "dtypes", mode="before")
    @classmethod
    def freeze_text_lists(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("kernel_name", "python_source", "hip_symbol")
    @classmethod
    def reject_blank_or_control_text(cls, value: str | None) -> str | None:
        if value is not None and not _is_auditable_text(value):
            raise ValueError("profiler text must be trimmed and contain no control characters")
        return value

    @field_validator("shapes", "dtypes")
    @classmethod
    def reject_blank_shape_or_dtype(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _is_auditable_text(item) for item in value):
            raise ValueError("shape and dtype entries must be auditable text")
        return value


class ProfilerEvidenceV2(_BoundRawEvidence):
    tool_name: Literal["rocprof", "profile-llm-torch"]
    parser_version: Literal["rocprof-csv-v1", "torch-trace-v1"]
    tool_version_output: RawEvidenceFileV2
    raw_output: RawEvidenceFileV2

    @model_validator(mode="after")
    def bind_tool_to_parser(self) -> ProfilerEvidenceV2:
        expected = {
            "rocprof": "rocprof-csv-v1",
            "profile-llm-torch": "torch-trace-v1",
        }
        if expected[self.tool_name] != self.parser_version:
            raise ValueError("profiler tool and parser version do not match")
        return self


class OverlayMountEvidenceV2(StrictMeasurementModel):
    source_uri: str = Field(min_length=1, max_length=4000)
    source_hash: str = Field(pattern=SHA256_PATTERN)
    target_path: str = Field(pattern=r"^/[^\x00]*$")
    read_only: Literal[True]
    container_id: str = Field(min_length=1, max_length=200)

    @field_validator("target_path")
    @classmethod
    def require_safe_mount_target(cls, value: str) -> str:
        parsed = PurePosixPath(value)
        if parsed == PurePosixPath("/") or ".." in parsed.parts or parsed.as_posix() != value:
            raise ValueError("overlay target must be a normalized non-root absolute path")
        return value


class HotpatchEvidenceV2(_BoundRawEvidence):
    activation_mode: Literal["runtime_hot_patch", "startup_overlay", "none"]
    baseline_source: RawEvidenceFileV2 | None = None
    candidate_source: RawEvidenceFileV2 | None = None
    artifact_manifest: RawEvidenceFileV2 | None = None
    artifact: RawEvidenceFileV2 | None = None
    baseline_state: RawEvidenceFileV2 | None = None
    candidate_state: RawEvidenceFileV2 | None = None
    recovery_state: RawEvidenceFileV2 | None = None
    overlay_mount: OverlayMountEvidenceV2 | None = None

    @model_validator(mode="after")
    def bind_overlay_capability(self) -> HotpatchEvidenceV2:
        activation_evidence = (
            self.baseline_source,
            self.candidate_source,
            self.artifact_manifest,
            self.artifact,
            self.baseline_state,
            self.candidate_state,
            self.recovery_state,
        )
        if self.activation_mode == "none" and any(
            value is not None for value in activation_evidence
        ):
            raise ValueError("activation_mode=none cannot claim activation evidence")
        if self.activation_mode != "none" and any(value is None for value in activation_evidence):
            raise ValueError("an activated hotpatch requires complete raw evidence")
        if self.activation_mode == "startup_overlay" and self.overlay_mount is None:
            raise ValueError("startup_overlay requires a verified read-only mount")
        if self.activation_mode != "startup_overlay" and self.overlay_mount is not None:
            raise ValueError("overlay mount evidence is only valid for startup_overlay")
        return self


class HotpatchStateEntryV2(StrictMeasurementModel):
    path: str = Field(min_length=1, max_length=2000)
    content: RawEvidenceFileV2

    @field_validator("path")
    @classmethod
    def require_normalized_relative_path(cls, value: str) -> str:
        parsed = PurePosixPath(value)
        if parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != value:
            raise ValueError("hotpatch state paths must be normalized relative paths")
        return value


class HotpatchPhaseManifestV2(StrictMeasurementModel):
    schema_version: Literal["hotpatch-phase-v2"] = "hotpatch-phase-v2"
    phase: Literal["baseline", "candidate", "recovery"]
    source_snapshot_id: UUID
    execution_request: ExecutionRequest
    execution_result: ExecutionResult
    process_id: int = Field(ge=1)
    process_start_token: str = Field(min_length=1, max_length=200)
    process_start_record: RawEvidenceFileV2
    process_exit_record: RawEvidenceFileV2
    container_id: str = Field(min_length=1, max_length=200)
    entries: tuple[HotpatchStateEntryV2, ...] = Field(min_length=1)
    output: RawEvidenceFileV2
    normalized_output: RawEvidenceFileV2
    cache_namespace: RawEvidenceFileV2

    @field_validator("entries", mode="before")
    @classmethod
    def freeze_entries(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def bind_execution(self) -> HotpatchPhaseManifestV2:
        if self.execution_result.request_id != self.execution_request.request_id:
            raise ValueError("hotpatch phase result does not match its execution request")
        if (
            self.execution_result.status != "succeeded"
            or self.execution_result.exit_code != 0
            or self.execution_result.synthetic
            or self.execution_result.adapter_provenance.implementation_kind != "real"
        ):
            raise ValueError("hotpatch phase requires a successful real execution")
        if self.execution_result.metadata.get("container_name") != self.container_id:
            raise ValueError("hotpatch container does not match execution metadata")
        if self.execution_result.stdout_uri != self.output.uri:
            raise ValueError("hotpatch output must be the execution stdout")
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("hotpatch phase state paths must be unique")
        return self


class HotpatchExecutionObservationV2(StrictMeasurementModel):
    """Versioned stdout emitted by the process that loaded the implementation."""

    protocol_version: Literal["hcuopt-overlay-result-v2"]
    activation_marker: str = Field(min_length=1, max_length=200)
    output_hash: str = Field(pattern=SHA256_PATTERN)
    workload_kind: Literal["sglang_python_triton"]
    replacement_point: str = Field(pattern=r"^/[^\x00]*$")
    implementation_hash: str = Field(pattern=SHA256_PATTERN)
    process_id: int = Field(ge=1)
    loaded_artifact_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @field_validator("replacement_point")
    @classmethod
    def require_safe_replacement_point(cls, value: str) -> str:
        parsed = PurePosixPath(value)
        if parsed == PurePosixPath("/") or ".." in parsed.parts or parsed.as_posix() != value:
            raise ValueError("replacement point must be a normalized non-root absolute path")
        return value


class Stage0VerificationResult(_VerifierModel):
    protocol_version: str
    protocol_hash: str = Field(pattern=SHA256_PATTERN)
    input_digest: str = Field(pattern=SHA256_PATTERN)
    measurement: GateResult
    profiler: ProfilerCapability
    hot_patch: HotPatchCapability
    hardware_fingerprint: str = Field(pattern=SHA256_PATTERN)
    software_fingerprint: str = Field(pattern=SHA256_PATTERN)
    timer_resolution_ns: float = Field(gt=0)
    noise_sigma_ns: float = Field(ge=0)
    noise_cv: float = Field(ge=0)
    mde_ratio: float = Field(ge=0)
    failure_codes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    statistics: dict[str, Any]
    input_evidence: tuple[dict[str, Any], ...]
    verifier_provenance: AdapterProvenance

    @field_validator("failure_codes", "reasons", "input_evidence", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    def to_stage0_evidence(self, *, evidence_uri: str) -> Stage0Evidence:
        return Stage0Evidence(
            measurement=self.measurement,
            profiler=self.profiler,
            hot_patch=self.hot_patch,
            hardware_fingerprint=self.hardware_fingerprint,
            software_fingerprint=self.software_fingerprint,
            timer_resolution_ns=self.timer_resolution_ns,
            noise_sigma_ns=self.noise_sigma_ns,
            noise_cv=self.noise_cv,
            mde_ratio=self.mde_ratio,
            evidence_uri=evidence_uri,
        )


class Stage0EvidenceReader:
    """Read canonical, hashed evidence without following links outside an allowed root."""

    def __init__(self, allowed_root: Path, *, max_bytes: int = MAX_RAW_EVIDENCE_BYTES) -> None:
        self.allowed_root = allowed_root.resolve(strict=True)
        if not self.allowed_root.is_dir():
            raise ValueError("allowed evidence root must be a directory")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.max_bytes = max_bytes

    def read(self, uri: str, expected_hash: str) -> dict[str, Any]:
        _, value = self._read_document(uri, expected_hash)
        return value

    def read_bytes(self, uri: str, expected_hash: str) -> bytes:
        """Return verified canonical JSON bytes for strict Pydantic JSON validation."""

        encoded, _ = self._read_document(uri, expected_hash)
        return encoded

    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes:
        """Return bounded, securely opened bytes after verifying their SHA-256."""

        path = self._resolve_file_uri(uri)
        encoded = self._secure_read(path)
        actual_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if actual_hash != expected_hash:
            raise Stage0EvidenceError("evidence_hash_mismatch", f"SHA-256 mismatch: {path}")
        return encoded

    def _read_document(self, uri: str, expected_hash: str) -> tuple[bytes, dict[str, Any]]:
        path = self._resolve_file_uri(uri)
        encoded = self._secure_read(path)
        actual_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if actual_hash != expected_hash:
            raise Stage0EvidenceError("evidence_hash_mismatch", f"SHA-256 mismatch: {path}")
        try:
            text = encoded.decode("utf-8", errors="strict")
            value = json.loads(text, parse_constant=self._reject_json_constant)
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise Stage0EvidenceError("evidence_invalid_json", f"invalid JSON: {path}") from exc
        if not isinstance(value, dict):
            raise Stage0EvidenceError(
                "evidence_invalid_json", "raw evidence root must be an object"
            )
        _validate_json_depth(value, maximum_depth=MAX_RAW_JSON_DEPTH)
        try:
            canonical = canonical_json_bytes(value)
        except (TypeError, ValueError, RecursionError) as exc:
            raise Stage0EvidenceError(
                "evidence_noncanonical", "raw evidence is not canonical"
            ) from exc
        if encoded != canonical:
            raise Stage0EvidenceError(
                "evidence_noncanonical",
                "raw evidence bytes do not match canonical JSON representation",
            )
        return encoded, value

    def _resolve_file_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise Stage0EvidenceError(
                "evidence_uri_invalid", "only local file: evidence URIs are allowed"
            )
        raw_path = unquote(parsed.path)
        if os.name == "nt" and _WINDOWS_DRIVE_PATH.match(raw_path):
            raw_path = raw_path[1:]
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            raise Stage0EvidenceError("evidence_uri_invalid", "evidence URI path must be absolute")
        if ".." in candidate.parts:
            raise Stage0EvidenceError("evidence_path_escape", "evidence path contains traversal")
        try:
            candidate.relative_to(self.allowed_root)
            self._reject_symlinks(candidate)
            candidate.resolve(strict=True).relative_to(self.allowed_root)
        except Stage0EvidenceError:
            raise
        except (OSError, ValueError) as exc:
            raise Stage0EvidenceError(
                "evidence_path_escape", "evidence path escapes allowed root"
            ) from exc
        return candidate

    def _reject_symlinks(self, candidate: Path) -> None:
        relative = candidate.relative_to(self.allowed_root)
        cursor = self.allowed_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise Stage0EvidenceError(
                    "evidence_symlink", f"symbolic link is not allowed: {cursor}"
                )

    def _secure_read(self, path: Path) -> bytes:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise Stage0EvidenceError(
                "evidence_platform_unsupported",
                "Formal evidence requires POSIX openat/O_NOFOLLOW path semantics",
            )
        return self._secure_read_posix(path)

    def _secure_read_posix(self, path: Path) -> bytes:
        relative = path.relative_to(self.allowed_root)
        if not relative.parts:
            raise Stage0EvidenceError("evidence_not_regular", f"not a regular file: {path}")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_descriptors: list[int] = []
        file_descriptor: int | None = None
        try:
            current = os.open(self.allowed_root, directory_flags)
            directory_descriptors.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, directory_flags, dir_fd=current)
                directory_descriptors.append(current)
            file_descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=current,
            )
            encoded = self._read_bounded_descriptor(file_descriptor, path)
            opened = os.fstat(file_descriptor)
            current_path = os.stat(relative.parts[-1], dir_fd=current, follow_symlinks=False)
            if _is_reparse_point(current_path) or not _same_file_identity(opened, current_path):
                raise Stage0EvidenceError(
                    "evidence_changed_during_read",
                    "evidence path identity changed while it was being read",
                )
            return encoded
        except OSError as exc:
            raise Stage0EvidenceError(
                "evidence_unreadable", f"cannot securely open evidence: {path}"
            ) from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            for descriptor in reversed(directory_descriptors):
                os.close(descriptor)

    def _read_bounded_descriptor(self, descriptor: int, path: Path) -> bytes:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise Stage0EvidenceError("evidence_not_regular", f"not a regular file: {path}")
        if opened.st_size > self.max_bytes:
            raise Stage0EvidenceError("evidence_too_large", f"evidence exceeds size limit: {path}")
        chunks: list[bytes] = []
        remaining = self.max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > self.max_bytes:
            raise Stage0EvidenceError("evidence_too_large", f"evidence exceeds size limit: {path}")
        finished = os.fstat(descriptor)
        if not _same_file_identity(opened, finished) or finished.st_size != len(encoded):
            raise Stage0EvidenceError(
                "evidence_changed_during_read", "evidence changed while it was being read"
            )
        return encoded

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")


class Stage0Verifier:
    """D-owned verifier that derives all Stage 0 gates from raw evidence bytes."""

    provenance = AdapterProvenance(
        profile="stage0-d-verifier",
        capability="stage0_independent_verification",
        adapter_name="Stage0Verifier",
        adapter_version="1",
        implementation_kind="real",
    )

    def __init__(self, protocol: LoadedStage0Protocol, reader: Stage0EvidenceReader) -> None:
        try:
            registered = load_registered_stage0_protocol(protocol.protocol.protocol_version)
        except Stage0ProtocolError as exc:
            raise Stage0EvidenceError("protocol_not_registered", str(exc)) from exc
        if (
            protocol.protocol_hash != registered.protocol_hash
            or protocol.canonical_bytes != registered.canonical_bytes
        ):
            raise Stage0EvidenceError(
                "protocol_not_registered",
                "formal verification requires the repository-registered protocol content",
            )
        self.loaded_protocol = protocol
        self.protocol = protocol.protocol
        self.reader = reader

    def verify(
        self,
        context: Stage0VerificationContext,
        references: tuple[Stage0ProbeEvidenceReference, ...] | list[Stage0ProbeEvidenceReference],
    ) -> Stage0VerificationResult:
        context = _snapshot_context(context)
        by_type = self._index_references(references)
        reference_resources = {reference.resource_id for reference in by_type.values()}
        if reference_resources != {context.expected_resource_id}:
            raise Stage0EvidenceError(
                "resource_binding_mismatch",
                "all Stage 0 probes must use the run's expected accelerator resource",
            )
        raw = {
            probe_type: self.reader.read_bytes(
                reference.raw_evidence_uri, reference.raw_evidence_hash
            )
            for probe_type, reference in by_type.items()
        }

        try:
            fingerprint = FingerprintEvidenceV2.model_validate_json(
                raw[Stage0ProbeType.FINGERPRINT]
            )
            timer = MeasurementEvidenceV2.model_validate_json(raw[Stage0ProbeType.TIMER])
            noise = MeasurementEvidenceV2.model_validate_json(raw[Stage0ProbeType.NOISE])
            known = MeasurementEvidenceV2.model_validate_json(raw[Stage0ProbeType.KNOWN_SIGNAL])
            null = MeasurementEvidenceV2.model_validate_json(raw[Stage0ProbeType.NULL_SIGNAL])
            profiler = ProfilerEvidenceV2.model_validate_json(raw[Stage0ProbeType.PROFILER])
            hotpatch = HotpatchEvidenceV2.model_validate_json(raw[Stage0ProbeType.HOTPATCH])
        except ValidationError as exc:
            raise Stage0EvidenceError("evidence_schema_invalid", str(exc)) from exc

        parsed: dict[Stage0ProbeType, _BoundRawEvidence | MeasurementEvidenceV2] = {
            Stage0ProbeType.FINGERPRINT: fingerprint,
            Stage0ProbeType.TIMER: timer,
            Stage0ProbeType.NOISE: noise,
            Stage0ProbeType.KNOWN_SIGNAL: known,
            Stage0ProbeType.NULL_SIGNAL: null,
            Stage0ProbeType.PROFILER: profiler,
            Stage0ProbeType.HOTPATCH: hotpatch,
        }
        expected_environment_fingerprint = stable_fingerprint(
            context.target.model_dump(mode="json")
        )
        measurement_ids: set[UUID] = set()
        for probe_type, evidence in parsed.items():
            self._verify_binding(context, by_type[probe_type], evidence)
            if evidence.binding.environment_fingerprint != expected_environment_fingerprint:
                raise Stage0EvidenceError(
                    "environment_fingerprint_mismatch",
                    f"{probe_type.value} environment fingerprint does not match TargetSpec",
                )
            measurement_ids.add(evidence.binding.measurement_id)
        if len(measurement_ids) != len(Stage0ProbeType):
            raise Stage0EvidenceError(
                "measurement_id_reused",
                "each probe must have a distinct measurement ID",
            )
        timing_metric_units = {
            (evidence.binding.metric_name, evidence.binding.unit)
            for evidence in (timer, noise, known, null)
        }
        expected_timing_metric = {(self.protocol.timing_metric_name, self.protocol.timing_unit)}
        if timing_metric_units != expected_timing_metric:
            raise Stage0EvidenceError(
                "metric_binding_mismatch",
                "timer, noise, known and null evidence must bind the registered metric and unit",
            )

        self._verify_measurement_plan(Stage0ProbeType.TIMER, timer)
        self._verify_measurement_plan(Stage0ProbeType.NOISE, noise)
        self._verify_measurement_plan(Stage0ProbeType.KNOWN_SIGNAL, known)
        self._verify_measurement_plan(Stage0ProbeType.NULL_SIGNAL, null)
        lifecycle_evidence: list[tuple[str, RawEvidenceFileV2]] = []
        for label, evidence in (
            ("timer", timer),
            ("noise", noise),
            ("known_signal", known),
            ("null_signal", null),
        ):
            lifecycle_evidence.extend(_verify_measurement_lifecycle(label, evidence, self.reader))

        try:
            calibrations = {
                label: recompute_clock_calibration(
                    [point.model_dump(mode="python") for point in evidence.calibration.points],
                    resolution_tick_deltas=evidence.calibration.resolution_tick_deltas,
                )
                for label, evidence in (
                    ("timer", timer),
                    ("noise", noise),
                    ("known_signal", known),
                    ("null_signal", null),
                )
            }
            for label, evidence in (
                ("timer", timer),
                ("noise", noise),
                ("known_signal", known),
                ("null_signal", null),
            ):
                _verify_claimed_calibration(label, evidence, calibrations[label])
            timer_samples = normalize_device_samples(
                [item.model_dump(mode="python") for item in timer.samples],
                ns_per_tick=calibrations["timer"].ns_per_tick,
            )
            noise_samples = normalize_device_samples(
                [item.model_dump(mode="python") for item in noise.samples],
                ns_per_tick=calibrations["noise"].ns_per_tick,
            )
            known_samples = normalize_device_samples(
                [item.model_dump(mode="python") for item in known.samples],
                ns_per_tick=calibrations["known_signal"].ns_per_tick,
            )
            null_samples = normalize_device_samples(
                [item.model_dump(mode="python") for item in null.samples],
                ns_per_tick=calibrations["null_signal"].ns_per_tick,
            )
            noise_result = compute_noise_statistics(
                noise_samples,
                evidence_hash=by_type[Stage0ProbeType.NOISE].raw_evidence_hash,
                alpha=self.protocol.statistics.alpha,
                power=self.protocol.statistics.power,
                bootstrap_iterations=self.protocol.statistics.bootstrap_resamples,
                hampel_threshold=self.protocol.outliers.hampel_sigma,
            )
            known_result = compute_abba_effect(
                known_samples,
                evidence_hash=by_type[Stage0ProbeType.KNOWN_SIGNAL].raw_evidence_hash,
                alpha=self.protocol.statistics.corrected_signal_alpha,
                bootstrap_iterations=self.protocol.statistics.bootstrap_resamples,
                hampel_threshold=self.protocol.outliers.hampel_sigma,
            )
            null_result = compute_abba_effect(
                null_samples,
                evidence_hash=by_type[Stage0ProbeType.NULL_SIGNAL].raw_evidence_hash,
                alpha=self.protocol.statistics.corrected_signal_alpha,
                bootstrap_iterations=self.protocol.statistics.bootstrap_resamples,
                hampel_threshold=self.protocol.outliers.hampel_sigma,
            )
        except Stage0StatisticsError as exc:
            raise Stage0EvidenceError("statistics_input_invalid", str(exc)) from exc

        failures: list[tuple[str, str]] = []
        gates = self.protocol.measurement_gates
        if noise_result.cv > gates.max_cv_ratio:
            failures.append(("noise_cv_exceeded", "noise CV exceeds the registered limit"))
        if noise_result.mde_fraction > gates.max_mde_ratio:
            failures.append(("noise_mde_exceeded", "noise MDE exceeds the registered limit"))
        if not known_signal_passes(
            known_result,
            minimum_effect=gates.known_signal_min_effect_ratio,
            confidence_floor=gates.known_signal_ci_floor_ratio,
        ):
            failures.append(
                ("known_signal_not_detected", "known slowdown was not reliably detected")
            )
        if not null_signal_passes(
            null_result,
            equivalence_margin=gates.null_equivalence_margin_ratio,
        ):
            failures.append(
                (
                    "null_signal_not_equivalent",
                    "null signal did not remain inside equivalence bounds",
                )
            )

        for label, result in (
            ("noise", noise_result),
            ("known_signal", known_result),
            ("null_signal", null_result),
        ):
            if result.outlier_fraction > self.protocol.outliers.max_flagged_ratio:
                failures.append(
                    (f"{label}_outlier_ratio_exceeded", f"{label} has too many flagged samples")
                )

        timer_gate = self.protocol.timer_gates
        for label, normalized_samples in (
            ("timer", timer_samples),
            ("noise", noise_samples),
            ("known_signal", known_samples),
            ("null_signal", null_samples),
        ):
            mean_ns = fmean(item.sample_ns for item in normalized_samples)
            if (
                calibrations[label].timer_resolution_ns / mean_ns
                > timer_gate.max_resolution_to_mean_ratio
            ):
                failures.append(
                    (
                        f"{label}_timer_resolution_exceeded",
                        f"{label} timer resolution is too coarse",
                    )
                )
            if (
                calibrations[label].max_residual_ns / mean_ns
                > timer_gate.max_residual_to_mean_ratio
            ):
                failures.append(
                    (
                        f"{label}_calibration_residual_exceeded",
                        f"{label} clock calibration residual is too large",
                    )
                )

        for evidence in parsed.values():
            failures.extend(self._environment_failures(context.target, evidence))
        for phase in ("before_run", "after_run"):
            cache_signatures = {
                (
                    observation.telemetry.cache.state,
                    observation.telemetry.cache.cleared_before_sample,
                    observation.telemetry.cache.identity_hash,
                )
                for evidence in parsed.values()
                for observation in evidence.observations
                if observation.phase == phase
            }
            if len(cache_signatures) != 1:
                failures.append(
                    (
                        "cache_policy_mismatch",
                        f"{phase} cache evidence differs between Stage 0 probes",
                    )
                )

        profiler_capability = classify_profiler(profiler, self.reader)
        hotpatch_capability = classify_hotpatch(hotpatch, self.reader, context.target)
        unique_failures = _deduplicate_failures(failures)
        input_evidence_items = [
            {
                "probe_record_id": str(by_type[probe_type].probe_record_id),
                "probe_type": probe_type.value,
                "kind": "probe_envelope",
                "uri": by_type[probe_type].raw_evidence_uri,
                "sha256": by_type[probe_type].raw_evidence_hash,
            }
            for probe_type in sorted(Stage0ProbeType, key=lambda item: item.value)
        ]
        input_evidence_items.extend(
            {
                "probe_type": label,
                "kind": "process_lifecycle_record",
                "uri": reference.uri,
                "sha256": reference.sha256,
            }
            for label, reference in lifecycle_evidence
        )
        input_evidence_items.append(
            {
                "probe_type": Stage0ProbeType.PROFILER.value,
                "kind": "profiler_raw_output",
                "uri": profiler.raw_output.uri,
                "sha256": profiler.raw_output.sha256,
                "tool_name": profiler.tool_name,
                "parser_version": profiler.parser_version,
            }
        )
        input_evidence_items.append(
            {
                "probe_type": Stage0ProbeType.PROFILER.value,
                "kind": "profiler_tool_version",
                "uri": profiler.tool_version_output.uri,
                "sha256": profiler.tool_version_output.sha256,
            }
        )
        if hotpatch.activation_mode != "none":
            for field_name in (
                "baseline_source",
                "candidate_source",
                "artifact_manifest",
                "artifact",
                "baseline_state",
                "candidate_state",
                "recovery_state",
            ):
                reference = getattr(hotpatch, field_name)
                assert reference is not None
                input_evidence_items.append(
                    {
                        "probe_type": Stage0ProbeType.HOTPATCH.value,
                        "kind": field_name,
                        "uri": reference.uri,
                        "sha256": reference.sha256,
                    }
                )
            for state_name in ("baseline_state", "candidate_state", "recovery_state"):
                state_reference = getattr(hotpatch, state_name)
                assert state_reference is not None
                phase = _read_hotpatch_phase(self.reader, state_reference)
                for kind, reference in (
                    ("process_start_record", phase.process_start_record),
                    ("process_exit_record", phase.process_exit_record),
                    ("output", phase.output),
                    ("cache_namespace", phase.cache_namespace),
                ):
                    input_evidence_items.append(
                        {
                            "probe_type": Stage0ProbeType.HOTPATCH.value,
                            "phase": phase.phase,
                            "kind": kind,
                            "uri": reference.uri,
                            "sha256": reference.sha256,
                        }
                    )
                for entry in phase.entries:
                    input_evidence_items.append(
                        {
                            "probe_type": Stage0ProbeType.HOTPATCH.value,
                            "phase": phase.phase,
                            "kind": "state_entry",
                            "path": entry.path,
                            "uri": entry.content.uri,
                            "sha256": entry.content.sha256,
                        }
                    )
        input_evidence = tuple(input_evidence_items)
        return Stage0VerificationResult(
            protocol_version=self.protocol.protocol_version,
            protocol_hash=self.loaded_protocol.protocol_hash,
            input_digest=verification_input_digest(context, by_type, self.loaded_protocol),
            measurement=GateResult.FAIL if unique_failures else GateResult.PASS,
            profiler=profiler_capability,
            hot_patch=hotpatch_capability,
            hardware_fingerprint=fingerprint.hardware_fingerprint,
            software_fingerprint=fingerprint.software_fingerprint,
            timer_resolution_ns=calibrations["timer"].timer_resolution_ns,
            noise_sigma_ns=noise_result.sigma_ns,
            noise_cv=noise_result.cv,
            mde_ratio=noise_result.mde_fraction,
            failure_codes=tuple(code for code, _ in unique_failures),
            reasons=tuple(message for _, message in unique_failures),
            statistics={
                "noise": _noise_statistics_dict(noise_result),
                "known_signal": _signal_statistics_dict(known_result),
                "null_signal": _signal_statistics_dict(null_result),
                "clock_calibration": {
                    label: {
                        "timer_resolution_ns": value.timer_resolution_ns,
                        "ns_per_tick": value.ns_per_tick,
                        "max_residual_ns": value.max_residual_ns,
                        "point_count": value.point_count,
                    }
                    for label, value in calibrations.items()
                },
                "environment_fingerprint": expected_environment_fingerprint,
            },
            input_evidence=input_evidence,
            verifier_provenance=self.provenance,
        )

    @staticmethod
    def _index_references(
        references: tuple[Stage0ProbeEvidenceReference, ...] | list[Stage0ProbeEvidenceReference],
    ) -> dict[Stage0ProbeType, Stage0ProbeEvidenceReference]:
        by_type: dict[Stage0ProbeType, Stage0ProbeEvidenceReference] = {}
        for raw_reference in references:
            try:
                reference = Stage0ProbeEvidenceReference.model_validate(
                    raw_reference.model_dump(mode="python", round_trip=True)
                )
            except (AttributeError, TypeError, ValidationError) as exc:
                raise Stage0EvidenceError(
                    "reference_schema_invalid",
                    f"Stage 0 probe reference is invalid: {exc}",
                ) from exc
            if reference.probe_type in by_type:
                raise Stage0EvidenceError(
                    "duplicate_probe", f"duplicate {reference.probe_type.value} evidence"
                )
            by_type[reference.probe_type] = reference
        missing = set(Stage0ProbeType) - set(by_type)
        extra = set(by_type) - set(Stage0ProbeType)
        if missing or extra:
            names = ",".join(sorted(item.value for item in missing | extra))
            raise Stage0EvidenceError(
                "probe_barrier_incomplete", f"seven-probe barrier mismatch: {names}"
            )
        return by_type

    def _verify_binding(
        self,
        context: Stage0VerificationContext,
        reference: Stage0ProbeEvidenceReference,
        evidence: _BoundRawEvidence | MeasurementEvidenceV2,
    ) -> None:
        binding = evidence.binding
        expected = {
            "task_id": context.task_id,
            "stage0_run_id": context.stage0_run_id,
            "target_snapshot_id": context.target_snapshot_id,
            "target_id": context.target.target_id,
            "target_fingerprint": context.target_fingerprint,
            "workload_id": context.workload_id,
            "probe_type": reference.probe_type,
            "run_mode": Stage0RunMode.FORMAL,
            "protocol_version": self.protocol.protocol_version,
            "protocol_hash": self.loaded_protocol.protocol_hash,
            "lease_id": reference.lease_id,
            "lease_scope": LeaseScope.EXCLUSIVE,
            "resource_id": reference.resource_id,
            "fencing_token": reference.fencing_token,
        }
        actual = {
            "task_id": binding.task_id,
            "stage0_run_id": binding.stage0_run_id,
            "target_snapshot_id": binding.target_snapshot_id,
            "target_id": binding.target_id,
            "target_fingerprint": binding.target_fingerprint,
            "workload_id": binding.workload_id,
            "probe_type": binding.probe_type,
            "run_mode": binding.run_mode,
            "protocol_version": binding.protocol_version,
            "protocol_hash": binding.protocol_hash,
            "lease_id": binding.lease.lease_id,
            "lease_scope": binding.lease.lease_scope,
            "resource_id": binding.lease.resource_id,
            "fencing_token": binding.lease.fencing_token,
        }
        mismatch = [name for name in expected if expected[name] != actual[name]]
        if mismatch:
            raise Stage0EvidenceError(
                "evidence_binding_mismatch",
                f"{reference.probe_type.value} evidence binding mismatch: {', '.join(mismatch)}",
            )
        expected_device_index = context.target.execution_host.accelerator.device_index
        observed_device_indexes = {
            observation.telemetry.device.device_index for observation in evidence.observations
        }
        if isinstance(evidence, MeasurementEvidenceV2):
            observed_device_indexes.add(evidence.calibration.device_index)
        if observed_device_indexes != {expected_device_index}:
            raise Stage0EvidenceError(
                "evidence_device_mismatch",
                f"{reference.probe_type.value} telemetry or timer came from the wrong device",
            )
        raw_provenance = [item.model_dump(mode="json") for item in evidence.adapter_provenance]
        record_provenance = [item.model_dump(mode="json") for item in reference.adapter_provenance]
        if raw_provenance != record_provenance:
            raise Stage0EvidenceError("provenance_mismatch", "raw and recorded provenance differ")
        if any(
            item.implementation_kind != "real" or item.profile != context.adapter_profile
            for item in evidence.adapter_provenance
        ):
            raise Stage0EvidenceError(
                "provenance_invalid",
                "formal raw evidence requires real provenance from the bound adapter profile",
            )

    def _verify_measurement_plan(
        self,
        probe_type: Stage0ProbeType,
        evidence: _BoundRawEvidence | MeasurementEvidenceV2,
    ) -> None:
        plan = evidence.plan
        sampling = self.protocol.sampling
        mismatches: list[str] = []
        if plan.restart_count != sampling.restart_count:
            mismatches.append("restart_count")
        if plan.warmup_count != sampling.warmup_count:
            mismatches.append("warmup_count")
        if plan.batch_iterations != sampling.batch_iterations:
            mismatches.append("batch_iterations")
        if probe_type is Stage0ProbeType.NOISE:
            if plan.segment_order != ("noise",):
                mismatches.append("segment_order")
            if plan.samples_per_segment != sampling.noise_samples_per_restart:
                mismatches.append("samples_per_segment")
        elif probe_type in {Stage0ProbeType.KNOWN_SIGNAL, Stage0ProbeType.NULL_SIGNAL}:
            if plan.segment_order != sampling.signal_segment_order:
                mismatches.append("segment_order")
            if plan.samples_per_segment != sampling.signal_samples_per_segment:
                mismatches.append("samples_per_segment")
        elif probe_type is Stage0ProbeType.TIMER and plan.segment_order != ("timer",):
            mismatches.append("segment_order")
        if mismatches:
            raise Stage0EvidenceError(
                "measurement_plan_mismatch",
                f"{probe_type.value} plan differs from registered protocol: "
                f"{', '.join(mismatches)}",
            )

    def _environment_failures(
        self,
        target: TargetSpec,
        evidence: MeasurementEvidenceV2,
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        gate = self.protocol.environment_gates
        accelerator = target.execution_host.accelerator
        temperatures: dict[str, float] = {}
        for observation in evidence.observations:
            telemetry = observation.telemetry
            temperature = max(
                telemetry.device.temperature_c,
                telemetry.device.hotspot_temperature_c
                if telemetry.device.hotspot_temperature_c is not None
                else telemetry.device.temperature_c,
            )
            if temperature > gate.max_temperature_c:
                result.append(
                    ("temperature_exceeded", "device temperature exceeds the registered limit")
                )
            if observation.phase in {"before_run", "after_run"}:
                temperatures[observation.phase] = temperature
            for name, actual, expected in (
                ("sclk", telemetry.device.sclk_mhz, accelerator.expected_sclk_mhz),
                ("mclk", telemetry.device.mclk_mhz, accelerator.expected_mclk_mhz),
            ):
                if abs(actual - expected) / expected > gate.clock_tolerance_ratio:
                    result.append(
                        (
                            f"{name}_drift_exceeded",
                            f"{name.upper()} differs from Target expectation",
                        )
                    )
            if telemetry.device.performance_level != gate.required_performance_level:
                result.append(("performance_level_mismatch", "performance level is not manual"))
            if telemetry.cache.state == "unknown":
                result.append(("cache_state_unknown", "cache state is not auditable"))
            if not telemetry.cache.cleared_before_sample:
                result.append(
                    ("cache_not_cleared", "cache was not cleared according to the protocol")
                )
            if any(
                process.uses_accelerator and not process.managed_by_stage0
                for process in telemetry.background_processes
            ):
                result.append(
                    ("background_accelerator_process", "unmanaged accelerator process was observed")
                )
        if (
            set(temperatures) == {"before_run", "after_run"}
            and abs(temperatures["after_run"] - temperatures["before_run"])
            > gate.max_temperature_delta_c
        ):
            result.append(
                ("temperature_drift_exceeded", "run temperature drift exceeds the registered limit")
            )
        return result


def classify_profiler(
    evidence: ProfilerEvidenceV2,
    reader: Stage0EvidenceReader,
) -> ProfilerCapability:
    tool_version = reader.read_raw_bytes(
        evidence.tool_version_output.uri, evidence.tool_version_output.sha256
    )
    if not tool_version.strip():
        raise Stage0EvidenceError("profiler_version_invalid", "profiler version output is empty")
    raw_output = reader.read_raw_bytes(evidence.raw_output.uri, evidence.raw_output.sha256)
    if evidence.parser_version == "rocprof-csv-v1":
        kernels = _parse_rocprof_csv(raw_output)
    elif evidence.parser_version == "torch-trace-v1":
        kernels = _parse_torch_trace(raw_output)
    else:  # pragma: no cover - Literal plus parser/tool validator closes this path.
        raise Stage0EvidenceError("profiler_parser_unsupported", evidence.parser_version)
    if not kernels:
        return ProfilerCapability.NONE
    enriched = all(
        item.shapes
        and item.dtypes
        and item.metadata
        and item.python_source is not None
        and item.hip_symbol is not None
        for item in kernels
    )
    return ProfilerCapability.FULL if enriched else ProfilerCapability.DEGRADED


def _parse_rocprof_csv(encoded: bytes) -> tuple[ProfilerKernelRecordV2, ...]:
    try:
        text = encoded.decode("utf-8", errors="strict")
        rows = csv.DictReader(io.StringIO(text, newline=""))
        fieldnames = set(rows.fieldnames or ())
        trace_header = {"Kernel_Name", "Start_Timestamp", "End_Timestamp"}
        duration_header = {"Kernel_Name", "DurationNs"}
        statistics_header = {"Name", "Calls", "TotalDurationNs"}
        if not any(
            required.issubset(fieldnames)
            for required in (trace_header, duration_header, statistics_header)
        ):
            raise ValueError("unexpected raw rocprof CSV header")
        parsed: list[ProfilerKernelRecordV2] = []
        for row in rows:
            if None in row:
                raise ValueError("rocprof CSV row has extra columns")
            name = (row.get("Kernel_Name") or row.get("Name") or "").strip()
            if not name:
                continue
            if row.get("DurationNs") not in (None, ""):
                duration_ns = float(row["DurationNs"])
            elif row.get("TotalDurationNs") not in (None, ""):
                duration_ns = float(row["TotalDurationNs"])
            else:
                duration_ns = float(row["End_Timestamp"]) - float(row["Start_Timestamp"])
            calls = int(row.get("Calls") or 1)
            metadata = {
                key: value
                for key, value in row.items()
                if key
                not in {
                    "Kernel_Name",
                    "Name",
                    "DurationNs",
                    "TotalDurationNs",
                    "Calls",
                }
                and value not in (None, "")
            }
            parsed.append(
                ProfilerKernelRecordV2(
                    kernel_name=name,
                    duration_ns=duration_ns,
                    call_count=calls,
                    shapes=(),
                    dtypes=(),
                    metadata=metadata,
                    python_source=None,
                    hip_symbol=name,
                )
            )
    except (UnicodeError, csv.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise Stage0EvidenceError("profiler_raw_invalid", str(exc)) from exc
    return tuple(parsed)


def _parse_torch_trace(encoded: bytes) -> tuple[ProfilerKernelRecordV2, ...]:
    try:
        if encoded.startswith(b"\x1f\x8b"):
            encoded = gzip.decompress(encoded)
        value = json.loads(
            encoded.decode("utf-8", errors="strict"),
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {item}")
            ),
        )
        events = value.get("traceEvents") if isinstance(value, dict) else value
        if not isinstance(events, list):
            raise ValueError("PyTorch trace must contain traceEvents")
        grouped: dict[str, dict[str, Any]] = {}
        for event in events:
            if not isinstance(event, dict) or event.get("ph") != "X":
                continue
            categories = {
                item.strip().lower()
                for item in str(event.get("cat", "")).replace(";", ",").split(",")
            }
            if "kernel" not in categories:
                continue
            name = str(event.get("name", "")).strip()
            duration_us = event.get("dur")
            if (
                not name
                or isinstance(duration_us, bool)
                or not isinstance(duration_us, (int, float))
                or not math.isfinite(float(duration_us))
                or float(duration_us) <= 0
            ):
                continue
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            record = grouped.setdefault(
                name,
                {
                    "duration_ns": 0.0,
                    "call_count": 0,
                    "shapes": set(),
                    "dtypes": set(),
                    "metadata": {},
                    "python_source": None,
                    "hip_symbol": None,
                },
            )
            record["duration_ns"] += float(duration_us) * 1_000.0
            record["call_count"] += 1
            record["shapes"].update(
                _trace_text_values(args, ("Input Dims", "Input shapes", "shape"))
            )
            record["dtypes"].update(_trace_text_values(args, ("Input type", "dtype")))
            metadata = _trace_first(args, ("Concrete Inputs", "metadata", "meta"))
            if metadata not in (None, "", [], {}):
                record["metadata"] = (
                    metadata if isinstance(metadata, dict) else {"trace_metadata": metadata}
                )
            python_source = _trace_first(args, ("Call stack", "python_location"))
            if python_source not in (None, ""):
                record["python_source"] = _trace_text(python_source)
            hip_symbol = _trace_first(args, ("hip_symbol", "hip_api"))
            if hip_symbol not in (None, ""):
                record["hip_symbol"] = _trace_text(hip_symbol)
        return tuple(
            ProfilerKernelRecordV2(
                kernel_name=name,
                duration_ns=record["duration_ns"],
                call_count=record["call_count"],
                shapes=tuple(sorted(record["shapes"])),
                dtypes=tuple(sorted(record["dtypes"])),
                metadata=record["metadata"],
                python_source=record["python_source"],
                hip_symbol=record["hip_symbol"],
            )
            for name, record in grouped.items()
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise Stage0EvidenceError("profiler_raw_invalid", str(exc)) from exc


def _trace_first(args: dict[str, Any], names: tuple[str, ...]) -> Any:
    return next((args[name] for name in names if args.get(name) not in (None, "")), None)


def _trace_text_values(args: dict[str, Any], names: tuple[str, ...]) -> set[str]:
    value = _trace_first(args, names)
    if value in (None, "", [], {}):
        return set()
    if isinstance(value, (list, tuple)):
        return {_trace_text(item) for item in value if item not in (None, "")}
    return {_trace_text(value)}


def _trace_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _verify_claimed_calibration(
    label: str,
    evidence: MeasurementEvidenceV2,
    recomputed: ClockCalibrationStatistics,
) -> None:
    """Reject producer summaries that disagree with the raw calibration points."""

    claimed = evidence.calibration
    for field_name, claimed_value, recomputed_value in (
        ("timer_resolution_ns", claimed.timer_resolution_ns, recomputed.timer_resolution_ns),
        ("ns_per_tick", claimed.ns_per_tick, recomputed.ns_per_tick),
        ("max_residual_ns", claimed.max_residual_ns, recomputed.max_residual_ns),
    ):
        if not math.isclose(
            claimed_value,
            recomputed_value,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise Stage0EvidenceError(
                "calibration_summary_mismatch",
                f"{label} claimed {field_name} disagrees with raw calibration points",
            )


def _verify_measurement_lifecycle(
    label: str,
    evidence: MeasurementEvidenceV2,
    reader: Stage0EvidenceReader,
) -> list[tuple[str, RawEvidenceFileV2]]:
    references: list[tuple[str, RawEvidenceFileV2]] = []
    for restart_ordinal in range(evidence.plan.restart_count):
        samples = [
            sample for sample in evidence.samples if sample.restart_ordinal == restart_ordinal
        ]
        expected_identity = (samples[0].process_id, samples[0].process_start_token)
        observations = {
            observation.phase: observation
            for observation in evidence.observations
            if observation.restart_ordinal == restart_ordinal
            and observation.phase in {"before_restart", "after_restart"}
        }
        before = observations["before_restart"]
        after = observations["after_restart"]
        assert before.process_lifecycle_record is not None
        assert after.process_lifecycle_record is not None
        started = _read_process_lifecycle(reader, before.process_lifecycle_record)
        reaped = _read_process_lifecycle(reader, after.process_lifecycle_record)
        if started.event != "started" or reaped.event != "reaped":
            raise Stage0EvidenceError(
                "process_lifecycle_invalid",
                f"{label} restart {restart_ordinal} has invalid lifecycle event order",
            )
        if (
            started.restart_ordinal != restart_ordinal
            or reaped.restart_ordinal != restart_ordinal
            or started.process_id != expected_identity[0]
            or reaped.process_id != expected_identity[0]
            or started.observer_process_id != reaped.observer_process_id
        ):
            raise Stage0EvidenceError(
                "process_lifecycle_invalid",
                f"{label} restart {restart_ordinal} lifecycle does not bind the sample process",
            )
        start_token = _proc_start_token(started.proc_stat_line, started.process_id)
        exit_token = _proc_start_token(reaped.proc_stat_line, reaped.process_id)
        if start_token != expected_identity[1] or exit_token != expected_identity[1]:
            raise Stage0EvidenceError(
                "process_lifecycle_invalid",
                f"{label} restart {restart_ordinal} start token is not derived from procfs",
            )
        if (
            before.captured_monotonic_ns != started.captured_monotonic_ns
            or after.captured_monotonic_ns != reaped.captured_monotonic_ns
            or started.captured_monotonic_ns > samples[0].started_monotonic_ns
            or reaped.captured_monotonic_ns < samples[-1].finished_monotonic_ns
            or not _terminal_wait_status(reaped.wait_status)
        ):
            raise Stage0EvidenceError(
                "process_lifecycle_invalid",
                f"{label} restart {restart_ordinal} lacks an ordered terminal wait result",
            )
        references.extend(
            (
                (f"{label}:restart:{restart_ordinal}:started", before.process_lifecycle_record),
                (f"{label}:restart:{restart_ordinal}:reaped", after.process_lifecycle_record),
            )
        )
    return references


def _read_process_lifecycle(
    reader: Stage0EvidenceReader,
    reference: RawEvidenceFileV2,
) -> ProcessLifecycleRecordV2:
    encoded = reader.read_bytes(reference.uri, reference.sha256)
    try:
        return ProcessLifecycleRecordV2.model_validate_json(encoded)
    except ValidationError as exc:
        raise Stage0EvidenceError("process_lifecycle_invalid", str(exc)) from exc


def _proc_start_token(proc_stat_line: str, expected_pid: int) -> str:
    match = re.fullmatch(r"([1-9][0-9]*) \((.*)\) ([A-Za-z]) (.*)", proc_stat_line)
    if match is None or int(match.group(1)) != expected_pid:
        raise Stage0EvidenceError(
            "process_lifecycle_invalid", "procfs stat record does not identify the process"
        )
    remaining_fields = match.group(4).split()
    if len(remaining_fields) < 19:
        raise Stage0EvidenceError(
            "process_lifecycle_invalid", "procfs stat record does not contain starttime"
        )
    try:
        start_ticks = int(remaining_fields[18])
    except ValueError as exc:
        raise Stage0EvidenceError(
            "process_lifecycle_invalid", "procfs starttime is not an integer"
        ) from exc
    if start_ticks < 1:
        raise Stage0EvidenceError("process_lifecycle_invalid", "procfs starttime must be positive")
    return f"linux-proc-startticks:{start_ticks}"


def _terminal_wait_status(wait_status: int | None) -> bool:
    if wait_status is None or not 0 <= wait_status <= 0xFFFF or wait_status == 0xFFFF:
        return False
    signal = wait_status & 0x7F
    if signal == 0:
        return wait_status & 0xFF == 0
    if signal == 0x7F:
        return False
    return wait_status >> 8 == 0


def classify_hotpatch(
    evidence: HotpatchEvidenceV2,
    reader: Stage0EvidenceReader,
    target: TargetSpec,
) -> HotPatchCapability:
    if evidence.activation_mode == "none":
        return HotPatchCapability.NONE
    assert evidence.baseline_source is not None
    assert evidence.candidate_source is not None
    assert evidence.artifact_manifest is not None
    assert evidence.artifact is not None
    assert evidence.baseline_state is not None
    assert evidence.candidate_state is not None
    assert evidence.recovery_state is not None
    baseline_source = _read_contract(
        reader, evidence.baseline_source, SourceSnapshot, "hotpatch_baseline_source_invalid"
    )
    candidate_source = _read_contract(
        reader, evidence.candidate_source, SourceSnapshot, "hotpatch_candidate_source_invalid"
    )
    artifact_manifest = _read_contract(
        reader, evidence.artifact_manifest, ArtifactManifest, "hotpatch_artifact_manifest_invalid"
    )
    artifact = reader.read_raw_bytes(evidence.artifact.uri, evidence.artifact.sha256)
    baseline = _read_hotpatch_phase(reader, evidence.baseline_state)
    candidate = _read_hotpatch_phase(reader, evidence.candidate_state)
    recovery = _read_hotpatch_phase(reader, evidence.recovery_state)
    phases = (baseline, candidate, recovery)
    if [phase.phase for phase in phases] != ["baseline", "candidate", "recovery"]:
        return HotPatchCapability.NONE
    if not _valid_source_and_artifact_chain(
        baseline_source,
        candidate_source,
        artifact_manifest,
        evidence.artifact,
        artifact,
        target,
    ):
        return HotPatchCapability.NONE
    for ordinal, phase in enumerate(phases):
        if not _valid_hotpatch_execution(evidence, phase, target):
            return HotPatchCapability.NONE
        expected_process_ordinal = ordinal if evidence.activation_mode == "startup_overlay" else 0
        if not _verify_hotpatch_process(reader, phase, expected_process_ordinal):
            return HotPatchCapability.NONE

    baseline_entries = _verified_state_entries(reader, baseline.entries)
    candidate_entries = _verified_state_entries(reader, candidate.entries)
    recovery_entries = _verified_state_entries(reader, recovery.entries)
    observations = tuple(_read_hotpatch_observation(reader, phase) for phase in phases)
    normalized_outputs = tuple(
        reader.read_raw_bytes(phase.normalized_output.uri, phase.normalized_output.sha256)
        for phase in phases
    )
    caches = tuple(
        reader.read_raw_bytes(phase.cache_namespace.uri, phase.cache_namespace.sha256)
        for phase in phases
    )
    artifact_hash = artifact_manifest.content_hash
    baseline_implementation_hashes = {digest for _, digest in baseline_entries}
    candidate_implementation_hashes = {digest for _, digest in candidate_entries}
    recovery_implementation_hashes = {digest for _, digest in recovery_entries}
    cache_directories = tuple(
        phase.execution_request.environment.get("HCUOPT_CANDIDATE_CACHE_DIR") for phase in phases
    )
    phase_relations_are_safe = all(
        (
            bool(artifact),
            baseline.source_snapshot_id == baseline_source.snapshot_id,
            candidate.source_snapshot_id == candidate_source.snapshot_id,
            recovery.source_snapshot_id == baseline_source.snapshot_id,
            observations[0].process_id == baseline.process_id,
            observations[1].process_id == candidate.process_id,
            observations[2].process_id == recovery.process_id,
            observations[0].loaded_artifact_hash is None,
            observations[0].activation_marker == "baseline",
            observations[1].loaded_artifact_hash == artifact_hash,
            observations[1].implementation_hash == artifact_hash,
            observations[1].activation_marker != "baseline",
            observations[2].loaded_artifact_hash is None,
            observations[2].activation_marker == "baseline",
            observations[0].implementation_hash == observations[2].implementation_hash,
            observations[0].implementation_hash in baseline_implementation_hashes,
            observations[1].implementation_hash in candidate_implementation_hashes,
            observations[2].implementation_hash in recovery_implementation_hashes,
            observations[0].replacement_point
            == observations[1].replacement_point
            == observations[2].replacement_point,
            baseline_entries == recovery_entries,
            candidate_entries != baseline_entries,
            observations[0].output_hash
            == observations[1].output_hash
            == observations[2].output_hash,
            normalized_outputs[0] == normalized_outputs[1] == normalized_outputs[2],
            caches[0] == caches[2],
            caches[1] != caches[0],
            cache_directories[0] == cache_directories[2],
            bool(cache_directories[1]),
            cache_directories[1] != cache_directories[0],
        )
    )
    if not phase_relations_are_safe:
        return HotPatchCapability.NONE
    identities = {(phase.process_id, phase.process_start_token) for phase in phases}
    request_ids = {phase.execution_request.request_id for phase in phases}
    if evidence.activation_mode == "runtime_hot_patch":
        if len(identities) != 1 or len(request_ids) != 1:
            return HotPatchCapability.NONE
        if (
            len(
                {
                    (phase.process_start_record.uri, phase.process_start_record.sha256)
                    for phase in phases
                }
            )
            != 1
            or len(
                {
                    (phase.process_exit_record.uri, phase.process_exit_record.sha256)
                    for phase in phases
                }
            )
            != 1
        ):
            return HotPatchCapability.NONE
        return HotPatchCapability.HOT_PATCH
    if evidence.activation_mode == "startup_overlay":
        if len(identities) != 3 or len(request_ids) != 3:
            return HotPatchCapability.NONE
        if not _valid_startup_overlay_mount(
            evidence,
            artifact_manifest,
            observations[1],
            baseline,
            candidate,
            recovery,
        ):
            return HotPatchCapability.NONE
        return HotPatchCapability.OVERLAY_ONLY
    return HotPatchCapability.NONE


def _read_hotpatch_phase(
    reader: Stage0EvidenceReader,
    reference: RawEvidenceFileV2,
) -> HotpatchPhaseManifestV2:
    encoded = reader.read_bytes(reference.uri, reference.sha256)
    try:
        return HotpatchPhaseManifestV2.model_validate_json(encoded)
    except ValidationError as exc:
        raise Stage0EvidenceError("hotpatch_state_invalid", str(exc)) from exc


def _read_hotpatch_observation(
    reader: Stage0EvidenceReader,
    phase: HotpatchPhaseManifestV2,
) -> HotpatchExecutionObservationV2:
    encoded = reader.read_raw_bytes(phase.output.uri, phase.output.sha256)
    try:
        observation = HotpatchExecutionObservationV2.model_validate_json(encoded)
    except ValidationError as exc:
        raise Stage0EvidenceError("hotpatch_output_invalid", str(exc)) from exc
    reader.read_raw_bytes(phase.normalized_output.uri, phase.normalized_output.sha256)
    if observation.output_hash != phase.normalized_output.sha256:
        raise Stage0EvidenceError(
            "hotpatch_output_invalid",
            f"{phase.phase} output hash does not bind the normalized output bytes",
        )
    return observation


def _read_contract(
    reader: Stage0EvidenceReader,
    reference: RawEvidenceFileV2,
    model: type[SourceSnapshot] | type[ArtifactManifest],
    error_code: str,
) -> SourceSnapshot | ArtifactManifest:
    encoded = reader.read_bytes(reference.uri, reference.sha256)
    try:
        return model.model_validate_json(encoded)
    except ValidationError as exc:
        raise Stage0EvidenceError(error_code, str(exc)) from exc


def _valid_source_and_artifact_chain(
    baseline: SourceSnapshot,
    candidate: SourceSnapshot,
    artifact_manifest: ArtifactManifest,
    artifact_reference: RawEvidenceFileV2,
    artifact: bytes,
    target: TargetSpec,
) -> bool:
    baseline_path = unquote(urlparse(baseline.worktree_uri).path)
    return all(
        (
            baseline.kind == "baseline",
            baseline.clean,
            baseline.parent_snapshot_id is None,
            baseline.repository == target.source_baseline.repository,
            baseline.commit == target.source_baseline.commit,
            baseline_path == target.source_baseline.clean_checkout,
            candidate.kind == "candidate",
            candidate.clean,
            candidate.parent_snapshot_id == baseline.snapshot_id,
            candidate.repository == baseline.repository,
            candidate.commit == baseline.commit,
            candidate.source_hash != baseline.source_hash,
            not artifact_manifest.synthetic,
            artifact_manifest.candidate_id is not None,
            artifact_manifest.kind in {"python_overlay", "triton_source_overlay"},
            artifact_manifest.source_snapshot_id == candidate.snapshot_id,
            artifact_manifest.uri == artifact_reference.uri,
            artifact_manifest.content_hash == artifact_reference.sha256,
            bool(artifact),
        )
    )


def _valid_hotpatch_execution(
    evidence: HotpatchEvidenceV2,
    phase: HotpatchPhaseManifestV2,
    target: TargetSpec,
) -> bool:
    request = phase.execution_request
    result = phase.execution_result
    binding = evidence.binding
    return all(
        (
            request.target_id == binding.target_id == target.target_id,
            request.lease_scope is LeaseScope.EXCLUSIVE,
            request.resource_id == binding.lease.resource_id,
            request.fencing_token == binding.lease.fencing_token,
            request.container_image == target.inference_image.immutable_reference,
            result.metadata.get("target_id") == target.target_id,
            result.metadata.get("resource_id") == binding.lease.resource_id,
            result.metadata.get("fencing_token") == binding.lease.fencing_token,
            result.metadata.get("container_image") == target.inference_image.immutable_reference,
        )
    )


def _verify_hotpatch_process(
    reader: Stage0EvidenceReader,
    phase: HotpatchPhaseManifestV2,
    ordinal: int,
) -> bool:
    started = _read_process_lifecycle(reader, phase.process_start_record)
    reaped = _read_process_lifecycle(reader, phase.process_exit_record)
    try:
        start_token = _proc_start_token(started.proc_stat_line, started.process_id)
        exit_token = _proc_start_token(reaped.proc_stat_line, reaped.process_id)
    except Stage0EvidenceError:
        return False
    identity = (phase.process_id, phase.process_start_token)
    return all(
        (
            started.event == "started",
            reaped.event == "reaped",
            started.restart_ordinal == ordinal,
            reaped.restart_ordinal == ordinal,
            started.observer_process_id == reaped.observer_process_id,
            (started.process_id, start_token) == identity,
            (reaped.process_id, exit_token) == identity,
            started.captured_monotonic_ns < reaped.captured_monotonic_ns,
            _terminal_wait_status(reaped.wait_status),
        )
    )


def _verified_state_entries(
    reader: Stage0EvidenceReader,
    entries: tuple[HotpatchStateEntryV2, ...],
) -> tuple[tuple[str, str], ...]:
    verified: list[tuple[str, str]] = []
    for entry in entries:
        reader.read_raw_bytes(entry.content.uri, entry.content.sha256)
        verified.append((entry.path, entry.content.sha256))
    return tuple(verified)


def _valid_startup_overlay_mount(
    evidence: HotpatchEvidenceV2,
    artifact: ArtifactManifest,
    observation: HotpatchExecutionObservationV2,
    baseline: HotpatchPhaseManifestV2,
    candidate: HotpatchPhaseManifestV2,
    recovery: HotpatchPhaseManifestV2,
) -> bool:
    mount = evidence.overlay_mount
    if mount is None:
        return False
    if (
        mount.source_uri != artifact.uri
        or mount.source_hash != artifact.content_hash
        or mount.target_path != observation.replacement_point
        or mount.container_id != candidate.container_id
    ):
        return False
    artifact_source = unquote(urlparse(artifact.uri).path)
    candidate_mounts = [
        item
        for item in candidate.execution_request.mounts
        if item.source == artifact_source and item.target == mount.target_path
    ]
    if len(candidate_mounts) != 1 or not candidate_mounts[0].read_only:
        return False
    return all(
        item.source != artifact_source
        for phase in (baseline, recovery)
        for item in phase.execution_request.mounts
    )


def target_fingerprint(target: TargetSpec) -> str:
    encoded = json.dumps(
        target.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def verification_input_digest(
    context: Stage0VerificationContext,
    references: dict[Stage0ProbeType, Stage0ProbeEvidenceReference],
    protocol: LoadedStage0Protocol,
) -> str:
    """Bind immutable inputs and deliberately exclude producer-owned summaries."""

    context = _snapshot_context(context)
    value = {
        "task_id": str(context.task_id),
        "stage0_run_id": str(context.stage0_run_id),
        "target_snapshot_id": str(context.target_snapshot_id),
        "target_fingerprint": context.target_fingerprint,
        "workload_id": context.workload_id,
        "expected_resource_id": context.expected_resource_id,
        "protocol_version": protocol.protocol.protocol_version,
        "protocol_hash": protocol.protocol_hash,
        "probes": [
            {
                "probe_record_id": str(reference.probe_record_id),
                "probe_type": probe_type.value,
                "raw_evidence_uri": reference.raw_evidence_uri,
                "raw_evidence_hash": reference.raw_evidence_hash,
                "adapter_provenance": [
                    item.model_dump(mode="json") for item in reference.adapter_provenance
                ],
                "lease_id": str(reference.lease_id),
                "resource_id": reference.resource_id,
                "fencing_token": reference.fencing_token,
                "cleanup_evidence": reference.cleanup_evidence,
            }
            for probe_type, reference in sorted(references.items(), key=lambda pair: pair[0].value)
        ],
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_context_target_unchanged(context: Stage0VerificationContext) -> None:
    if target_fingerprint(context.target) != context.target_fingerprint:
        raise Stage0EvidenceError(
            "target_snapshot_mutated",
            "TargetSpec changed after the verification context was created",
        )


def _snapshot_context(context: Stage0VerificationContext) -> Stage0VerificationContext:
    _require_context_target_unchanged(context)
    try:
        return Stage0VerificationContext.model_validate(
            context.model_dump(mode="python", round_trip=True)
        )
    except (AttributeError, TypeError, ValidationError) as exc:
        raise Stage0EvidenceError(
            "verification_context_invalid",
            f"Stage 0 verification context is invalid: {exc}",
        ) from exc


def _noise_statistics_dict(result: NoiseStatistics) -> dict[str, Any]:
    return {
        "mean_ns": result.mean_ns,
        "sigma_ns": result.sigma_ns,
        "cv": result.cv,
        "mde_ratio": result.mde_fraction,
        "bootstrap_ci_ns": [result.ci_lower_ns, result.ci_upper_ns],
        "restart_means_ns": list(result.restart_means_ns),
        "outlier_count": result.outlier_count,
        "sample_count": result.sample_count,
        "outlier_ratio": result.outlier_fraction,
    }


def _signal_statistics_dict(result: SignalStatistics) -> dict[str, Any]:
    return {
        "effect_ratio": result.effect_fraction,
        "bootstrap_ci": [result.ci_lower, result.ci_upper],
        "restart_effects": list(result.restart_effects),
        "baseline_restart_means_ns": list(result.baseline_restart_means_ns),
        "comparison_restart_means_ns": list(result.comparison_restart_means_ns),
        "outlier_count": result.outlier_count,
        "sample_count": result.sample_count,
        "outlier_ratio": result.outlier_fraction,
    }


def _deduplicate_failures(failures: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for code, message in failures:
        if code not in seen:
            seen.add(code)
            result.append((code, message))
    return result


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_attribute)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _is_auditable_text(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _validate_json_depth(value: object, *, maximum_depth: int) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum_depth:
            raise Stage0EvidenceError(
                "evidence_invalid_json",
                f"raw evidence exceeds maximum JSON depth {maximum_depth}",
            )
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
