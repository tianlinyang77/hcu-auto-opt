from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN, AdapterProvenance
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    ProjectMode,
    Stage0ProbeType,
    Stage0RunMode,
)
from hcuopt.domain.models import Stage0Report
from hcuopt.evaluation.stage0_verifier import (
    Stage0VerificationContext,
    Stage0VerificationResult,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.stage0 import evaluate_stage0

STAGE0_REPORT_SCHEMA_VERSION = "stage0-verification-report-v1"
STAGE0_REPORT_DISCLAIMER = (
    "MDE 仅绑定本次 Target、Workload、指标、协议和样本预算，不是机器永久属性；"
    "Stage 0 不构成优化收益或自动发布授权。"
)

VERIFICATION_JSON_NAME = "stage0-verification.json"
VERIFICATION_MARKDOWN_NAME = "stage0-verification.md"
SHA256SUMS_NAME = "sha256sums.json"
_REPORT_FILE_NAMES = (
    VERIFICATION_JSON_NAME,
    VERIFICATION_MARKDOWN_NAME,
    SHA256SUMS_NAME,
)
_MAX_REPORT_ARTIFACT_BYTES = 16 * 1024 * 1024


class Stage0ReportError(RuntimeError):
    """Base error for deterministic Stage 0 report construction and publication."""


class Stage0ReportConflictError(Stage0ReportError):
    """An immutable report path already contains different bytes."""


class _ReportModel(ContractModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class Stage0InputEvidenceRecord(_ReportModel):
    probe_record_id: UUID
    probe_type: Stage0ProbeType
    uri: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=SHA256_PATTERN)


class Stage0GateReport(_ReportModel):
    measurement: GateResult
    profiler: ProfilerCapability
    hot_patch: HotPatchCapability


class Stage0MeasurementReport(_ReportModel):
    timer_resolution_ns: float = Field(gt=0)
    noise_sigma_ns: float = Field(ge=0)
    noise_cv: float = Field(ge=0)
    mde_ratio: float = Field(ge=0)


class Stage0FinalDecision(_ReportModel):
    mode: ProjectMode
    reasons: tuple[str, ...]
    automatic_release_allowed: Literal[False] = False

    @field_validator("reasons", mode="before")
    @classmethod
    def freeze_reasons(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


class Stage0VerificationDocument(_ReportModel):
    schema_version: Literal["stage0-verification-report-v1"] = (
        STAGE0_REPORT_SCHEMA_VERSION
    )
    task_id: UUID
    stage0_run_id: UUID
    target_snapshot_id: UUID
    target_id: str = Field(min_length=1, max_length=200)
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_snapshot: dict[str, Any]
    workload_id: str = Field(min_length=1, max_length=200)
    adapter_profile: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    run_mode: Literal[Stage0RunMode.FORMAL] = Stage0RunMode.FORMAL
    protocol_version: str = Field(min_length=1, max_length=200)
    protocol_hash: str = Field(pattern=SHA256_PATTERN)
    input_digest: str = Field(pattern=SHA256_PATTERN)
    input_evidence: tuple[Stage0InputEvidenceRecord, ...] = Field(
        min_length=len(Stage0ProbeType),
        max_length=len(Stage0ProbeType),
    )
    gates: Stage0GateReport
    measurement: Stage0MeasurementReport
    hardware_fingerprint: str = Field(pattern=SHA256_PATTERN)
    software_fingerprint: str = Field(pattern=SHA256_PATTERN)
    failure_codes: tuple[str, ...]
    verification_reasons: tuple[str, ...]
    statistics: dict[str, Any]
    verifier_provenance: AdapterProvenance
    final_decision: Stage0FinalDecision
    disclaimer: Literal[
        "MDE 仅绑定本次 Target、Workload、指标、协议和样本预算，不是机器永久属性；"
        "Stage 0 不构成优化收益或自动发布授权。"
    ] = STAGE0_REPORT_DISCLAIMER

    @field_validator(
        "input_evidence",
        "failure_codes",
        "verification_reasons",
        mode="before",
    )
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def require_complete_consistent_report(self) -> Stage0VerificationDocument:
        probe_types = tuple(item.probe_type for item in self.input_evidence)
        if len(set(probe_types)) != len(Stage0ProbeType) or set(probe_types) != set(
            Stage0ProbeType
        ):
            raise ValueError("report requires exactly one input for each Stage 0 probe")
        record_ids = {item.probe_record_id for item in self.input_evidence}
        if len(record_ids) != len(Stage0ProbeType):
            raise ValueError("report input probe record IDs must be distinct")
        if len(self.failure_codes) != len(self.verification_reasons):
            raise ValueError("failure codes and verification reasons must have equal length")
        return self


class Stage0ReportArtifact(_ReportModel):
    uri: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    byte_count: int = Field(ge=1)

    @field_validator("uri")
    @classmethod
    def require_local_file_uri(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("report artifact URI must be a local file URI")
        return value


class Stage0ReportArtifacts(_ReportModel):
    verification_json: Stage0ReportArtifact
    verification_markdown: Stage0ReportArtifact
    sha256sums: Stage0ReportArtifact


def build_stage0_verification_document(
    context: Stage0VerificationContext,
    verification: Stage0VerificationResult,
    evaluation: Stage0Report,
) -> Stage0VerificationDocument:
    """Build the canonical report payload exclusively from trusted server results."""

    try:
        context_snapshot = Stage0VerificationContext.model_validate(
            context.model_dump(mode="python", round_trip=True)
        )
        verification_snapshot = Stage0VerificationResult.model_validate(
            verification.model_dump(mode="python", round_trip=True)
        )
    except (AttributeError, TypeError, ValidationError) as exc:
        raise Stage0ReportError(f"invalid Stage 0 report input: {exc}") from exc

    if not isinstance(evaluation, Stage0Report):
        raise Stage0ReportError("evaluation must be a Stage0Report")
    if evaluation.automatic_release_allowed:
        raise Stage0ReportError("Stage 0 reports can never authorize automatic release")

    expected_evaluation = evaluate_stage0(
        verification_snapshot.to_stage0_evidence(evidence_uri="stage0-report:pending")
    )
    if evaluation != expected_evaluation:
        raise Stage0ReportError(
            "final Stage 0 evaluation does not match the independently verified gates"
        )

    try:
        input_evidence = tuple(
            sorted(
                (
                    Stage0InputEvidenceRecord.model_validate_json(
                        canonical_json_bytes(item)
                    )
                    for item in verification_snapshot.input_evidence
                ),
                key=lambda item: (item.probe_type.value, str(item.probe_record_id)),
            )
        )
        document = Stage0VerificationDocument(
            task_id=context_snapshot.task_id,
            stage0_run_id=context_snapshot.stage0_run_id,
            target_snapshot_id=context_snapshot.target_snapshot_id,
            target_id=context_snapshot.target.target_id,
            target_fingerprint=context_snapshot.target_fingerprint,
            target_snapshot=context_snapshot.target.model_dump(mode="json"),
            workload_id=context_snapshot.workload_id,
            adapter_profile=context_snapshot.adapter_profile,
            resource_id=context_snapshot.expected_resource_id,
            protocol_version=verification_snapshot.protocol_version,
            protocol_hash=verification_snapshot.protocol_hash,
            input_digest=verification_snapshot.input_digest,
            input_evidence=input_evidence,
            gates=Stage0GateReport(
                measurement=verification_snapshot.measurement,
                profiler=verification_snapshot.profiler,
                hot_patch=verification_snapshot.hot_patch,
            ),
            measurement=Stage0MeasurementReport(
                timer_resolution_ns=verification_snapshot.timer_resolution_ns,
                noise_sigma_ns=verification_snapshot.noise_sigma_ns,
                noise_cv=verification_snapshot.noise_cv,
                mde_ratio=verification_snapshot.mde_ratio,
            ),
            hardware_fingerprint=verification_snapshot.hardware_fingerprint,
            software_fingerprint=verification_snapshot.software_fingerprint,
            failure_codes=verification_snapshot.failure_codes,
            verification_reasons=verification_snapshot.reasons,
            statistics=verification_snapshot.statistics,
            verifier_provenance=verification_snapshot.verifier_provenance,
            final_decision=Stage0FinalDecision(
                mode=evaluation.mode,
                reasons=evaluation.reasons,
                automatic_release_allowed=evaluation.automatic_release_allowed,
            ),
        )
        # Validate the complete arbitrary statistics/Target subtrees as canonical JSON now,
        # rather than failing after a report directory has already been created.
        canonical_json_bytes(document)
    except (TypeError, ValueError, ValidationError) as exc:
        raise Stage0ReportError(f"cannot construct canonical Stage 0 report: {exc}") from exc
    return document


def render_stage0_verification_markdown(document: Stage0VerificationDocument) -> str:
    """Render stable Markdown with no wall-clock time or generated identifiers."""

    evidence_rows = [
        "| Probe | Record | SHA-256 | URI |",
        "| --- | --- | --- | --- |",
    ]
    evidence_rows.extend(
        f"| {_escape_markdown(item.probe_type.value)} "
        f"| `{item.probe_record_id}` "
        f"| `{item.sha256}` "
        f"| `{_escape_markdown(item.uri)}` |"
        for item in document.input_evidence
    )
    failure_lines = (
        [
            f"- `{_escape_markdown(code)}`: {_escape_markdown(reason)}"
            for code, reason in zip(
                document.failure_codes,
                document.verification_reasons,
                strict=False,
            )
        ]
        if document.failure_codes
        else ["- None"]
    )
    final_reason_lines = (
        [f"- {_escape_markdown(reason)}" for reason in document.final_decision.reasons]
        if document.final_decision.reasons
        else ["- None"]
    )
    statistics = json.dumps(
        document.statistics,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

    lines = [
        "# Stage 0 Verification Report",
        "",
        "## Binding",
        "",
        f"- Task: `{document.task_id}`",
        f"- Stage0Run: `{document.stage0_run_id}`",
        f"- Target snapshot: `{document.target_snapshot_id}`",
        f"- Target: `{_escape_markdown(document.target_id)}`",
        f"- Target fingerprint: `{document.target_fingerprint}`",
        f"- Workload: `{_escape_markdown(document.workload_id)}`",
        f"- Resource: `{_escape_markdown(document.resource_id)}`",
        f"- Adapter profile: `{_escape_markdown(document.adapter_profile)}`",
        f"- Input digest: `{document.input_digest}`",
        "",
        "## Protocol",
        "",
        f"- Version: `{_escape_markdown(document.protocol_version)}`",
        f"- SHA-256: `{document.protocol_hash}`",
        "",
        "## Gates",
        "",
        "| Gate | Result |",
        "| --- | --- |",
        f"| Measurement | `{document.gates.measurement.value}` |",
        f"| Profiler | `{document.gates.profiler.value}` |",
        f"| Hot patch | `{document.gates.hot_patch.value}` |",
        "",
        "## Recomputed measurement",
        "",
        f"- Timer resolution (ns): `{document.measurement.timer_resolution_ns}`",
        f"- Noise sigma (ns): `{document.measurement.noise_sigma_ns}`",
        f"- Noise CV: `{document.measurement.noise_cv}`",
        f"- MDE ratio: `{document.measurement.mde_ratio}`",
        "",
        "## Verification failures",
        "",
        *failure_lines,
        "",
        "## Input evidence",
        "",
        *evidence_rows,
        "",
        "## Verifier provenance",
        "",
        f"- Profile: `{_escape_markdown(document.verifier_provenance.profile)}`",
        f"- Adapter: `{_escape_markdown(document.verifier_provenance.adapter_name)}`",
        f"- Version: `{_escape_markdown(document.verifier_provenance.adapter_version)}`",
        f"- Kind: `{document.verifier_provenance.implementation_kind}`",
        "- Source commit: "
        f"`{document.verifier_provenance.source_commit or 'not-recorded'}`",
        "",
        "## Recomputed statistics",
        "",
        "```json",
        statistics,
        "```",
        "",
        "## Final project mode",
        "",
        f"- Mode: `{document.final_decision.mode.value}`",
        "- Automatic release allowed: `false`",
        "- Reasons:",
        *final_reason_lines,
        "",
        f"> {document.disclaimer}",
        "",
    ]
    return "\n".join(lines)


def write_stage0_verification_report(
    run_root: Path,
    context: Stage0VerificationContext,
    verification: Stage0VerificationResult,
    evaluation: Stage0Report,
) -> Stage0ReportArtifacts:
    """Atomically publish an immutable, exactly replayable Stage 0 report bundle."""

    document = build_stage0_verification_document(context, verification, evaluation)
    json_bytes = canonical_json_bytes(document)
    markdown_bytes = render_stage0_verification_markdown(document).encode("utf-8")

    root = _require_existing_directory(Path(run_root).absolute(), label="run root")
    report_directory = root / "verification"
    _create_or_validate_directory(report_directory)

    json_path = report_directory / VERIFICATION_JSON_NAME
    markdown_path = report_directory / VERIFICATION_MARKDOWN_NAME
    sums_path = report_directory / SHA256SUMS_NAME

    expected_json_artifact = _artifact_for(json_path, json_bytes)
    expected_markdown_artifact = _artifact_for(markdown_path, markdown_bytes)
    sums_bytes = canonical_json_bytes(
        {
            "algorithm": "sha256",
            "files": {
                VERIFICATION_JSON_NAME: expected_json_artifact.sha256,
                VERIFICATION_MARKDOWN_NAME: expected_markdown_artifact.sha256,
            },
        }
    )
    for path, encoded in (
        (json_path, json_bytes),
        (markdown_path, markdown_bytes),
        (sums_path, sums_bytes),
    ):
        _preflight_immutable(path, encoded)

    json_artifact = _publish_immutable(json_path, json_bytes)
    markdown_artifact = _publish_immutable(markdown_path, markdown_bytes)
    sums_artifact = _publish_immutable(sums_path, sums_bytes)
    return Stage0ReportArtifacts(
        verification_json=json_artifact,
        verification_markdown=markdown_artifact,
        sha256sums=sums_artifact,
    )


def _publish_immutable(path: Path, encoded: bytes) -> Stage0ReportArtifact:
    if len(encoded) > _MAX_REPORT_ARTIFACT_BYTES:
        raise Stage0ReportError(f"report artifact is too large: {path.name}")
    expected = _artifact_for(path, encoded)
    try:
        existing = _read_regular_file(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        _require_exact_replay(path, existing, encoded)
        return expected

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path, follow_symlinks=False)
            _fsync_directory(path.parent)
        except FileExistsError:
            existing = _read_regular_file(path)
            _require_exact_replay(path, existing, encoded)
    except Stage0ReportError:
        raise
    except OSError as exc:
        raise Stage0ReportError(f"cannot publish immutable report artifact: {path}") from exc
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            _fsync_directory(path.parent)
    return expected


def _preflight_immutable(path: Path, encoded: bytes) -> None:
    """Reject a conflicting bundle before creating any missing sibling artifact."""

    try:
        existing = _read_regular_file(path)
    except FileNotFoundError:
        return
    _require_exact_replay(path, existing, encoded)


def _artifact_for(path: Path, encoded: bytes) -> Stage0ReportArtifact:
    return Stage0ReportArtifact(
        uri=path.absolute().as_uri(),
        sha256="sha256:" + hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
    )


def _read_regular_file(path: Path) -> bytes:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise Stage0ReportError(f"report artifact must not be a symbolic link: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise Stage0ReportError(f"report artifact must be a regular file: {path}")
    if metadata.st_size > _MAX_REPORT_ARTIFACT_BYTES:
        raise Stage0ReportError(f"existing report artifact is too large: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(metadata, opened):
            raise Stage0ReportError(f"report artifact changed while opening: {path}")
        if opened.st_size > _MAX_REPORT_ARTIFACT_BYTES:
            raise Stage0ReportError(f"existing report artifact is too large: {path}")
        chunks: list[bytes] = []
        remaining = _MAX_REPORT_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise Stage0ReportError(f"existing report artifact is too large: {path}")
        current = os.lstat(path)
        if not _same_file(opened, current):
            raise Stage0ReportError(f"report artifact changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _require_exact_replay(path: Path, existing: bytes, expected: bytes) -> None:
    if existing != expected:
        raise Stage0ReportConflictError(
            f"immutable report artifact already contains different bytes: {path}"
        )


def _require_existing_directory(path: Path, *, label: str) -> Path:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise Stage0ReportError(f"{label} does not exist or is unreadable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise Stage0ReportError(f"{label} must not be a symbolic link: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise Stage0ReportError(f"{label} must be a directory: {path}")
    return path


def _create_or_validate_directory(path: Path) -> None:
    created = False
    try:
        os.mkdir(path, mode=0o750)
        created = True
    except FileExistsError:
        pass
    _require_existing_directory(path, label="verification report directory")
    if created:
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    # Formal execution runs on POSIX. Python does not expose a portable Windows
    # directory handle that can be passed to fsync; individual file fsync and
    # hard-link create-if-absent semantics still apply to Windows development runs.
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _escape_markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("`", "\\`").replace("\n", " ")


__all__ = [
    "SHA256SUMS_NAME",
    "STAGE0_REPORT_DISCLAIMER",
    "STAGE0_REPORT_SCHEMA_VERSION",
    "VERIFICATION_JSON_NAME",
    "VERIFICATION_MARKDOWN_NAME",
    "Stage0FinalDecision",
    "Stage0GateReport",
    "Stage0InputEvidenceRecord",
    "Stage0MeasurementReport",
    "Stage0ReportArtifact",
    "Stage0ReportArtifacts",
    "Stage0ReportConflictError",
    "Stage0ReportError",
    "Stage0VerificationDocument",
    "build_stage0_verification_document",
    "render_stage0_verification_markdown",
    "write_stage0_verification_report",
]
