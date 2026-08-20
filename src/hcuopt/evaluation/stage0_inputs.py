from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN, AdapterProvenance, TargetSpec
from hcuopt.domain.enums import (
    Stage0ProbeType,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
)
from hcuopt.evaluation.stage0_protocol import LoadedStage0Protocol
from hcuopt.evaluation.stage0_verifier import (
    Stage0EvidenceError,
    Stage0ProbeEvidenceReference,
    Stage0VerificationContext,
    verification_input_digest,
)
from hcuopt.measurement.evidence import canonical_json_bytes


class _SnapshotModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stage0FinalizationRunSnapshot(_SnapshotModel):
    stage0_run_id: UUID
    task_id: UUID
    target_snapshot_id: UUID
    adapter_profile: str = Field(min_length=1, max_length=200)
    mode: Stage0RunMode
    state: Stage0RunState
    protocol_version: str = Field(min_length=1, max_length=200)
    report: dict[str, Any] | None = None


class Stage0FinalizationTaskSnapshot(_SnapshotModel):
    task_id: UUID
    state: TaskState
    workload_id: str = Field(min_length=1, max_length=200)
    adapter_profile: str = Field(min_length=1, max_length=200)
    stage0_authority: str = Field(min_length=1, max_length=50)


class Stage0FinalizationTargetSnapshot(_SnapshotModel):
    target_snapshot_id: UUID
    target_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    specification: TargetSpec


