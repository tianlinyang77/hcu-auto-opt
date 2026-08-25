# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.contracts.v1 import (
    ManualCandidateAdjudicationResult,
    ManualCorrectnessResult,
    ManualPerformanceEvidenceResult,
)
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.evaluation.evidence_reader import EvidenceReadError, HashedEvidenceReader
from hcuopt.evaluation.m1_protocol import (
    LoadedM1Protocol,
    M1HotspotCorrectnessSpec,
    m1_hotspot_spec_sha256,
)
from hcuopt.evaluation.m1_reporting import (
    M1AdjudicationContext,
    build_m1_adjudication_result,
    write_m1_signoff_report,
)
from hcuopt.evaluation.m1_verifier import (
    M1CorrectnessEvidenceReference,
    M1CorrectnessEvidenceV1,
    M1CorrectnessVerificationResult,
    M1CorrectnessVerifier,
    M1PerformanceEvidenceReference,
    M1PerformanceVerificationContext,
    M1PerformanceVerifier,
    M1VerificationContext,
)
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.targets import target_fingerprint


class _M1AdapterModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class M1CorrectnessEvidenceSubmission(_M1AdapterModel):
    """Raw HCU correctness evidence returned by a deployment-owned process runner."""

    reference: M1CorrectnessEvidenceReference
    cleanup_evidence: dict[str, Any]
    adapter_provenance: tuple[AdapterProvenance, ...] = Field(min_length=1)

    @field_validator("adapter_provenance", mode="before")
    @classmethod
    def freeze_provenance(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_real_provenance_and_cleanup(self) -> M1CorrectnessEvidenceSubmission:
        if any(item.implementation_kind != "real" for item in self.adapter_provenance):
            raise ValueError("M1 correctness evidence producer must be real")
        if not cleanup_is_healthy(self.cleanup_evidence):
            raise ValueError("M1 correctness evidence requires healthy fenced cleanup")
        return self


@runtime_checkable
class M1CorrectnessEvidenceProducer(Protocol):
    """Runs the frozen reference and Candidate without making the verdict."""

    provenance: AdapterProvenance

    def produce_manual_correctness_evidence(
        self,
        payload: Mapping[str, Any],
        context: M1VerificationContext,
        hotspot: M1HotspotCorrectnessSpec,
        output_dir: Path,
    ) -> M1CorrectnessEvidenceSubmission: ...


class M1KernelCorrectnessWorkerAdapter:
    """D-owned Worker boundary around a deployment-owned correctness runner."""

    def __init__(
        self,
        *,
        profile: str,
        protocol: LoadedM1Protocol,
        reader: HashedEvidenceReader,
        producer: M1CorrectnessEvidenceProducer,
        evidence_root: Path,
    ) -> None:
        if producer.provenance.profile != profile:
            raise ValueError("M1 correctness producer uses another Adapter Profile")
        if producer.provenance.implementation_kind != "real":
            raise ValueError("M1 correctness producer must be real")
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="kernel_correctness",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )
        self.reader = reader
        self.producer = producer
        self.verifier = M1CorrectnessVerifier(protocol, reader)
        self.evidence_root = evidence_root.resolve()

    def run_manual_correctness(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> ManualCorrectnessResult:
        del output_dir
        _require_profile(payload, self.provenance.profile)
        hotspot = _load_hotspot_spec(payload, self.reader)
        context = _correctness_context(payload, payload.get("_job_context"))
        submission = M1CorrectnessEvidenceSubmission.model_validate(
            self.producer.produce_manual_correctness_evidence(
                payload,
                context,
                hotspot,
                self.evidence_root,
            )
        )
        if self.producer.provenance not in submission.adapter_provenance:
            raise ExecutionSafetyError(
                "M1 correctness evidence omits the active process-runner provenance"
            )
        _require_cleanup_binding(submission.cleanup_evidence, context)

        verification = self.verifier.verify(context, hotspot, submission.reference)
        _require_evidence_submission_matches_raw(
            self.reader,
            submission,
            allow_invalid=verification.verdict == "invalid",
        )
        verification_artifact = _publish_verification(
            self.evidence_root,
            context.candidate_id,
            verification,
        )
        provenance = _merge_provenance(
            submission.adapter_provenance,
            (self.provenance,),
        )
        return ManualCorrectnessResult(
            candidate_id=context.candidate_id,
            verdict=verification.verdict,
            protocol_version=verification.protocol_version,
            raw_evidence_uri=submission.reference.uri,
            raw_evidence_hash=submission.reference.sha256,
            verification_artifact_uri=verification_artifact.uri,
            verification_artifact_hash=verification_artifact.sha256,
            adapter_provenance=list(provenance),
            cleanup_evidence=submission.cleanup_evidence,
            synthetic=False,
        )


class M1CandidateAdjudicatorWorkerAdapter:
    """Re-read D and B raw evidence and produce the only formal M1 verdict."""

    def __init__(
        self,
        *,
        profile: str,
        protocol: LoadedM1Protocol,
        reader: HashedEvidenceReader,
        evidence_root: Path,
    ) -> None:
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="candidate_adjudicator",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )
        self.reader = reader
        self.correctness_verifier = M1CorrectnessVerifier(protocol, reader)
        self.performance_verifier = M1PerformanceVerifier(protocol, reader)
        self.evidence_root = evidence_root.resolve()

    def adjudicate_manual_candidate(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> ManualCandidateAdjudicationResult:
        del output_dir
        _require_profile(payload, self.provenance.profile)
        hotspot = _load_hotspot_spec(payload, self.reader)
        correctness_context = _correctness_context(
            payload,
            payload.get("correctness_authority"),
        )
        performance_context = _performance_context(
            payload,
            payload.get("performance_authority"),
        )
        correctness_reference = M1CorrectnessEvidenceReference(
            uri=str(payload["correctness_evidence_uri"]),
            sha256=str(payload["correctness_evidence_hash"]),
        )
        correctness = self.correctness_verifier.verify(
            correctness_context,
            hotspot,
            correctness_reference,
        )
        correctness = _bind_previous_verification(payload, self.reader, correctness)

        performance_result = ManualPerformanceEvidenceResult.model_validate(
            payload["performance_evidence"]
        )
        measurement = performance_result.measurement
        if measurement.raw_samples_uri is None or measurement.raw_samples_hash is None:
            raise ExecutionSafetyError("M1 performance result has no immutable raw evidence")
        performance_reference = M1PerformanceEvidenceReference(
            measurement_id=measurement.measurement_id,
            uri=measurement.raw_samples_uri,
            sha256=measurement.raw_samples_hash,
        )
        performance = self.performance_verifier.verify(
            correctness,
            performance_context,
            performance_reference,
        )

        producer_provenance = tuple(
            AdapterProvenance.model_validate(item)
            for item in payload.get("correctness_adapter_provenance", ())
        )
        provenance = _merge_provenance(
            producer_provenance,
            (measurement.adapter_provenance, self.provenance),
        )
        adjudication = M1AdjudicationContext(
            verification=correctness_context,
            performance_verification=performance_context,
            job_id=UUID(str(_mapping(payload.get("_job_context"), "Job context")["job_id"])),
            round_id=UUID(str(payload["round_id"])),
            measurement=measurement,
            created_at=_datetime(payload["evidence_created_at"]),
            adapter_provenance=provenance,
            additional_raw_uris=(
                str(payload["correctness_evidence_uri"]),
                str(payload["correctness_verification_artifact_uri"]),
            ),
        )
        result = build_m1_adjudication_result(adjudication, correctness, performance)
        if not isinstance(result, ManualCandidateAdjudicationResult):
            raise ExecutionSafetyError(
                "M1 invalid evidence must still produce a complete adjudication result"
            )
        report_root = (
            self.evidence_root
            / "m1"
            / str(correctness_context.candidate_id)
            / "adjudication"
            / str(adjudication.job_id)
        )
        report_root.mkdir(parents=True, exist_ok=True)
        write_m1_signoff_report(report_root, result, correctness, performance)
        return result


def _require_profile(payload: Mapping[str, Any], profile: str) -> None:
    if payload.get("adapter_profile") != profile:
        raise ExecutionSafetyError("M1 Job uses another Adapter Profile")


def _load_hotspot_spec(
    payload: Mapping[str, Any], reader: HashedEvidenceReader
) -> M1HotspotCorrectnessSpec:
    hotspot = payload.get("hotspot")
    if not isinstance(hotspot, Mapping):
        raise ExecutionSafetyError("M1 Job is missing its frozen Hotspot Intake")
    uri = hotspot.get("correctness_spec_uri")
    sha256 = hotspot.get("correctness_spec_hash")
    if not isinstance(uri, str) or not isinstance(sha256, str):
        raise ExecutionSafetyError("M1 Hotspot lacks a hashed correctness specification")
    spec = M1HotspotCorrectnessSpec.model_validate_json(reader.read_bytes(uri, sha256))
    if spec.hotspot_id != str(payload.get("hotspot_id")):
        raise ExecutionSafetyError("M1 correctness specification belongs to another Hotspot")
    if m1_hotspot_spec_sha256(spec) != sha256:
        raise ExecutionSafetyError("M1 correctness specification Hash is not canonical")
    return spec


def _correctness_context(payload: Mapping[str, Any], authority: object) -> M1VerificationContext:
    lease = _lease_authority(authority, LeaseScope.SHARED)
    target = _target(payload)
    baseline = SourceSnapshot.model_validate(payload["baseline_source"])
    candidate = SourceSnapshot.model_validate(payload["candidate_source"])
    artifact = ArtifactManifest.model_validate(payload["artifact"])
    stage0 = _mapping(payload.get("stage0_report"), "Stage 0 report")
    return M1VerificationContext(
        task_id=UUID(str(payload["task_id"])),
        candidate_id=UUID(str(payload["candidate_id"])),
        baseline_epoch_id=UUID(str(payload["baseline_epoch_id"])),
        target_snapshot_id=UUID(str(payload["target_snapshot_id"])),
        target_id=target.target_id,
        target_fingerprint=str(payload["target_fingerprint"]),
        workload_id=str(payload["workload_id"]),
        workload_hash=str(payload["workload_hash"]),
        stage0_run_id=UUID(str(payload["stage0_run_id"])),
        stage0_protocol_hash=str(payload["stage0_protocol_hash"]),
        stage0_mde_evidence_uri=str(stage0["uri"]),
        stage0_mde_evidence_hash=str(stage0["sha256"]),
        baseline_source_snapshot_id=baseline.snapshot_id,
        baseline_source_hash=baseline.source_hash,
        candidate_source_snapshot_id=candidate.snapshot_id,
        candidate_source_hash=candidate.source_hash,
        artifact_id=artifact.artifact_id,
        artifact_hash=artifact.content_hash,
        lease_id=UUID(str(lease["lease_id"])),
        lease_scope=LeaseScope.SHARED,
        resource_id=str(lease["resource_id"]),
        fencing_token=int(lease["fencing_token"]),
    )


def _performance_context(
    payload: Mapping[str, Any], authority: object
) -> M1PerformanceVerificationContext:
    lease = _lease_authority(authority, LeaseScope.EXCLUSIVE)
    target = _target(payload)
    baseline = SourceSnapshot.model_validate(payload["baseline_source"])
    candidate = SourceSnapshot.model_validate(payload["candidate_source"])
    artifact = ArtifactManifest.model_validate(payload["artifact"])
    stage0 = _mapping(payload.get("stage0_report"), "Stage 0 report")
    return M1PerformanceVerificationContext(
        task_id=UUID(str(payload["task_id"])),
        candidate_id=UUID(str(payload["candidate_id"])),
        round_id=UUID(str(payload["round_id"])),
        baseline_epoch_id=UUID(str(payload["baseline_epoch_id"])),
        stage0_run_id=UUID(str(payload["stage0_run_id"])),
        target_snapshot_id=UUID(str(payload["target_snapshot_id"])),
        target_id=target.target_id,
        target_fingerprint=str(payload["target_fingerprint"]),
        environment_fingerprint=stable_fingerprint(target.model_dump(mode="json")),
        workload_id=str(payload["workload_id"]),
        workload_hash=str(payload["workload_hash"]),
        configuration_hash=str(payload["configuration_hash"]),
        image_digest=target.inference_image.registry_digest,
        baseline_source_hash=baseline.source_hash,
        candidate_source_hash=candidate.source_hash,
        artifact_id=artifact.artifact_id,
        artifact_hash=artifact.content_hash,
        adapter_profile=str(payload["adapter_profile"]),
        stage0_report_uri=str(stage0["uri"]),
        stage0_report_hash=str(stage0["sha256"]),
        stage0_input_digest=str(stage0["input_digest"]),
        stage0_protocol_version=str(stage0["protocol_version"]),
        stage0_protocol_hash=str(stage0["protocol_hash"]),
        stage0_task_id=UUID(str(stage0["stage0_task_id"])),
        stage0_workload_id=str(stage0["stage0_workload_id"]),
        stage0_adapter_profile=str(stage0["stage0_adapter_profile"]),
        lease_id=UUID(str(lease["lease_id"])),
        lease_scope=LeaseScope.EXCLUSIVE,
        resource_id=str(lease["resource_id"]),
        fencing_token=int(lease["fencing_token"]),
    )


def _target(payload: Mapping[str, Any]) -> TargetSpec:
    target = TargetSpec.model_validate(payload["target"])
    if payload.get("target_fingerprint") != target_fingerprint(target):
        raise ExecutionSafetyError("M1 Target fingerprint differs from TargetSpec")
    return target


def _lease_authority(value: object, expected_scope: LeaseScope) -> Mapping[str, Any]:
    authority = _mapping(value, f"{expected_scope.value} lease authority")
    required = ("lease_id", "resource_id", "fencing_token")
    if authority.get("lease_scope") != expected_scope.value or any(
        authority.get(name) is None for name in required
    ):
        raise ExecutionSafetyError(
            f"M1 requires complete {expected_scope.value} control-plane lease authority"
        )
    return authority


def _require_cleanup_binding(cleanup: Mapping[str, Any], context: M1VerificationContext) -> None:
    fence = _mapping(cleanup.get("fence"), "correctness fence")
    health = _mapping(cleanup.get("health"), "correctness health")
    if (
        fence.get("fenced") is not True
        or health.get("healthy") is not True
        or fence.get("resource_id") != context.resource_id
        or fence.get("fencing_token") != context.fencing_token
        or health.get("resource_id") != context.resource_id
    ):
        raise ExecutionSafetyError("M1 correctness cleanup belongs to another lease")


def _require_evidence_submission_matches_raw(
    reader: HashedEvidenceReader,
    submission: M1CorrectnessEvidenceSubmission,
    *,
    allow_invalid: bool,
) -> None:
    try:
        raw = M1CorrectnessEvidenceV1.model_validate_json(
            reader.read_bytes(submission.reference.uri, submission.reference.sha256)
        )
    except (EvidenceReadError, ValueError):
        if allow_invalid:
            return
        raise
    if raw.cleanup_evidence != submission.cleanup_evidence:
        raise ExecutionSafetyError("M1 correctness submission cleanup differs from raw evidence")
    submitted = {
        (item.profile, item.capability, item.adapter_name, item.adapter_version)
        for item in submission.adapter_provenance
    }
    recorded = {
        (item.profile, item.capability, item.adapter_name, item.adapter_version)
        for item in raw.adapter_provenance
    }
    if not submitted.issubset(recorded):
        raise ExecutionSafetyError("M1 correctness submission provenance differs from raw evidence")


def _publish_verification(
    evidence_root: Path,
    candidate_id: UUID,
    result: M1CorrectnessVerificationResult,
):  # type: ignore[no-untyped-def]
    encoded = canonical_json_bytes(result)
    digest = hashlib.sha256(encoded).hexdigest()
    return write_evidence(
        evidence_root
        / "m1"
        / str(candidate_id)
        / "correctness-verification"
        / f"sha256-{digest}.json",
        result,
    )


def _bind_previous_verification(
    payload: Mapping[str, Any],
    reader: HashedEvidenceReader,
    current: M1CorrectnessVerificationResult,
) -> M1CorrectnessVerificationResult:
    uri = str(payload["correctness_verification_artifact_uri"])
    sha256 = str(payload["correctness_verification_artifact_hash"])
    try:
        previous = M1CorrectnessVerificationResult.model_validate_json(
            reader.read_bytes(uri, sha256)
        )
    except (EvidenceReadError, ValueError) as exc:
        return current.model_copy(
            update={
                "verdict": "invalid",
                "failure_codes": (*current.failure_codes, "verification_artifact_invalid"),
                "reasons": (*current.reasons, str(exc)),
                "input_evidence": (*current.input_evidence, {"uri": uri, "sha256": sha256}),
            }
        )
    if previous != current:
        return current.model_copy(
            update={
                "verdict": "invalid",
                "failure_codes": (*current.failure_codes, "verification_replay_mismatch"),
                "reasons": (
                    *current.reasons,
                    "correctness verification replay differs from the persisted artifact",
                ),
                "input_evidence": (*current.input_evidence, {"uri": uri, "sha256": sha256}),
            }
        )
    return current.model_copy(
        update={"input_evidence": (*current.input_evidence, {"uri": uri, "sha256": sha256})}
    )


def _merge_provenance(
    *groups: tuple[AdapterProvenance, ...],
) -> tuple[AdapterProvenance, ...]:
    result: list[AdapterProvenance] = []
    seen: set[tuple[str, str, str, str]] = set()
    for group in groups:
        for item in group:
            key = (item.profile, item.capability, item.adapter_name, item.adapter_version)
            if key not in seen:
                result.append(item)
                seen.add(key)
    return tuple(result)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionSafetyError(f"M1 {label} must be an object")
    return value


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ExecutionSafetyError("M1 evidence_created_at is invalid") from exc
        if parsed.tzinfo is None:
            raise ExecutionSafetyError("M1 evidence_created_at must include a timezone")
        return parsed
    raise ExecutionSafetyError("M1 evidence_created_at is invalid")
