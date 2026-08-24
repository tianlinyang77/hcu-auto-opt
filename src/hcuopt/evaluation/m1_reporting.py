from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import (
    SHA256_PATTERN,
    AdapterProvenance,
    EvaluationRun,
    EvidenceBundle,
    MeasurementSeries,
)
from hcuopt.contracts.v1 import ManualCandidateAdjudicationResult
from hcuopt.domain.enums import LeaseScope, ManualCandidateVerdict
from hcuopt.evaluation.m1_verifier import (
    M1CorrectnessVerificationResult,
    M1PerformanceVerificationContext,
    M1PerformanceVerificationResult,
    M1VerificationContext,
)
from hcuopt.measurement.evidence import (
    EvidenceArtifact,
    canonical_json_bytes,
    write_evidence,
    write_evidence_bytes,
)

M1_SIGNOFF_WARNING = "人工批准仅表示本 Candidate 证据已接受，不授权自动发布、自动安装或生产灰度。"


class _ReportModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class M1AdjudicationContext(_ReportModel):
    verification: M1VerificationContext
    performance_verification: M1PerformanceVerificationContext
    job_id: UUID
    round_id: UUID
    measurement: MeasurementSeries
    created_at: datetime
    adapter_provenance: tuple[AdapterProvenance, ...] = Field(min_length=1)
    additional_raw_uris: tuple[str, ...] = ()

    @field_validator("adapter_provenance", "additional_raw_uris", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_real_complete_provenance(self) -> M1AdjudicationContext:
        if any(item.implementation_kind != "real" for item in self.adapter_provenance):
            raise ValueError("M1 adjudication requires real Adapter provenance")
        known = {
            (item.profile, item.capability, item.adapter_name, item.adapter_version)
            for item in self.adapter_provenance
        }
        producer = self.measurement.adapter_provenance
        if (
            producer.profile,
            producer.capability,
            producer.adapter_name,
            producer.adapter_version,
        ) not in known:
            raise ValueError("measurement producer must be included in adjudication provenance")
        performance = self.performance_verification
        correctness = self.verification
        shared_bindings = (
            (correctness.task_id, performance.task_id),
            (correctness.candidate_id, performance.candidate_id),
            (correctness.baseline_epoch_id, performance.baseline_epoch_id),
            (correctness.target_snapshot_id, performance.target_snapshot_id),
            (correctness.target_id, performance.target_id),
            (correctness.target_fingerprint, performance.target_fingerprint),
            (correctness.workload_id, performance.workload_id),
            (correctness.workload_hash, performance.workload_hash),
            (correctness.stage0_run_id, performance.stage0_run_id),
            (correctness.stage0_protocol_hash, performance.stage0_protocol_hash),
            (correctness.stage0_mde_evidence_uri, performance.stage0_report_uri),
            (correctness.stage0_mde_evidence_hash, performance.stage0_report_hash),
            (correctness.baseline_source_hash, performance.baseline_source_hash),
            (correctness.candidate_source_hash, performance.candidate_source_hash),
            (correctness.artifact_id, performance.artifact_id),
            (correctness.artifact_hash, performance.artifact_hash),
            (correctness.resource_id, performance.resource_id),
        )
        if any(left != right for left, right in shared_bindings):
            raise ValueError("correctness and performance contexts bind different immutable inputs")
        if self.round_id != performance.round_id:
            raise ValueError("adjudication belongs to another performance Round")
        if producer.profile != performance.adapter_profile:
            raise ValueError("measurement producer uses another control-plane Adapter Profile")
        return self


class M1EvidenceSummaryV1(_ReportModel):
    schema_version: Literal["m1-adjudication-summary-v1"] = "m1-adjudication-summary-v1"
    verdict: ManualCandidateVerdict
    correctness_verdict: str
    task_id: UUID
    candidate_id: UUID
    baseline_epoch_id: UUID
    target_snapshot_id: UUID
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    workload_id: str
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    stage0_run_id: UUID
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    stage0_mde_evidence_uri: str
    stage0_mde_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_source_snapshot_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_source_snapshot_id: UUID
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_id: UUID
    artifact_hash: str = Field(pattern=SHA256_PATTERN)
    correctness_protocol_version: str
    correctness_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_spec_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_id: UUID
    correctness_input_digest: str = Field(pattern=SHA256_PATTERN)
    correctness_lease_id: UUID
    correctness_lease_scope: Literal[LeaseScope.SHARED]
    correctness_resource_id: str
    correctness_fencing_token: int = Field(ge=1)
    correctness_process_identities: tuple[str, ...]
    correctness_cache_namespaces: tuple[str, ...]
    correctness_cleanup_hash: str = Field(pattern=SHA256_PATTERN)
    correctness_provenance_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_raw_uri: str
    measurement_raw_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_protocol_version: str
    measurement_environment_fingerprint: str
    performance_lease_id: UUID
    performance_lease_scope: Literal[LeaseScope.EXCLUSIVE]
    performance_resource_id: str
    performance_fencing_token: int = Field(ge=1)
    performance_process_identities: tuple[str, ...]
    performance_cache_namespaces: tuple[str, ...]
    performance_cleanup_hash: str = Field(pattern=SHA256_PATTERN)
    performance_plan_hash: str = Field(pattern=SHA256_PATTERN)
    performance_sample_budget_hash: str = Field(pattern=SHA256_PATTERN)
    performance_input_digest: str = Field(pattern=SHA256_PATTERN)
    adjudication_input_digest: str = Field(pattern=SHA256_PATTERN)
    verifier_version: Literal["m1-d-verifier-v1"] = "m1-d-verifier-v1"
    automatic_release_allowed: Literal[False] = False

    @field_validator(
        "correctness_process_identities",
        "correctness_cache_namespaces",
        "performance_process_identities",
        "performance_cache_namespaces",
        mode="before",
    )
    @classmethod
    def freeze_audit_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def deny_automatic_release(self) -> M1EvidenceSummaryV1:
        if self.automatic_release_allowed:
            raise ValueError("M1 adjudication cannot grant automatic release")
        return self


class M1FailureEvidenceSummaryV1(_ReportModel):
    schema_version: Literal["m1-failure-adjudication-summary-v1"] = (
        "m1-failure-adjudication-summary-v1"
    )
    verdict: Literal[ManualCandidateVerdict.INVALID] = ManualCandidateVerdict.INVALID
    correctness_verdict: Literal["correct", "incorrect", "invalid"]
    task_id: UUID
    candidate_id: UUID
    baseline_epoch_id: UUID
    target_snapshot_id: UUID
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    workload_id: str
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    stage0_run_id: UUID
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    stage0_mde_evidence_uri: str
    stage0_mde_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_source_snapshot_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_source_snapshot_id: UUID
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_id: UUID
    artifact_hash: str = Field(pattern=SHA256_PATTERN)
    correctness_protocol_version: str
    correctness_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_spec_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_id: UUID
    measurement_raw_uri: str
    measurement_raw_hash: str = Field(pattern=SHA256_PATTERN)
    correctness_input_digest: str = Field(pattern=SHA256_PATTERN)
    performance_input_digest: str = Field(pattern=SHA256_PATTERN)
    adjudication_input_digest: str = Field(pattern=SHA256_PATTERN)
    correctness_failure_codes: tuple[str, ...]
    performance_failure_codes: tuple[str, ...]
    verifier_version: Literal["m1-d-verifier-v1"] = "m1-d-verifier-v1"
    automatic_release_allowed: Literal[False] = False

    @field_validator("correctness_failure_codes", "performance_failure_codes", mode="before")
    @classmethod
    def freeze_failures(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class M1FailedCandidateAdjudicationResult(_ReportModel):
    candidate_id: UUID
    verdict: Literal[ManualCandidateVerdict.INVALID] = ManualCandidateVerdict.INVALID
    evidence: EvidenceBundle
    synthetic: Literal[False] = False

    @model_validator(mode="after")
    def bind_failure_evidence(self) -> M1FailedCandidateAdjudicationResult:
        if self.evidence.candidate_id != self.candidate_id:
            raise ValueError("failure EvidenceBundle is bound to another Candidate")
        if self.evidence.summary.get("automatic_release_allowed") is not False:
            raise ValueError("failure EvidenceBundle must deny automatic release")
        if self.evidence.synthetic:
            raise ValueError("failure EvidenceBundle cannot be synthetic")
        return self


class M1ReportArtifacts(_ReportModel):
    correctness: dict[str, Any]
    performance: dict[str, Any]
    evidence_bundle: dict[str, Any]
    signoff: dict[str, Any]
    manifest: dict[str, Any]


def build_m1_adjudication_result(
    context: M1AdjudicationContext,
    correctness: M1CorrectnessVerificationResult,
    performance: M1PerformanceVerificationResult,
) -> ManualCandidateAdjudicationResult | M1FailedCandidateAdjudicationResult:
    _verify_measurement_series(context, performance)
    if correctness.verdict != "correct" or performance.verdict is ManualCandidateVerdict.INVALID:
        return _build_failure_adjudication_result(context, correctness, performance)
    if (
        context.measurement.raw_samples_uri is None
        or context.measurement.raw_samples_hash is None
        or context.measurement.environment_fingerprint is None
    ):
        raise ValueError("M1 adjudication requires immutable raw measurement evidence")
    if context.measurement.raw_samples_hash != performance.raw_evidence_hash:
        raise ValueError("performance verdict is bound to another raw measurement")
    if context.measurement.environment_fingerprint != performance.environment_fingerprint:
        raise ValueError("performance verdict is bound to another measured environment")
    if context.verification.resource_id != performance.resource_id:
        raise ValueError("correctness and performance were measured on different resources")
    if (
        correctness.cleanup_hash is None
        or correctness.provenance_hash is None
        or not correctness.process_identities
        or not correctness.cache_namespaces
    ):
        raise ValueError("correctness verdict lacks typed audit bindings")
    binding = context.verification
    input_digest = _digest(
        {
            "job_id": context.job_id,
            "round_id": context.round_id,
            "verification": binding,
            "performance_verification": context.performance_verification,
            "correctness": correctness,
            "performance": performance,
            "measurement_id": context.measurement.measurement_id,
        }
    )
    evaluation_id = uuid5(
        NAMESPACE_URL,
        f"hcuopt:m1:{context.job_id}:{binding.candidate_id}:{input_digest}:evaluation",
    )
    evidence_id = uuid5(
        NAMESPACE_URL,
        f"hcuopt:m1:{context.job_id}:{binding.candidate_id}:{input_digest}:evidence",
    )
    passed = {
        ManualCandidateVerdict.FASTER: True,
        ManualCandidateVerdict.SLOWER: False,
        ManualCandidateVerdict.INCONCLUSIVE: None,
        ManualCandidateVerdict.INVALID: None,
    }[performance.verdict]
    raw_uris = sorted(
        {
            *(item["uri"] for item in correctness.input_evidence),
            *(context.additional_raw_uris),
            binding.stage0_mde_evidence_uri,
            *(
                [context.measurement.raw_samples_uri]
                if context.measurement.raw_samples_uri is not None
                else []
            ),
        }
    )
    metrics = {
        "verdict": performance.verdict.value,
        "correctness_verdict": correctness.verdict,
        "effect_ratio": performance.effect_ratio,
        "confidence_interval": (
            list(performance.confidence_interval)
            if performance.confidence_interval is not None
            else None
        ),
        "credible_threshold": performance.credible_threshold,
        "automatic_release_allowed": False,
    }
    evaluation = EvaluationRun(
        evaluation_run_id=evaluation_id,
        task_id=binding.task_id,
        candidate_id=binding.candidate_id,
        round_id=context.round_id,
        baseline_epoch_id=binding.baseline_epoch_id,
        phase="performance",
        protocol_version=correctness.protocol_version,
        target_fingerprint=binding.target_fingerprint,
        idempotency_key=f"m1:{binding.candidate_id}:{input_digest}:evaluation",
        passed=passed,
        metrics=metrics,
        measurement=context.measurement,
        evidence_uris=raw_uris,
        adapter_provenance=list(context.adapter_provenance),
        synthetic=False,
        created_at=context.created_at,
    )
    summary = M1EvidenceSummaryV1(
        verdict=performance.verdict,
        correctness_verdict=correctness.verdict,
        task_id=binding.task_id,
        candidate_id=binding.candidate_id,
        baseline_epoch_id=binding.baseline_epoch_id,
        target_snapshot_id=binding.target_snapshot_id,
        target_fingerprint=binding.target_fingerprint,
        workload_id=binding.workload_id,
        workload_hash=binding.workload_hash,
        stage0_run_id=binding.stage0_run_id,
        stage0_protocol_hash=binding.stage0_protocol_hash,
        stage0_mde_evidence_uri=binding.stage0_mde_evidence_uri,
        stage0_mde_evidence_hash=binding.stage0_mde_evidence_hash,
        baseline_source_snapshot_id=binding.baseline_source_snapshot_id,
        baseline_source_hash=binding.baseline_source_hash,
        candidate_source_snapshot_id=binding.candidate_source_snapshot_id,
        candidate_source_hash=binding.candidate_source_hash,
        artifact_id=binding.artifact_id,
        artifact_hash=binding.artifact_hash,
        correctness_protocol_version=correctness.protocol_version,
        correctness_protocol_hash=correctness.protocol_hash,
        hotspot_spec_hash=correctness.hotspot_spec_hash,
        measurement_id=context.measurement.measurement_id,
        correctness_input_digest=correctness.input_digest,
        correctness_lease_id=binding.lease_id,
        correctness_lease_scope=binding.lease_scope,
        correctness_resource_id=binding.resource_id,
        correctness_fencing_token=binding.fencing_token,
        correctness_process_identities=correctness.process_identities,
        correctness_cache_namespaces=correctness.cache_namespaces,
        correctness_cleanup_hash=correctness.cleanup_hash,
        correctness_provenance_hash=correctness.provenance_hash,
        measurement_raw_uri=context.measurement.raw_samples_uri,
        measurement_raw_hash=context.measurement.raw_samples_hash,
        measurement_protocol_version=context.measurement.protocol_version,
        measurement_environment_fingerprint=context.measurement.environment_fingerprint,
        performance_lease_id=performance.lease_id,
        performance_lease_scope=performance.lease_scope,
        performance_resource_id=performance.resource_id,
        performance_fencing_token=performance.fencing_token,
        performance_process_identities=performance.process_identities,
        performance_cache_namespaces=performance.cache_namespaces,
        performance_cleanup_hash=performance.cleanup_hash,
        performance_plan_hash=performance.plan_hash,
        performance_sample_budget_hash=performance.sample_budget_hash,
        performance_input_digest=performance.input_digest,
        adjudication_input_digest=input_digest,
    )
    evidence = EvidenceBundle(
        evidence_id=evidence_id,
        task_id=binding.task_id,
        candidate_id=binding.candidate_id,
        baseline_epoch_id=binding.baseline_epoch_id,
        target_id=binding.target_id,
        evidence_type="m1_manual_candidate",
        protocol_version=correctness.protocol_version,
        artifact_ids=[binding.artifact_id],
        measurement_ids=[context.measurement.measurement_id],
        summary=summary.model_dump(mode="json"),
        raw_uris=raw_uris,
        adapter_provenance=list(context.adapter_provenance),
        synthetic=False,
        created_at=context.created_at,
    )
    return ManualCandidateAdjudicationResult(
        candidate_id=binding.candidate_id,
        verdict=performance.verdict,
        evaluation=evaluation,
        evidence=evidence,
        synthetic=False,
    )


def _verify_measurement_series(
    context: M1AdjudicationContext,
    performance: M1PerformanceVerificationResult,
) -> None:
    measurement = context.measurement
    if measurement.measurement_id != performance.measurement_id:
        raise ValueError("performance verdict is bound to another MeasurementSeries")
    if (
        measurement.raw_samples_uri != performance.raw_evidence_uri
        or measurement.raw_samples_hash != performance.raw_evidence_hash
    ):
        raise ValueError("MeasurementSeries is bound to another Performance Reference")
    if performance.verdict is ManualCandidateVerdict.INVALID:
        return
    authority = context.performance_verification
    verified_authority = {
        "lease_id": performance.lease_id,
        "lease_scope": performance.lease_scope,
        "resource_id": performance.resource_id,
        "fencing_token": performance.fencing_token,
        "environment_fingerprint": performance.environment_fingerprint,
        "adapter_profile": performance.adapter_profile,
    }
    expected_authority = {
        "lease_id": authority.lease_id,
        "lease_scope": authority.lease_scope,
        "resource_id": authority.resource_id,
        "fencing_token": authority.fencing_token,
        "environment_fingerprint": authority.environment_fingerprint,
        "adapter_profile": authority.adapter_profile,
    }
    if verified_authority != expected_authority:
        raise ValueError("performance verdict uses another control-plane authority")
    expected = {
        "metric_name": performance.metric_name,
        "unit": performance.unit,
        "protocol_version": performance.protocol_version,
        "sample_count": performance.sample_count,
        "warmup_count": performance.warmup_count,
        "process_restart_count": performance.process_restart_count,
        "environment_fingerprint": performance.environment_fingerprint,
    }
    if any(getattr(measurement, name) != value for name, value in expected.items()):
        raise ValueError("MeasurementSeries metadata differs from verified raw evidence")
    if measurement.adapter_provenance.profile != performance.adapter_profile:
        raise ValueError("MeasurementSeries Adapter Profile differs from control-plane authority")


def _build_failure_adjudication_result(
    context: M1AdjudicationContext,
    correctness: M1CorrectnessVerificationResult,
    performance: M1PerformanceVerificationResult,
) -> M1FailedCandidateAdjudicationResult:
    measurement = context.measurement
    if measurement.raw_samples_uri is None or measurement.raw_samples_hash is None:
        raise ValueError("failure adjudication requires the available raw measurement reference")
    binding = context.verification
    input_digest = _digest(
        {
            "job_id": context.job_id,
            "round_id": context.round_id,
            "verification": binding,
            "performance_verification": context.performance_verification,
            "correctness": correctness,
            "performance": performance,
            "measurement_id": measurement.measurement_id,
            "failure": True,
        }
    )
    evidence_id = uuid5(
        NAMESPACE_URL,
        f"hcuopt:m1:{context.job_id}:{binding.candidate_id}:{input_digest}:failure-evidence",
    )
    raw_uris = sorted(
        {
            *(item["uri"] for item in correctness.input_evidence),
            measurement.raw_samples_uri,
            binding.stage0_mde_evidence_uri,
            *context.additional_raw_uris,
        }
    )
    summary = M1FailureEvidenceSummaryV1(
        correctness_verdict=correctness.verdict,
        task_id=binding.task_id,
        candidate_id=binding.candidate_id,
        baseline_epoch_id=binding.baseline_epoch_id,
        target_snapshot_id=binding.target_snapshot_id,
        target_fingerprint=binding.target_fingerprint,
        workload_id=binding.workload_id,
        workload_hash=binding.workload_hash,
        stage0_run_id=binding.stage0_run_id,
        stage0_protocol_hash=binding.stage0_protocol_hash,
        stage0_mde_evidence_uri=binding.stage0_mde_evidence_uri,
        stage0_mde_evidence_hash=binding.stage0_mde_evidence_hash,
        baseline_source_snapshot_id=binding.baseline_source_snapshot_id,
        baseline_source_hash=binding.baseline_source_hash,
        candidate_source_snapshot_id=binding.candidate_source_snapshot_id,
        candidate_source_hash=binding.candidate_source_hash,
        artifact_id=binding.artifact_id,
        artifact_hash=binding.artifact_hash,
        correctness_protocol_version=correctness.protocol_version,
        correctness_protocol_hash=correctness.protocol_hash,
        hotspot_spec_hash=correctness.hotspot_spec_hash,
        measurement_id=measurement.measurement_id,
        measurement_raw_uri=measurement.raw_samples_uri,
        measurement_raw_hash=measurement.raw_samples_hash,
        correctness_input_digest=correctness.input_digest,
        performance_input_digest=performance.input_digest,
        adjudication_input_digest=input_digest,
        correctness_failure_codes=correctness.failure_codes,
        performance_failure_codes=performance.failure_codes,
    )
    evidence = EvidenceBundle(
        evidence_id=evidence_id,
        task_id=binding.task_id,
        candidate_id=binding.candidate_id,
        baseline_epoch_id=binding.baseline_epoch_id,
        target_id=binding.target_id,
        evidence_type="m1_candidate_failure",
        protocol_version=correctness.protocol_version,
        artifact_ids=[binding.artifact_id],
        measurement_ids=[measurement.measurement_id],
        summary=summary.model_dump(mode="json"),
        raw_uris=raw_uris,
        adapter_provenance=list(context.adapter_provenance),
        synthetic=False,
        created_at=context.created_at,
    )
    return M1FailedCandidateAdjudicationResult(
        candidate_id=binding.candidate_id,
        evidence=evidence,
    )


def write_m1_signoff_report(
    root: Path,
    result: ManualCandidateAdjudicationResult | M1FailedCandidateAdjudicationResult,
    correctness: M1CorrectnessVerificationResult,
    performance: M1PerformanceVerificationResult,
) -> M1ReportArtifacts:
    if root.is_symlink():
        raise ValueError("M1 adjudication report root must be an existing regular directory")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("M1 adjudication report root must be an existing regular directory")
    correctness_artifact = write_evidence(root / "correctness.json", correctness)
    performance_artifact = write_evidence(root / "performance.json", performance)
    evidence_artifact = write_evidence(root / "evidence-bundle.json", result.evidence)
    signoff_artifact = write_evidence_bytes(
        root / "signoff.md",
        _render_signoff(result, correctness, performance).encode("utf-8"),
    )
    manifest_value = {
        "schema_version": "m1-adjudication-manifest-v1",
        "files": {
            "correctness.json": _artifact_entry(correctness_artifact),
            "performance.json": _artifact_entry(performance_artifact),
            "evidence-bundle.json": _artifact_entry(evidence_artifact),
            "signoff.md": _artifact_entry(signoff_artifact),
        },
    }
    manifest_artifact = write_evidence(root / "sha256sums.json", manifest_value)
    return M1ReportArtifacts(
        correctness=_artifact_entry(correctness_artifact),
        performance=_artifact_entry(performance_artifact),
        evidence_bundle=_artifact_entry(evidence_artifact),
        signoff=_artifact_entry(signoff_artifact),
        manifest=_artifact_entry(manifest_artifact),
    )


def _render_signoff(
    result: ManualCandidateAdjudicationResult | M1FailedCandidateAdjudicationResult,
    correctness: M1CorrectnessVerificationResult,
    performance: M1PerformanceVerificationResult,
) -> str:
    if isinstance(result, M1FailedCandidateAdjudicationResult):
        summary = M1FailureEvidenceSummaryV1.model_validate_json(
            canonical_json_bytes(result.evidence.summary)
        )
    else:
        summary = M1EvidenceSummaryV1.model_validate_json(
            canonical_json_bytes(result.evidence.summary)
        )
    failure_codes = sorted({*correctness.failure_codes, *performance.failure_codes})
    failures = [f"- `{item}`" for item in failure_codes] or ["- 无"]
    confidence = (
        "不可用"
        if performance.confidence_interval is None
        else f"[{performance.confidence_interval[0]:.8f}, {performance.confidence_interval[1]:.8f}]"
    )
    return "\n".join(
        [
            "# M1 Candidate 人工签核摘要",
            "",
            f"- Task：`{summary.task_id}`",
            f"- Candidate：`{summary.candidate_id}`",
            f"- Baseline Epoch：`{summary.baseline_epoch_id}`",
            f"- Target Snapshot：`{summary.target_snapshot_id}`",
            f"- Workload：`{summary.workload_id}`",
            f"- 正确性：`{correctness.verdict}`",
            f"- 性能判决：`{performance.verdict.value}`",
            f"- 性能变化：`{performance.effect_ratio}`",
            f"- 置信区间：`{confidence}`",
            f"- 可信门限（Stage 0 MDE）：`{performance.credible_threshold}`",
            f"- 输入证据摘要：`{summary.adjudication_input_digest}`",
            "- 自动发布：`false`",
            "",
            "## 失败代码",
            "",
            *failures,
            "",
            f"> {M1_SIGNOFF_WARNING}",
            "",
        ]
    )


def _artifact_entry(artifact: EvidenceArtifact) -> dict[str, Any]:
    return {
        "uri": artifact.uri,
        "sha256": artifact.sha256,
        "byte_count": artifact.byte_count,
    }


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
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
