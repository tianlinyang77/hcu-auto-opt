# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.m2 import BudgetUsage
from hcuopt.contracts.platform_v1 import SHA256_PATTERN, AdapterProvenance
from hcuopt.domain.enums import LeaseScope, RoundPhase
from hcuopt.measurement.m2_models import M2PhaseBudgetReservationPlan, RoundMeasurementRef

M2_FORMAL_EXECUTION_CONTRACT_VERSION = "m2a-formal-phase-execution-v1"
_HCU_RESOURCE_PATTERN = re.compile(r"^hcu-(0|[1-9][0-9]*)$")
_CPU_AFFINITY_PATTERN = re.compile(r"^[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*$")


def _require_canonical_cpu_affinity(value: str) -> str:
    previous = -1
    for segment in value.split(","):
        bounds = tuple(int(item) for item in segment.split("-"))
        start, end = (bounds[0], bounds[0]) if len(bounds) == 1 else bounds
        if end < start or start <= previous:
            raise ValueError("CPU affinity ranges must be increasing and non-overlapping")
        previous = end
    return value


class FrozenFormalExecutionModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class M2FormalExecutionWindow(FrozenFormalExecutionModel):
    starts_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def require_bounded_aware_window(self) -> M2FormalExecutionWindow:
        for value in (self.starts_at, self.expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Formal execution window must be timezone-aware")
        if self.expires_at <= self.starts_at:
            raise ValueError("Formal execution window must have positive duration")
        return self


class M2FormalExecutionAdapterProfileContent(FrozenFormalExecutionModel):
    schema_version: Literal["m2a-formal-execution-adapter-profile-v1"] = (
        "m2a-formal-execution-adapter-profile-v1"
    )
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    profile_version: str = Field(min_length=1, max_length=100)
    capability: Literal["formal_execution_adapter"] = "formal_execution_adapter"
    harness_capability: Literal["measurement_harness"] = "measurement_harness"
    supported_phases: tuple[RoundPhase, RoundPhase] = (
        RoundPhase.SEARCH,
        RoundPhase.HOLDOUT,
    )
    registration_state: Literal["implementation_ready_unregistered"] = (
        "implementation_ready_unregistered"
    )
    synthetic: Literal[False] = False
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_both_isolated_phases(self) -> M2FormalExecutionAdapterProfileContent:
        if self.supported_phases != (RoundPhase.SEARCH, RoundPhase.HOLDOUT):
            raise ValueError("Formal Adapter Profile must support isolated Search then Holdout")
        return self


def m2_formal_execution_adapter_profile_hash(
    content: M2FormalExecutionAdapterProfileContent,
) -> str:
    encoded = (
        json.dumps(
            content.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class M2FormalExecutionAdapterProfile(M2FormalExecutionAdapterProfileContent):
    profile_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_profile_hash(self) -> M2FormalExecutionAdapterProfile:
        content = M2FormalExecutionAdapterProfileContent.model_validate(
            self.model_dump(mode="json", exclude={"profile_hash"})
        )
        if m2_formal_execution_adapter_profile_hash(content) != self.profile_hash:
            raise ValueError("Formal Adapter Profile content does not match profile_hash")
        return self


def publish_m2_formal_execution_adapter_profile(
    content: M2FormalExecutionAdapterProfileContent,
) -> M2FormalExecutionAdapterProfile:
    return M2FormalExecutionAdapterProfile.model_validate(
        {
            **content.model_dump(mode="json"),
            "profile_hash": m2_formal_execution_adapter_profile_hash(content),
        }
    )


class M2FormalTargetLockRefreshContent(FrozenFormalExecutionModel):
    schema_version: Literal["m2a-formal-target-lock-refresh-v1"] = (
        "m2a-formal-target-lock-refresh-v1"
    )
    refresh_id: UUID
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    authority_context_hash: str = Field(pattern=SHA256_PATTERN)
    target_snapshot_id: UUID
    target_lock_hash: str = Field(pattern=SHA256_PATTERN)
    host_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    resource_id: str = Field(pattern=r"^hcu-(0|[1-9][0-9]*)$")
    device_index: int = Field(ge=0)
    numa_node: int = Field(ge=0)
    cpu_affinity: str = Field(pattern=_CPU_AFFINITY_PATTERN.pattern)
    lease_id: UUID
    fencing_token: int = Field(ge=1)
    observed_at: datetime
    valid_until: datetime
    status: Literal["matched", "mismatched", "failed"]
    mismatch_codes: tuple[str, ...] = ()
    evidence_uri: str = Field(min_length=1, max_length=4000)
    evidence_hash: str = Field(pattern=SHA256_PATTERN)
    adapter_provenance: AdapterProvenance
    synthetic: bool = False
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @field_validator("cpu_affinity")
    @classmethod
    def require_canonical_cpu_affinity(cls, value: str) -> str:
        return _require_canonical_cpu_affinity(value)

    @model_validator(mode="after")
    def require_bounded_refresh_evidence(self) -> M2FormalTargetLockRefreshContent:
        for value in (self.observed_at, self.valid_until):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Target Lock refresh timestamps must be timezone-aware")
        if self.valid_until <= self.observed_at:
            raise ValueError("Target Lock refresh validity must follow observation")
        if self.resource_id != f"hcu-{self.device_index}":
            raise ValueError("Target Lock refresh HCU resource and device index differ")
        if self.status == "matched" and self.mismatch_codes:
            raise ValueError("matched Target Lock refresh cannot contain mismatch codes")
        if self.status != "matched" and not self.mismatch_codes:
            raise ValueError("failed Target Lock refresh requires mismatch codes")
        if self.adapter_provenance.capability != "target_lock_refresh":
            raise ValueError("Target Lock refresh requires its dedicated capability")
        if self.synthetic == (self.adapter_provenance.implementation_kind == "real"):
            raise ValueError("Target Lock refresh provenance and synthetic flag differ")
        return self


def m2_formal_target_lock_refresh_hash(
    content: M2FormalTargetLockRefreshContent,
) -> str:
    encoded = (
        json.dumps(
            content.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class M2FormalTargetLockRefreshReport(M2FormalTargetLockRefreshContent):
    report_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_report_hash(self) -> M2FormalTargetLockRefreshReport:
        content = M2FormalTargetLockRefreshContent.model_validate(
            self.model_dump(mode="json", exclude={"report_hash"})
        )
        if m2_formal_target_lock_refresh_hash(content) != self.report_hash:
            raise ValueError("Target Lock refresh content does not match report_hash")
        return self


def publish_m2_formal_target_lock_refresh(
    content: M2FormalTargetLockRefreshContent,
) -> M2FormalTargetLockRefreshReport:
    return M2FormalTargetLockRefreshReport.model_validate(
        {
            **content.model_dump(mode="json"),
            "report_hash": m2_formal_target_lock_refresh_hash(content),
        }
    )


class M2FormalExecutionBinding(FrozenFormalExecutionModel):
    formal_authorization_id: UUID
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    authority_context_id: UUID
    authority_context_hash: str = Field(pattern=SHA256_PATTERN)
    round_id: UUID
    task_id: UUID
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    round_candidate_id: UUID
    candidate_id: UUID
    artifact_id: UUID
    artifact_hash: str = Field(pattern=SHA256_PATTERN)
    target_snapshot_id: UUID
    target_profile_hash: str = Field(pattern=SHA256_PATTERN)
    workload_profile_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_profile_hash: str = Field(pattern=SHA256_PATTERN)
    target_lock_hash: str = Field(pattern=SHA256_PATTERN)
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    adapter_profile: str = Field(min_length=1, max_length=200)
    adapter_profile_version: str = Field(min_length=1, max_length=100)
    adapter_profile_hash: str = Field(pattern=SHA256_PATTERN)
    phase: RoundPhase
    phase_plan_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_reveal_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    host_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    execution_host_hash: str = Field(pattern=SHA256_PATTERN)
    resource_id: str = Field(min_length=1, max_length=200)
    device_index: int = Field(ge=0)
    numa_node: int = Field(ge=0)
    cpu_affinity: str = Field(pattern=_CPU_AFFINITY_PATTERN.pattern)
    topology_lease_hash: str = Field(pattern=SHA256_PATTERN)
    lease_id: UUID
    lease_scope: Literal[LeaseScope.EXCLUSIVE] = LeaseScope.EXCLUSIVE
    fencing_token: int = Field(ge=1)
    lease_authority_hash: str = Field(pattern=SHA256_PATTERN)
    lease_receipt_uri: str = Field(min_length=1, max_length=4000)
    lease_receipt_hash: str = Field(pattern=SHA256_PATTERN)
    lease_acquired_at: datetime
    lease_last_renewed_at: datetime
    lease_renewal_due_at: datetime
    lease_renewal_sequence: int = Field(ge=0)
    lease_expires_at: datetime
    window: M2FormalExecutionWindow
    job_id: UUID
    attempt: int = Field(ge=1, le=100)

    @field_validator("resource_id")
    @classmethod
    def require_one_hcu_resource(cls, value: str) -> str:
        if _HCU_RESOURCE_PATTERN.fullmatch(value) is None:
            raise ValueError("Formal execution resource must be one hcu-<index>")
        return value

    @field_validator("cpu_affinity")
    @classmethod
    def require_canonical_cpu_affinity(cls, value: str) -> str:
        return _require_canonical_cpu_affinity(value)

    @model_validator(mode="after")
    def require_phase_and_lease_bindings(self) -> M2FormalExecutionBinding:
        lease_times = (
            self.lease_acquired_at,
            self.lease_last_renewed_at,
            self.lease_renewal_due_at,
            self.lease_expires_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in lease_times):
            raise ValueError("Formal execution Lease times must be timezone-aware")
        if not (
            self.lease_acquired_at
            <= self.lease_last_renewed_at
            < self.lease_renewal_due_at
            <= self.lease_expires_at
        ):
            raise ValueError("Formal execution Lease renewal timeline is invalid")
        if self.lease_renewal_sequence == 0 and (
            self.lease_last_renewed_at != self.lease_acquired_at
        ):
            raise ValueError("unrenewed Formal Lease must retain its acquisition time")
        if self.lease_expires_at < self.window.expires_at:
            raise ValueError("Formal execution Lease cannot expire before its window")
        if self.lease_acquired_at > self.window.starts_at:
            raise ValueError("Formal execution Lease must be acquired before its window")
        if self.resource_id != f"hcu-{self.device_index}":
            raise ValueError("Formal execution HCU resource and device index differ")
        if self.phase is RoundPhase.SEARCH:
            if (
                self.holdout_family_hash is not None
                or self.holdout_reveal_evidence_hash is not None
            ):
                raise ValueError("Search execution cannot consume Holdout reveal evidence")
        elif self.holdout_family_hash is None or self.holdout_reveal_evidence_hash is None:
            raise ValueError("Holdout execution requires reveal evidence")
        return self


class M2FormalPhaseExecutionRequest(FrozenFormalExecutionModel):
    schema_version: Literal["m2a-formal-phase-execution-request-v1"] = (
        "m2a-formal-phase-execution-request-v1"
    )
    binding: M2FormalExecutionBinding
    reservation: M2PhaseBudgetReservationPlan
    harness_payload: dict[str, Any] = Field(default_factory=dict)
    run_mode: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    producer_verdict: Literal[None] = None
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_reservation_to_execution(self) -> M2FormalPhaseExecutionRequest:
        reservation = self.reservation
        binding = self.binding
        if (
            reservation.round_id != binding.round_id
            or reservation.round_candidate_id != binding.round_candidate_id
            or reservation.candidate_id != binding.candidate_id
            or reservation.phase is not binding.phase
            or reservation.phase_plan_hash != binding.phase_plan_hash
            or reservation.measurement_plan_hash != binding.measurement_plan_hash
            or reservation.job_id != binding.job_id
            or reservation.attempt != binding.attempt
        ):
            raise ValueError("Formal execution and Budget reservation bindings differ")
        return self


def m2_formal_phase_execution_request_hash(
    request: M2FormalPhaseExecutionRequest,
) -> str:
    encoded = (
        json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


M2FormalExecutionStatus = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "fence_lost",
    "evidence_invalid",
    "cleanup_failed",
]


class M2FormalPhaseExecutionRecord(FrozenFormalExecutionModel):
    schema_version: Literal["m2a-formal-phase-execution-record-v1"] = (
        "m2a-formal-phase-execution-record-v1"
    )
    execution_id: UUID
    request_hash: str = Field(pattern=SHA256_PATTERN)
    binding: M2FormalExecutionBinding
    target_lock_refresh: M2FormalTargetLockRefreshReport
    adapter_provenance: AdapterProvenance
    status: M2FormalExecutionStatus
    started_at: datetime
    finished_at: datetime
    actual: BudgetUsage
    lease_held_seconds: float = Field(ge=0)
    harness_active_seconds: float = Field(ge=0)
    measurement_ref: RoundMeasurementRef | None = None
    usage_evidence_uri: str | None = Field(default=None, min_length=1, max_length=4000)
    usage_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    cleanup_evidence_uri: str | None = Field(default=None, min_length=1, max_length=4000)
    cleanup_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    sample_count: int = Field(default=0, ge=0, le=10_000_000)
    cleanup_evidence: dict[str, Any] | None = None
    cleanup_status: Literal["verified", "failed"]
    termination_reason: str | None = Field(default=None, min_length=1, max_length=500)
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_]{2,99}$")
    producer_verdict: Literal[None] = None
    performance_conclusion: Literal["not_measured"] = "not_measured"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_terminal_execution_shape(self) -> M2FormalPhaseExecutionRecord:
        binding = self.binding
        refresh = self.target_lock_refresh
        if (
            refresh.synthetic
            or refresh.status != "matched"
            or refresh.formal_authorization_hash != binding.formal_authorization_hash
            or refresh.authority_context_hash != binding.authority_context_hash
            or refresh.target_snapshot_id != binding.target_snapshot_id
            or refresh.target_lock_hash != binding.target_lock_hash
            or refresh.host_id != binding.host_id
            or refresh.resource_id != binding.resource_id
            or refresh.device_index != binding.device_index
            or refresh.numa_node != binding.numa_node
            or refresh.cpu_affinity != binding.cpu_affinity
            or refresh.lease_id != binding.lease_id
            or refresh.fencing_token != binding.fencing_token
        ):
            raise ValueError("Formal execution Record requires its exact real Target Lock refresh")
        for value in (self.started_at, self.finished_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Formal execution timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("Formal execution cannot finish before it starts")
        if self.harness_active_seconds > self.lease_held_seconds:
            raise ValueError("Harness time cannot exceed exclusive Lease time")
        if self.adapter_provenance.capability != "formal_execution_adapter":
            raise ValueError("Formal execution requires Formal Adapter provenance")
        if self.adapter_provenance.implementation_kind != "real":
            raise ValueError("Formal execution receipt requires real Adapter provenance")
        if (
            self.adapter_provenance.profile != binding.adapter_profile
            or self.adapter_provenance.adapter_version != binding.adapter_profile_version
        ):
            raise ValueError("Formal execution Adapter provenance differs from its Profile")
        if self.status == "succeeded":
            if self.measurement_ref is None or self.sample_count < 1:
                raise ValueError("successful Formal execution requires a Round Measurement Ref")
            if self.error_code is not None:
                raise ValueError("successful Formal execution cannot contain an error")
            if self.cleanup_status != "verified" or self.termination_reason is not None:
                raise ValueError("successful Formal execution requires verified cleanup")
        elif self.measurement_ref is not None:
            raise ValueError("failed Formal execution cannot publish a Round Measurement Ref")
        elif self.error_code is None:
            raise ValueError("failed Formal execution requires an error code")
        elif self.termination_reason is None:
            raise ValueError("failed Formal execution requires a termination reason")
        if self.cleanup_evidence is None:
            raise ValueError("Formal execution requires a terminal cleanup receipt")
        if (self.usage_evidence_uri is None) != (self.usage_evidence_hash is None):
            raise ValueError(
                "Formal execution usage evidence URI and Hash must be written together"
            )
        if (self.cleanup_evidence_uri is None) != (self.cleanup_evidence_hash is None):
            raise ValueError(
                "Formal execution cleanup evidence URI and Hash must be written together"
            )
        if (self.status == "cleanup_failed") != (self.cleanup_status == "failed"):
            raise ValueError("cleanup failure status must match its cleanup receipt")
        if self.measurement_ref is not None:
            reference = self.measurement_ref
            expected = (
                binding.round_id,
                binding.round_candidate_id,
                binding.candidate_id,
                binding.phase,
                binding.candidate_family_hash,
                binding.artifact_family_hash,
                binding.holdout_family_hash,
                binding.artifact_id,
                binding.artifact_hash,
                binding.measurement_plan_hash,
                binding.phase_plan_hash,
                binding.holdout_reveal_evidence_hash,
                binding.lease_id,
                binding.resource_id,
                binding.fencing_token,
            )
            actual = (
                reference.round_id,
                reference.round_candidate_id,
                reference.candidate_id,
                reference.phase,
                reference.candidate_family_hash,
                reference.artifact_family_hash,
                reference.holdout_family_hash,
                reference.artifact_id,
                reference.artifact_hash,
                reference.measurement_plan_hash,
                reference.phase_plan_hash,
                reference.holdout_reveal_evidence_hash,
                reference.lease_id,
                reference.resource_id,
                reference.fencing_token,
            )
            if actual != expected or reference.measurement_id is None:
                raise ValueError("Round Measurement Ref differs from Formal execution binding")
        return self


def m2_formal_execution_id_for(binding: M2FormalExecutionBinding) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "hcuopt:m2a-formal-execution:"
        f"{binding.round_id}:{binding.candidate_id}:{binding.phase.value}:"
        f"{binding.job_id}:{binding.attempt}",
    )


def m2_formal_execution_receipt_id_for(execution_id: UUID) -> UUID:
    return uuid5(execution_id, "hcuopt:m2a-formal-execution-receipt:v1")


class M2FormalPhaseExecutionReceipt(FrozenFormalExecutionModel):
    schema_version: Literal["m2a-formal-phase-execution-receipt-v1"] = (
        "m2a-formal-phase-execution-receipt-v1"
    )
    receipt_id: UUID
    execution: M2FormalPhaseExecutionRecord
    created_at: datetime
    producer_verdict: Literal[None] = None
    performance_conclusion: Literal["not_measured"] = "not_measured"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_receipt_identity(self) -> M2FormalPhaseExecutionReceipt:
        if self.receipt_id != m2_formal_execution_receipt_id_for(self.execution.execution_id):
            raise ValueError("Formal execution Receipt identity differs from execution")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Formal execution Receipt time must be timezone-aware")
        return self


def m2_formal_execution_receipt_hash(
    receipt: M2FormalPhaseExecutionReceipt,
) -> str:
    encoded = (
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class M2FormalPhaseExecutionReceiptRef(FrozenFormalExecutionModel):
    schema_version: Literal["m2a-formal-phase-execution-receipt-ref-v1"] = (
        "m2a-formal-phase-execution-receipt-ref-v1"
    )
    receipt_id: UUID
    execution_id: UUID
    round_id: UUID
    candidate_id: UUID
    phase: RoundPhase
    uri: str = Field(min_length=1, max_length=4000)
    content_hash: str = Field(pattern=SHA256_PATTERN)


__all__ = [
    "M2_FORMAL_EXECUTION_CONTRACT_VERSION",
    "M2FormalExecutionAdapterProfile",
    "M2FormalExecutionAdapterProfileContent",
    "M2FormalExecutionBinding",
    "M2FormalExecutionStatus",
    "M2FormalExecutionWindow",
    "M2FormalTargetLockRefreshContent",
    "M2FormalTargetLockRefreshReport",
    "M2FormalPhaseExecutionReceipt",
    "M2FormalPhaseExecutionReceiptRef",
    "M2FormalPhaseExecutionRecord",
    "M2FormalPhaseExecutionRequest",
    "m2_formal_execution_id_for",
    "m2_formal_execution_adapter_profile_hash",
    "m2_formal_execution_receipt_hash",
    "m2_formal_execution_receipt_id_for",
    "m2_formal_target_lock_refresh_hash",
    "m2_formal_phase_execution_request_hash",
    "publish_m2_formal_target_lock_refresh",
    "publish_m2_formal_execution_adapter_profile",
]