class Stage0FinalizationProbeSnapshot(_SnapshotModel):
    probe_record_id: UUID
    task_id: UUID
    target_snapshot_id: UUID
    probe_type: Stage0ProbeType
    protocol_version: str = Field(min_length=1, max_length=200)
    raw_evidence_uri: str | None
    raw_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    adapter_provenance: tuple[AdapterProvenance, ...] = Field(min_length=1)
    synthetic: bool
    lease_id: UUID | None
    resource_id: str | None
    fencing_token: int | None
    cleanup_evidence: dict[str, Any] | None

    @field_validator("adapter_provenance", mode="before")
    @classmethod
    def freeze_provenance(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


class Stage0FinalizationSnapshot(_SnapshotModel):
    run: Stage0FinalizationRunSnapshot
    task: Stage0FinalizationTaskSnapshot
    target: Stage0FinalizationTargetSnapshot
    probes: tuple[Stage0FinalizationProbeSnapshot, ...]

    @field_validator("probes", mode="before")
    @classmethod
    def freeze_probes(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def require_unique_probe_records(self) -> Stage0FinalizationSnapshot:
        record_ids = [probe.probe_record_id for probe in self.probes]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("Stage 0 finalization contains duplicate probe record IDs")
        return self


def _verify_snapshot_bindings(snapshot: Stage0FinalizationSnapshot) -> None:
    mismatches: list[str] = []
    if snapshot.run.task_id != snapshot.task.task_id:
        mismatches.append("run.task_id")
    if snapshot.run.target_snapshot_id != snapshot.target.target_snapshot_id:
        mismatches.append("run.target_snapshot_id")
    if snapshot.run.adapter_profile != snapshot.task.adapter_profile:
        mismatches.append("run.adapter_profile")
    probe_types = {probe.probe_type for probe in snapshot.probes}
    if len(snapshot.probes) != len(Stage0ProbeType) or probe_types != set(
        Stage0ProbeType
    ):
        mismatches.append("seven_probe_barrier")
    for probe in snapshot.probes:
        if probe.task_id != snapshot.run.task_id:
            mismatches.append(f"{probe.probe_type.value}.task_id")
        if probe.target_snapshot_id != snapshot.run.target_snapshot_id:
            mismatches.append(f"{probe.probe_type.value}.target_snapshot_id")
        if probe.protocol_version != snapshot.run.protocol_version:
            mismatches.append(f"{probe.probe_type.value}.protocol_version")
    if mismatches:
        raise Stage0EvidenceError(
            "control_plane_binding_mismatch",
            "Stage 0 control-plane bindings differ: " + ", ".join(mismatches),
        )


def build_verification_inputs(
    snapshot: Stage0FinalizationSnapshot,
    protocol: LoadedStage0Protocol,
    *,
    run_root: Path | None = None,
) -> tuple[Stage0VerificationContext, dict[Stage0ProbeType, Stage0ProbeEvidenceReference]]:
    """Convert the summary-free DB snapshot into the strict D verifier inputs."""

    _verify_snapshot_bindings(snapshot)
    if snapshot.run.protocol_version != protocol.protocol.protocol_version:
        raise Stage0EvidenceError(
            "protocol_binding_mismatch",
            "Stage 0 run protocol does not match the registered verifier protocol",
        )
    resources = {probe.resource_id for probe in snapshot.probes if probe.resource_id is not None}
    if len(resources) != 1 or any(probe.resource_id is None for probe in snapshot.probes):
        raise Stage0EvidenceError(
            "resource_binding_mismatch",
            "all seven Stage 0 probes must bind one accelerator resource",
        )
    expected_resource_id = next(iter(resources))
    try:
        context = Stage0VerificationContext(
            task_id=snapshot.task.task_id,
            stage0_run_id=snapshot.run.stage0_run_id,
            target_snapshot_id=snapshot.target.target_snapshot_id,
            target=snapshot.target.specification,
            target_fingerprint=snapshot.target.target_fingerprint,
            workload_id=snapshot.task.workload_id,
            adapter_profile=snapshot.run.adapter_profile,
            expected_resource_id=expected_resource_id,
        )
        references = {
            probe.probe_type: Stage0ProbeEvidenceReference(
                probe_record_id=probe.probe_record_id,
                probe_type=probe.probe_type,
                raw_evidence_uri=_required_text(
                    probe.raw_evidence_uri,
                    probe.probe_type,
                    "raw evidence URI",
                ),
                raw_evidence_hash=_required_text(
                    probe.raw_evidence_hash,
                    probe.probe_type,
                    "raw evidence hash",
                ),
                adapter_provenance=probe.adapter_provenance,
                synthetic=probe.synthetic,
                lease_id=_required_value(
                    probe.lease_id,
                    probe.probe_type,
                    "lease ID",
                ),
                resource_id=_required_text(
                    probe.resource_id,
                    probe.probe_type,
                    "resource ID",
                ),
                fencing_token=_required_value(
                    probe.fencing_token,
                    probe.probe_type,
                    "fencing token",
                ),
                cleanup_evidence=_required_value(
                    probe.cleanup_evidence,
                    probe.probe_type,
                    "cleanup evidence",
                ),
            )
            for probe in snapshot.probes
        }
    except (TypeError, ValueError, ValidationError) as exc:
        raise Stage0EvidenceError(
            "finalization_input_invalid",
            f"Stage 0 finalization input is invalid: {exc}",
        ) from exc

    if run_root is not None:
        if not run_root.is_absolute():
            raise Stage0EvidenceError(
                "evidence_root_invalid",
                "Stage 0 evidence root must be absolute",
            )
        for probe_type, reference in references.items():
            expected_uri = (run_root / probe_type.value / "raw.json").as_uri()
            if reference.raw_evidence_uri != expected_uri:
                raise Stage0EvidenceError(
                    "evidence_layout_mismatch",
                    f"{probe_type.value} evidence must use the registered run layout",
                )
    return context, references


def stage0_snapshot_digest(snapshot: Stage0FinalizationSnapshot) -> str:
    """CAS digest for DB-selected finalization inputs; producer summary is absent by type."""

    value = {
        "run_mode": snapshot.run.mode.value,
        "run_state": snapshot.run.state.value,
        "run_adapter_profile": snapshot.run.adapter_profile,
        "task_id": str(snapshot.task.task_id),
        "task_state": snapshot.task.state.value,
        "task_adapter_profile": snapshot.task.adapter_profile,
        "stage0_authority": snapshot.task.stage0_authority,
        "stage0_run_id": str(snapshot.run.stage0_run_id),
        "target_snapshot_id": str(snapshot.target.target_snapshot_id),
        "target_id": snapshot.target.target_id,
        "target_fingerprint": snapshot.target.target_fingerprint,
        "target": snapshot.target.specification.model_dump(mode="json"),
        "workload_id": snapshot.task.workload_id,
        "protocol_version": snapshot.run.protocol_version,
        "probes": [
            {
                "probe_record_id": str(probe.probe_record_id),
                "task_id": str(probe.task_id),
                "target_snapshot_id": str(probe.target_snapshot_id),
                "probe_type": probe.probe_type.value,
                "protocol_version": probe.protocol_version,
                "raw_evidence_uri": probe.raw_evidence_uri,
                "raw_evidence_hash": probe.raw_evidence_hash,
                "adapter_provenance": [
                    item.model_dump(mode="json") for item in probe.adapter_provenance
                ],
                "synthetic": probe.synthetic,
                "lease_id": str(probe.lease_id) if probe.lease_id is not None else None,
                "resource_id": probe.resource_id,
                "fencing_token": probe.fencing_token,
                "cleanup_evidence": probe.cleanup_evidence,
            }
            for probe in sorted(snapshot.probes, key=lambda item: item.probe_type.value)
        ],
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def strict_verification_input_digest(
    snapshot: Stage0FinalizationSnapshot,
    protocol: LoadedStage0Protocol,
) -> str:
    context, references = build_verification_inputs(snapshot, protocol)
    return verification_input_digest(context, references, protocol)


def expected_verification_input_evidence(
    snapshot: Stage0FinalizationSnapshot,
    protocol: LoadedStage0Protocol,
) -> tuple[dict[str, str], ...]:
    """Return the only report input list accepted for the locked DB snapshot."""

    _, references = build_verification_inputs(snapshot, protocol)
    return tuple(
        {
            "probe_record_id": str(reference.probe_record_id),
            "probe_type": probe_type.value,
            "uri": reference.raw_evidence_uri,
            "sha256": reference.raw_evidence_hash,
        }
        for probe_type, reference in sorted(
            references.items(), key=lambda item: item[0].value
        )
    )


def _required_text(
    value: str | None,
    probe_type: Stage0ProbeType,
    name: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{probe_type.value} probe requires {name}")
    return value


def _required_value(
    value: Any | None,
    probe_type: Stage0ProbeType,
    name: str,
) -> Any:
    if value is None:
        raise ValueError(f"{probe_type.value} probe requires {name}")
    return value
