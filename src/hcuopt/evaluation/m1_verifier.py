from __future__ import annotations

import hashlib
import math
import random
from enum import Enum
from statistics import fmean
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import (
    SHA256_PATTERN,
    AdapterProvenance,
    ArtifactManifest,
    SourceSnapshot,
)
from hcuopt.domain.enums import LeaseScope, ManualCandidateVerdict
from hcuopt.evaluation.evidence_reader import EvidenceReadError, HashedEvidenceReader
from hcuopt.evaluation.m1_protocol import (
    LoadedM1Protocol,
    M1HotspotCorrectnessSpec,
    M1OutputSpec,
    M1ProtocolError,
    load_registered_m1_protocol,
    m1_hotspot_spec_sha256,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.models import ProcessLifecycleRecordV2, RawEvidenceFileV2


class M1EvidenceError(ValueError):
    """M1 evidence is unsafe, malformed, or bound to another immutable input."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _M1Model(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class M1VerificationContext(_M1Model):
    task_id: UUID
    candidate_id: UUID
    baseline_epoch_id: UUID
    target_snapshot_id: UUID
    target_id: str = Field(min_length=1, max_length=200)
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    stage0_run_id: UUID
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_source_snapshot_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_source_snapshot_id: UUID
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_id: UUID
    artifact_hash: str = Field(pattern=SHA256_PATTERN)
    lease_id: UUID
    lease_scope: Literal[LeaseScope.SHARED] = LeaseScope.SHARED
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)


class M1CorrectnessEvidenceReference(_M1Model):
    uri: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=SHA256_PATTERN)


class M1CorrectnessBinding(_M1Model):
    task_id: UUID
    candidate_id: UUID
    baseline_epoch_id: UUID
    target_snapshot_id: UUID
    target_id: str = Field(min_length=1, max_length=200)
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    stage0_run_id: UUID
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    protocol_version: str = Field(min_length=1, max_length=200)
    protocol_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_spec_hash: str = Field(pattern=SHA256_PATTERN)
    lease_id: UUID
    lease_scope: Literal[LeaseScope.SHARED] = LeaseScope.SHARED
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)


class M1ProcessEvidence(_M1Model):
    variant: Literal["reference", "candidate"]
    process_id: int = Field(ge=1)
    process_start_token: str = Field(min_length=1, max_length=200)
    start_record: RawEvidenceFileV2
    exit_record: RawEvidenceFileV2
    stdout: RawEvidenceFileV2
    normalized_output: RawEvidenceFileV2
    cache_namespace: RawEvidenceFileV2


class M1CorrectnessEvidenceV1(_M1Model):
    schema_version: Literal["m1-kernel-correctness-evidence-v1"]
    binding: M1CorrectnessBinding
    baseline_source_snapshot: RawEvidenceFileV2
    candidate_source_snapshot: RawEvidenceFileV2
    reference_source: RawEvidenceFileV2
    artifact_manifest: RawEvidenceFileV2
    artifact: RawEvidenceFileV2
    executions: tuple[M1ProcessEvidence, M1ProcessEvidence]
    adapter_provenance: tuple[AdapterProvenance, ...] = Field(min_length=1)
    cleanup_evidence: dict[str, Any]
    producer_summary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("executions", "adapter_provenance", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_variants_and_provenance(self) -> M1CorrectnessEvidenceV1:
        if {item.variant for item in self.executions} != {"reference", "candidate"}:
            raise ValueError("correctness evidence requires reference and candidate executions")
        if any(item.implementation_kind != "real" for item in self.adapter_provenance):
            raise ValueError("formal M1 correctness evidence requires real provenance")
        return self


ScalarValue = bool | int | float | Literal["nan", "positive_inf", "negative_inf"]


class M1TensorOutput(_M1Model):
    name: str = Field(min_length=1, max_length=128)
    shape: tuple[int, ...] = Field(min_length=1, max_length=16)
    dtype: str = Field(min_length=1, max_length=32)
    values: tuple[ScalarValue, ...] = Field(min_length=1)

    @field_validator("shape", "values", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("values")
    @classmethod
    def validate_scalar_values(cls, value: tuple[ScalarValue, ...]) -> tuple[ScalarValue, ...]:
        for item in value:
            if isinstance(item, str) and item not in {"nan", "positive_inf", "negative_inf"}:
                raise ValueError("string tensor values must use a registered special token")
        return value


class M1OutputRecord(_M1Model):
    case_id: str = Field(min_length=1, max_length=128)
    seed: int
    special_value: str = Field(min_length=1, max_length=32)
    repeat_ordinal: int = Field(ge=0)
    inputs: tuple[M1TensorOutput, ...] = Field(min_length=1)
    outputs: tuple[M1TensorOutput, ...] = Field(min_length=1)

    @field_validator("inputs", "outputs", mode="before")
    @classmethod
    def freeze_tensors(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class M1NormalizedOutputV1(_M1Model):
    schema_version: Literal["m1-normalized-kernel-output-v1"]
    hotspot_id: str = Field(min_length=1, max_length=200)
    records: tuple[M1OutputRecord, ...] = Field(min_length=1)

    @field_validator("records", mode="before")
    @classmethod
    def freeze_records(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class M1CacheNamespaceV1(_M1Model):
    schema_version: Literal["m1-cache-namespace-v1"]
    variant: Literal["reference", "candidate"]
    namespace: str = Field(min_length=1, max_length=500)
    empty_before_execution: Literal[True]


class M1Mismatch(_M1Model):
    case_id: str
    seed: int
    special_value: str
    repeat_ordinal: int
    output_name: str
    element_index: int = Field(ge=0)
    reference: str
    candidate: str
    absolute_error: float | None = Field(default=None, ge=0)
    relative_error: float | None = Field(default=None, ge=0)


class M1CorrectnessVerificationResult(_M1Model):
    verdict: Literal["correct", "incorrect", "invalid"]
    protocol_version: str
    protocol_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_spec_hash: str = Field(pattern=SHA256_PATTERN)
    input_digest: str = Field(pattern=SHA256_PATTERN)
    checked_records: int = Field(ge=0)
    checked_elements: int = Field(ge=0)
    mismatch_count: int = Field(ge=0)
    max_absolute_error: float | None = Field(default=None, ge=0)
    max_relative_error: float | None = Field(default=None, ge=0)
    mismatches: tuple[M1Mismatch, ...] = ()
    failure_codes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    input_evidence: tuple[dict[str, str], ...] = ()
    process_identities: tuple[str, ...] = ()
    cache_namespaces: tuple[str, ...] = ()
    cleanup_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    provenance_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @field_validator(
        "mismatches",
        "failure_codes",
        "reasons",
        "input_evidence",
        "process_identities",
        "cache_namespaces",
        mode="before",
    )
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_valid_evidence_audit(self) -> M1CorrectnessVerificationResult:
        if self.verdict != "invalid" and (
            not self.process_identities
            or not self.cache_namespaces
            or self.cleanup_hash is None
            or self.provenance_hash is None
        ):
            raise ValueError("valid correctness evidence requires complete audit bindings")
        return self


class M1RestartSamples(_M1Model):
    restart_ordinal: int = Field(ge=0)
    baseline_ns: tuple[float, ...] = Field(min_length=1, max_length=1_000_000)
    candidate_ns: tuple[float, ...] = Field(min_length=1, max_length=1_000_000)

    @field_validator("baseline_ns", "candidate_ns", mode="before")
    @classmethod
    def freeze_samples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_samples(self) -> M1RestartSamples:
        if len(self.baseline_ns) != len(self.candidate_ns):
            raise ValueError("baseline and candidate sample budgets must match")
        if any(
            not math.isfinite(item) or item <= 0 for item in (*self.baseline_ns, *self.candidate_ns)
        ):
            raise ValueError("performance samples must be finite positive nanoseconds")
        return self


class M1PerformanceInput(_M1Model):
    measurement_id: UUID
    evidence_hash: str = Field(pattern=SHA256_PATTERN)
    stage0_mde_ratio: float = Field(gt=0, lt=1)
    restarts: tuple[M1RestartSamples, ...] = Field(min_length=2, max_length=1000)
    lease_id: UUID
    lease_scope: Literal[LeaseScope.EXCLUSIVE] = LeaseScope.EXCLUSIVE
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    process_identities: tuple[str, ...] = Field(min_length=2)
    cache_namespaces: tuple[str, ...] = Field(min_length=2)
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    cleanup_hash: str = Field(pattern=SHA256_PATTERN)
    cleanup_healthy: bool
    bindings_valid: bool
    producer_summary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("restarts", "process_identities", "cache_namespaces", mode="before")
    @classmethod
    def freeze_restarts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_restart_order(self) -> M1PerformanceInput:
        ordinals = [item.restart_ordinal for item in self.restarts]
        if ordinals != list(range(len(ordinals))):
            raise ValueError("performance restart ordinals must be consecutive and ordered")
        if len(self.process_identities) != len(self.restarts):
            raise ValueError("each performance restart requires one process identity")
        if len(self.cache_namespaces) != len(self.restarts):
            raise ValueError("each performance restart requires one cache namespace")
        if len(set(self.process_identities)) != len(self.process_identities):
            raise ValueError("performance restarts require distinct processes")
        if len(set(self.cache_namespaces)) != len(self.cache_namespaces):
            raise ValueError("performance restarts require distinct caches")
        return self


class M1PerformanceVerificationResult(_M1Model):
    verdict: ManualCandidateVerdict
    input_digest: str = Field(pattern=SHA256_PATTERN)
    measurement_id: UUID
    raw_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    lease_id: UUID
    lease_scope: Literal[LeaseScope.EXCLUSIVE]
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    process_identities: tuple[str, ...] = Field(min_length=2)
    cache_namespaces: tuple[str, ...] = Field(min_length=2)
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    cleanup_hash: str = Field(pattern=SHA256_PATTERN)
    effect_ratio: float | None = None
    confidence_interval: tuple[float, float] | None = None
    credible_threshold: float
    restart_effects: tuple[float, ...] = ()
    failure_codes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @field_validator(
        "confidence_interval",
        "restart_effects",
        "process_identities",
        "cache_namespaces",
        "failure_codes",
        "reasons",
        mode="before",
    )
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class M1CorrectnessVerifier:
    provenance = AdapterProvenance(
        profile="m1-d-verifier",
        capability="kernel_correctness",
        adapter_name="M1CorrectnessVerifier",
        adapter_version="1",
        implementation_kind="real",
    )

    def __init__(self, protocol: LoadedM1Protocol, reader: HashedEvidenceReader) -> None:
        try:
            registered = load_registered_m1_protocol(protocol.protocol.protocol_version)
        except M1ProtocolError as exc:
            raise M1EvidenceError("protocol_not_registered", str(exc)) from exc
        if (
            protocol.protocol_hash != registered.protocol_hash
            or protocol.canonical_bytes != registered.canonical_bytes
        ):
            raise M1EvidenceError(
                "protocol_not_registered",
                "M1 verification requires repository-registered protocol content",
            )
        if reader.max_bytes > protocol.protocol.max_evidence_bytes:
            raise M1EvidenceError(
                "evidence_budget_unenforced",
                "evidence reader limit exceeds the registered M1 protocol",
            )
        self.loaded_protocol = protocol
        self.protocol = protocol.protocol
        self.reader = reader

    def verify(
        self,
        context: M1VerificationContext,
        hotspot: M1HotspotCorrectnessSpec,
        reference: M1CorrectnessEvidenceReference,
    ) -> M1CorrectnessVerificationResult:
        input_digest = _digest(
            {
                "context": context,
                "hotspot_spec_hash": m1_hotspot_spec_sha256(hotspot),
                "evidence": reference,
                "protocol_hash": self.loaded_protocol.protocol_hash,
            }
        )
        try:
            return self._verify(context, hotspot, reference, input_digest)
        except (EvidenceReadError, M1EvidenceError, ValidationError, ValueError) as exc:
            code = getattr(exc, "code", "evidence_schema_invalid")
            return M1CorrectnessVerificationResult(
                verdict="invalid",
                protocol_version=self.protocol.protocol_version,
                protocol_hash=self.loaded_protocol.protocol_hash,
                hotspot_spec_hash=m1_hotspot_spec_sha256(hotspot),
                input_digest=input_digest,
                checked_records=0,
                checked_elements=0,
                mismatch_count=0,
                failure_codes=(str(code),),
                reasons=(str(exc),),
                input_evidence=({"uri": reference.uri, "sha256": reference.sha256},),
            )

    def _verify(
        self,
        context: M1VerificationContext,
        hotspot: M1HotspotCorrectnessSpec,
        reference: M1CorrectnessEvidenceReference,
        input_digest: str,
    ) -> M1CorrectnessVerificationResult:
        self._validate_hotspot_budget(hotspot)
        encoded = self.reader.read_bytes(reference.uri, reference.sha256)
        evidence = M1CorrectnessEvidenceV1.model_validate_json(encoded)
        self._verify_binding(context, hotspot, evidence)
        source_baseline = SourceSnapshot.model_validate_json(
            self.reader.read_bytes(
                evidence.baseline_source_snapshot.uri,
                evidence.baseline_source_snapshot.sha256,
            )
        )
        source_candidate = SourceSnapshot.model_validate_json(
            self.reader.read_bytes(
                evidence.candidate_source_snapshot.uri,
                evidence.candidate_source_snapshot.sha256,
            )
        )
        self.reader.read_raw_bytes(
            evidence.reference_source.uri,
            evidence.reference_source.sha256,
        )
        if evidence.reference_source.sha256 != hotspot.reference_source_hash:
            raise M1EvidenceError(
                "reference_source_hash_mismatch",
                "reference implementation differs from the registered Hotspot source",
            )
        artifact = ArtifactManifest.model_validate_json(
            self.reader.read_bytes(
                evidence.artifact_manifest.uri,
                evidence.artifact_manifest.sha256,
            )
        )
        self._verify_immutable_objects(context, source_baseline, source_candidate, artifact)
        self.reader.read_raw_bytes(evidence.artifact.uri, evidence.artifact.sha256)
        if evidence.artifact.sha256 != artifact.content_hash:
            raise M1EvidenceError(
                "artifact_hash_mismatch", "Artifact content differs from manifest"
            )

        by_variant = {item.variant: item for item in evidence.executions}
        outputs: dict[str, M1NormalizedOutputV1] = {}
        input_evidence = [
            {"uri": reference.uri, "sha256": reference.sha256},
            evidence.baseline_source_snapshot.model_dump(),
            evidence.candidate_source_snapshot.model_dump(),
            evidence.reference_source.model_dump(),
            evidence.artifact_manifest.model_dump(),
            evidence.artifact.model_dump(),
        ]
        process_tokens: set[tuple[int, str]] = set()
        cache_namespaces: set[str] = set()
        for variant in ("reference", "candidate"):
            process = by_variant[variant]
            self.reader.read_raw_bytes(process.stdout.uri, process.stdout.sha256)
            start = ProcessLifecycleRecordV2.model_validate_json(
                self.reader.read_bytes(process.start_record.uri, process.start_record.sha256)
            )
            exit_record = ProcessLifecycleRecordV2.model_validate_json(
                self.reader.read_bytes(process.exit_record.uri, process.exit_record.sha256)
            )
            self._verify_process(
                process,
                start,
                exit_record,
                self.protocol.max_execution_seconds,
            )
            process_tokens.add((process.process_id, process.process_start_token))
            cache = M1CacheNamespaceV1.model_validate_json(
                self.reader.read_bytes(process.cache_namespace.uri, process.cache_namespace.sha256)
            )
            if cache.variant != variant:
                raise M1EvidenceError("cache_binding_mismatch", "cache variant is incorrect")
            cache_namespaces.add(cache.namespace)
            outputs[variant] = M1NormalizedOutputV1.model_validate_json(
                self.reader.read_bytes(
                    process.normalized_output.uri,
                    process.normalized_output.sha256,
                )
            )
            for item in (
                process.start_record,
                process.exit_record,
                process.stdout,
                process.normalized_output,
                process.cache_namespace,
            ):
                input_evidence.append({"uri": item.uri, "sha256": item.sha256})
        if len(process_tokens) != 2:
            raise M1EvidenceError(
                "process_identity_reused", "reference and candidate require distinct processes"
            )
        if len(cache_namespaces) != 2:
            raise M1EvidenceError(
                "cache_namespace_reused", "reference and candidate require distinct caches"
            )
        self._verify_cleanup(context, evidence.cleanup_evidence)
        mismatches, checked_records, checked_elements, max_abs, max_rel = _compare_outputs(
            hotspot,
            outputs["reference"],
            outputs["candidate"],
            self.protocol.max_output_elements,
        )
        verdict = "correct" if not mismatches else "incorrect"
        return M1CorrectnessVerificationResult(
            verdict=verdict,
            protocol_version=self.protocol.protocol_version,
            protocol_hash=self.loaded_protocol.protocol_hash,
            hotspot_spec_hash=m1_hotspot_spec_sha256(hotspot),
            input_digest=input_digest,
            checked_records=checked_records,
            checked_elements=checked_elements,
            mismatch_count=len(mismatches),
            max_absolute_error=max_abs,
            max_relative_error=max_rel,
            mismatches=tuple(mismatches[:100]),
            failure_codes=() if not mismatches else ("output_mismatch",),
            reasons=() if not mismatches else ("Candidate output differs from reference",),
            input_evidence=tuple(input_evidence),
            process_identities=tuple(sorted(f"{pid}:{token}" for pid, token in process_tokens)),
            cache_namespaces=tuple(sorted(cache_namespaces)),
            cleanup_hash=_digest(evidence.cleanup_evidence),
            provenance_hash=_digest(evidence.adapter_provenance),
        )

    def _validate_hotspot_budget(self, hotspot: M1HotspotCorrectnessSpec) -> None:
        for case in hotspot.cases:
            if not self.protocol.min_repeats <= case.repeats <= self.protocol.max_repeats:
                raise M1EvidenceError(
                    "repeat_budget_invalid",
                    f"case {case.case_id} repeat count is outside the registered protocol",
                )

    def _verify_binding(
        self,
        context: M1VerificationContext,
        hotspot: M1HotspotCorrectnessSpec,
        evidence: M1CorrectnessEvidenceV1,
    ) -> None:
        expected = {
            "task_id": context.task_id,
            "candidate_id": context.candidate_id,
            "baseline_epoch_id": context.baseline_epoch_id,
            "target_snapshot_id": context.target_snapshot_id,
            "target_id": context.target_id,
            "target_fingerprint": context.target_fingerprint,
            "workload_id": context.workload_id,
            "workload_hash": context.workload_hash,
            "stage0_run_id": context.stage0_run_id,
            "stage0_protocol_hash": context.stage0_protocol_hash,
            "protocol_version": self.protocol.protocol_version,
            "protocol_hash": self.loaded_protocol.protocol_hash,
            "hotspot_spec_hash": m1_hotspot_spec_sha256(hotspot),
            "lease_id": context.lease_id,
            "lease_scope": context.lease_scope,
            "resource_id": context.resource_id,
            "fencing_token": context.fencing_token,
        }
        actual = evidence.binding.model_dump()
        if any(actual[name] != value for name, value in expected.items()):
            raise M1EvidenceError(
                "immutable_binding_mismatch", "correctness evidence belongs to another input"
            )

    @staticmethod
    def _verify_immutable_objects(
        context: M1VerificationContext,
        baseline: SourceSnapshot,
        candidate: SourceSnapshot,
        artifact: ArtifactManifest,
    ) -> None:
        if (
            baseline.snapshot_id != context.baseline_source_snapshot_id
            or baseline.source_hash != context.baseline_source_hash
            or baseline.kind != "baseline"
            or not baseline.clean
        ):
            raise M1EvidenceError("baseline_source_mismatch", "Baseline SourceSnapshot is invalid")
        if (
            candidate.snapshot_id != context.candidate_source_snapshot_id
            or candidate.source_hash != context.candidate_source_hash
            or candidate.kind != "candidate"
            or candidate.parent_snapshot_id != baseline.snapshot_id
            or not candidate.clean
        ):
            raise M1EvidenceError(
                "candidate_source_mismatch", "Candidate SourceSnapshot is invalid"
            )
        if (
            artifact.artifact_id != context.artifact_id
            or artifact.candidate_id != context.candidate_id
            or artifact.source_snapshot_id != candidate.snapshot_id
            or artifact.content_hash != context.artifact_hash
            or artifact.synthetic
        ):
            raise M1EvidenceError("artifact_binding_mismatch", "ArtifactManifest is invalid")

    @staticmethod
    def _verify_process(
        process: M1ProcessEvidence,
        start: ProcessLifecycleRecordV2,
        exit_record: ProcessLifecycleRecordV2,
        max_execution_seconds: int,
    ) -> None:
        if start.event != "started" or exit_record.event != "reaped":
            raise M1EvidenceError("process_lifecycle_invalid", "process was not started and reaped")
        if start.process_id != process.process_id or exit_record.process_id != process.process_id:
            raise M1EvidenceError(
                "process_identity_mismatch", "process ID does not match lifecycle"
            )
        if start.captured_monotonic_ns >= exit_record.captured_monotonic_ns:
            raise M1EvidenceError("process_lifecycle_invalid", "process timestamps are not ordered")
        duration_ns = exit_record.captured_monotonic_ns - start.captured_monotonic_ns
        if duration_ns > max_execution_seconds * 1_000_000_000:
            raise M1EvidenceError(
                "execution_budget_exceeded",
                "correctness execution exceeded the registered time budget",
            )
        if _procfs_start_token(start.proc_stat_line) != process.process_start_token:
            raise M1EvidenceError("process_identity_mismatch", "process start token is incorrect")

    @staticmethod
    def _verify_cleanup(context: M1VerificationContext, cleanup: dict[str, Any]) -> None:
        if not cleanup_is_healthy(cleanup):
            raise M1EvidenceError("cleanup_unhealthy", "correctness cleanup is not healthy")
        fence = cleanup.get("fence", {})
        health = cleanup.get("health", {})
        if (
            fence.get("resource_id") != context.resource_id
            or fence.get("fencing_token") != context.fencing_token
            or health.get("resource_id") != context.resource_id
        ):
            raise M1EvidenceError("cleanup_binding_mismatch", "cleanup belongs to another lease")


def adjudicate_performance(
    correctness: M1CorrectnessVerificationResult,
    evidence: M1PerformanceInput,
    protocol: LoadedM1Protocol,
) -> M1PerformanceVerificationResult:
    input_digest = _digest(
        {
            "correctness_input_digest": correctness.input_digest,
            "measurement_id": evidence.measurement_id,
            "evidence_hash": evidence.evidence_hash,
            "stage0_mde_ratio": evidence.stage0_mde_ratio,
            "restarts": evidence.restarts,
            "lease_id": evidence.lease_id,
            "lease_scope": evidence.lease_scope,
            "resource_id": evidence.resource_id,
            "fencing_token": evidence.fencing_token,
            "process_identities": evidence.process_identities,
            "cache_namespaces": evidence.cache_namespaces,
            "environment_fingerprint": evidence.environment_fingerprint,
            "cleanup_hash": evidence.cleanup_hash,
            "protocol_hash": protocol.protocol_hash,
        }
    )
    if correctness.verdict != "correct":
        return _invalid_performance(
            evidence,
            input_digest,
            "correctness_not_established",
            "performance cannot be judged unless correctness is correct",
        )
    if not evidence.cleanup_healthy or not evidence.bindings_valid:
        return _invalid_performance(
            evidence,
            input_digest,
            "performance_evidence_invalid",
            "performance bindings and cleanup must be valid",
        )
    effects = tuple(
        (fmean(item.baseline_ns) - fmean(item.candidate_ns)) / fmean(item.baseline_ns)
        for item in evidence.restarts
    )
    effect = fmean(effects)
    confidence = protocol.protocol.confidence_level
    lower, upper = _bootstrap_mean_ci(
        effects,
        evidence.evidence_hash,
        protocol.protocol.bootstrap_resamples,
        confidence,
    )
    threshold = evidence.stage0_mde_ratio
    if lower > threshold:
        verdict = ManualCandidateVerdict.FASTER
    elif upper < -threshold:
        verdict = ManualCandidateVerdict.SLOWER
    else:
        verdict = ManualCandidateVerdict.INCONCLUSIVE
    return M1PerformanceVerificationResult(
        verdict=verdict,
        input_digest=input_digest,
        measurement_id=evidence.measurement_id,
        raw_evidence_hash=evidence.evidence_hash,
        lease_id=evidence.lease_id,
        lease_scope=evidence.lease_scope,
        resource_id=evidence.resource_id,
        fencing_token=evidence.fencing_token,
        process_identities=evidence.process_identities,
        cache_namespaces=evidence.cache_namespaces,
        environment_fingerprint=evidence.environment_fingerprint,
        cleanup_hash=evidence.cleanup_hash,
        effect_ratio=effect,
        confidence_interval=(lower, upper),
        credible_threshold=threshold,
        restart_effects=effects,
        reasons=(
            "effect confidence interval exceeds the Stage 0 MDE"
            if verdict in {ManualCandidateVerdict.FASTER, ManualCandidateVerdict.SLOWER}
            else "effect is not distinguishable from the Stage 0 MDE",
        ),
    )


def _invalid_performance(
    evidence: M1PerformanceInput,
    input_digest: str,
    code: str,
    reason: str,
) -> M1PerformanceVerificationResult:
    return M1PerformanceVerificationResult(
        verdict=ManualCandidateVerdict.INVALID,
        input_digest=input_digest,
        measurement_id=evidence.measurement_id,
        raw_evidence_hash=evidence.evidence_hash,
        lease_id=evidence.lease_id,
        lease_scope=evidence.lease_scope,
        resource_id=evidence.resource_id,
        fencing_token=evidence.fencing_token,
        process_identities=evidence.process_identities,
        cache_namespaces=evidence.cache_namespaces,
        environment_fingerprint=evidence.environment_fingerprint,
        cleanup_hash=evidence.cleanup_hash,
        credible_threshold=evidence.stage0_mde_ratio,
        failure_codes=(code,),
        reasons=(reason,),
    )


def _compare_outputs(
    hotspot: M1HotspotCorrectnessSpec,
    reference: M1NormalizedOutputV1,
    candidate: M1NormalizedOutputV1,
    max_elements: int,
) -> tuple[list[M1Mismatch], int, int, float | None, float | None]:
    if reference.hotspot_id != hotspot.hotspot_id or candidate.hotspot_id != hotspot.hotspot_id:
        raise M1EvidenceError("hotspot_binding_mismatch", "normalized output Hotspot is wrong")
    expected_keys: set[tuple[str, int, str, int]] = set()
    cases = {item.case_id: item for item in hotspot.cases}
    for case in hotspot.cases:
        for seed in case.seeds:
            for special in case.special_values:
                for repeat in range(case.repeats):
                    expected_keys.add((case.case_id, seed, special, repeat))
    reference_by_key = _index_records(reference.records)
    candidate_by_key = _index_records(candidate.records)
    if set(reference_by_key) != expected_keys or set(candidate_by_key) != expected_keys:
        raise M1EvidenceError(
            "output_budget_mismatch", "normalized records do not match case/seed/repeat budget"
        )
    mismatches: list[M1Mismatch] = []
    checked_elements = 0
    max_abs: float | None = None
    max_rel: float | None = None
    for key in sorted(expected_keys):
        case = cases[key[0]]
        expected_inputs = {item.name: item for item in case.inputs}
        expected_outputs = {item.name: item for item in case.outputs}
        reference_inputs = _index_tensors(reference_by_key[key].inputs)
        candidate_inputs = _index_tensors(candidate_by_key[key].inputs)
        reference_outputs = _index_tensors(reference_by_key[key].outputs)
        candidate_outputs = _index_tensors(candidate_by_key[key].outputs)
        if set(reference_inputs) != set(expected_inputs) or set(candidate_inputs) != set(
            expected_inputs
        ):
            raise M1EvidenceError("input_schema_mismatch", "normalized input names differ")
        for name, spec in expected_inputs.items():
            left_input = reference_inputs[name]
            right_input = candidate_inputs[name]
            if left_input.shape != spec.shape or right_input.shape != spec.shape:
                raise M1EvidenceError("input_shape_mismatch", f"input {name} shape differs")
            if left_input.dtype != spec.dtype or right_input.dtype != spec.dtype:
                raise M1EvidenceError("input_dtype_mismatch", f"input {name} dtype differs")
            if len(left_input.values) != spec.element_count:
                raise M1EvidenceError("input_element_count_mismatch", f"input {name} size differs")
            if left_input != right_input:
                raise M1EvidenceError(
                    "input_value_mismatch",
                    f"reference and Candidate received different input {name}",
                )
        if set(reference_outputs) != set(expected_outputs) or set(candidate_outputs) != set(
            expected_outputs
        ):
            raise M1EvidenceError("output_schema_mismatch", "normalized output names differ")
        for name, spec in expected_outputs.items():
            left = reference_outputs[name]
            right = candidate_outputs[name]
            if left.shape != spec.shape or right.shape != spec.shape:
                raise M1EvidenceError("output_shape_mismatch", f"output {name} shape differs")
            if left.dtype != spec.dtype or right.dtype != spec.dtype:
                raise M1EvidenceError("output_dtype_mismatch", f"output {name} dtype differs")
            if len(left.values) != spec.element_count or len(right.values) != spec.element_count:
                raise M1EvidenceError(
                    "output_element_count_mismatch", f"output {name} size differs"
                )
            checked_elements += spec.element_count
            if checked_elements > max_elements:
                raise M1EvidenceError("output_budget_exceeded", "normalized output is too large")
            for index, (reference_value, candidate_value) in enumerate(
                zip(left.values, right.values, strict=True)
            ):
                matched, absolute, relative = _compare_scalar(
                    reference_value, candidate_value, spec
                )
                if absolute is not None:
                    max_abs = absolute if max_abs is None else max(max_abs, absolute)
                if relative is not None:
                    max_rel = relative if max_rel is None else max(max_rel, relative)
                if not matched:
                    mismatches.append(
                        M1Mismatch(
                            case_id=key[0],
                            seed=key[1],
                            special_value=key[2],
                            repeat_ordinal=key[3],
                            output_name=name,
                            element_index=index,
                            reference=str(reference_value),
                            candidate=str(candidate_value),
                            absolute_error=absolute,
                            relative_error=relative,
                        )
                    )
    return mismatches, len(expected_keys), checked_elements, max_abs, max_rel


def _index_records(
    records: tuple[M1OutputRecord, ...],
) -> dict[tuple[str, int, str, int], M1OutputRecord]:
    indexed: dict[tuple[str, int, str, int], M1OutputRecord] = {}
    for item in records:
        key = (item.case_id, item.seed, item.special_value, item.repeat_ordinal)
        if key in indexed:
            raise M1EvidenceError("output_record_duplicate", f"duplicate output record: {key}")
        indexed[key] = item
    return indexed


def _index_tensors(outputs: tuple[M1TensorOutput, ...]) -> dict[str, M1TensorOutput]:
    indexed = {item.name: item for item in outputs}
    if len(indexed) != len(outputs):
        raise M1EvidenceError("output_name_duplicate", "normalized output names repeat")
    return indexed


def _compare_scalar(
    reference: ScalarValue,
    candidate: ScalarValue,
    spec: M1OutputSpec,
) -> tuple[bool, float | None, float | None]:
    left = _decode_scalar(reference)
    right = _decode_scalar(candidate)
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right, None, None
    if spec.dtype.startswith("int"):
        return isinstance(left, int) and isinstance(right, int) and left == right, None, None
    left_float = float(left)
    right_float = float(right)
    if math.isnan(left_float) or math.isnan(right_float):
        return spec.equal_nan and math.isnan(left_float) and math.isnan(right_float), None, None
    if math.isinf(left_float) or math.isinf(right_float):
        return left_float == right_float, None, None
    absolute = abs(right_float - left_float)
    relative = absolute / abs(left_float) if left_float != 0 else None
    return absolute <= spec.atol + spec.rtol * abs(left_float), absolute, relative


def _decode_scalar(value: ScalarValue) -> bool | int | float:
    if value == "nan":
        return math.nan
    if value == "positive_inf":
        return math.inf
    if value == "negative_inf":
        return -math.inf
    return value  # type: ignore[return-value]


def _procfs_start_token(stat_line: str) -> str:
    close = stat_line.rfind(")")
    if close < 0:
        raise M1EvidenceError("process_lifecycle_invalid", "proc stat line has no comm field")
    fields = stat_line[close + 1 :].strip().split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise M1EvidenceError("process_lifecycle_invalid", "proc stat starttime is missing")
    return fields[19]


def _bootstrap_mean_ci(
    values: tuple[float, ...],
    evidence_hash: str,
    iterations: int,
    confidence: float,
) -> tuple[float, float]:
    if len(values) < 2:
        raise ValueError("bootstrap requires at least two restart effects")
    seed = int(hashlib.sha256(evidence_hash.encode("ascii")).hexdigest()[:16], 16)
    generator = random.Random(seed)
    count = len(values)
    estimates = sorted(
        fmean(values[generator.randrange(count)] for _ in range(count)) for _ in range(iterations)
    )
    tail = (1.0 - confidence) / 2.0
    return _percentile(estimates, tail), _percentile(estimates, 1.0 - tail)


def _percentile(values: list[float], probability: float) -> float:
    position = probability * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(_jsonable(value))).hexdigest()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value
