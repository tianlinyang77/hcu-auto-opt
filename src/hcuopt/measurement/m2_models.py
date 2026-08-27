# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.m2 import BudgetUsage
from hcuopt.contracts.platform_v1 import SHA256_PATTERN, AdapterProvenance
from hcuopt.domain.enums import RoundPhase

M2_PHASE_PLAN_SCHEMA_VERSION = "m2a-phase-measurement-plan-v1"
M2_SCRIPTED_PHASE_EVIDENCE_SCHEMA_VERSION = "m2a-scripted-phase-evidence-v1"
M2_SCRIPTED_PHASE_RECEIPT_SCHEMA_VERSION = "m2a-scripted-phase-receipt-v1"
M2_ROUND_MEASUREMENT_REF_SCHEMA_VERSION = "m2a-round-measurement-ref-v1"
M2_PHASE_USAGE_EVIDENCE_SCHEMA_VERSION = "m2a-phase-budget-usage-v1"
M1_KERNEL_PERFORMANCE_EVIDENCE_SCHEMA_VERSION = (
    "m1-kernel-performance-evidence-v1"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FrozenMeasurementModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class M2PhaseBudgetReservationPlan(FrozenMeasurementModel):
    schema_version: Literal["m2a-phase-measurement-plan-v1"] = (
        M2_PHASE_PLAN_SCHEMA_VERSION
    )
    round_id: UUID
    round_candidate_id: UUID
    candidate_id: UUID
    phase: RoundPhase
    phase_plan_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_plan_hash: str = Field(pattern=SHA256_PATTERN)
    expected_sample_count: int = Field(ge=1, le=10_000_000)
    reservation_id: UUID
    job_id: UUID
    attempt: int = Field(ge=1)
    planned: BudgetUsage
    idempotency_key: str = Field(min_length=8, max_length=270)

    @model_validator(mode="after")
    def bind_planned_phase_usage(self) -> M2PhaseBudgetReservationPlan:
        if any(
            value
            for value in (
                self.planned.candidates,
                self.planned.build_attempts,
                self.planned.correctness_attempts,
            )
        ):
            raise ValueError("M2 phase measurement cannot reserve non-measurement usage")
        phase_samples = (
            self.planned.search_samples
            if self.phase is RoundPhase.SEARCH
            else self.planned.holdout_samples
        )
        other_samples = (
            self.planned.holdout_samples
            if self.phase is RoundPhase.SEARCH
            else self.planned.search_samples
        )
        if phase_samples != self.expected_sample_count or other_samples != 0:
            raise ValueError("M2 phase sample reservation must match the frozen plan")
        if self.planned.wall_seconds <= 0 or self.planned.exclusive_lease_seconds <= 0:
            raise ValueError("M2 phase measurement requires wall and exclusive Lease budget")
        return self


class M2PhaseExecutionContext(FrozenMeasurementModel):
    lease_id: UUID
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    lease_acquired_monotonic_ns: int = Field(ge=0)
    job_started_monotonic_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def require_lease_before_job(self) -> M2PhaseExecutionContext:
        if self.job_started_monotonic_ns < self.lease_acquired_monotonic_ns:
            raise ValueError("M2 phase Job cannot start before its exclusive Lease")
        return self


class M2PhaseMeasurementRequest(FrozenMeasurementModel):
    reservation: M2PhaseBudgetReservationPlan
    execution: M2PhaseExecutionContext
    holdout_reveal_evidence_hash: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    harness_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_phase_specific_reveal(self) -> M2PhaseMeasurementRequest:
        if self.reservation.phase is RoundPhase.SEARCH:
            if self.holdout_reveal_evidence_hash is not None:
                raise ValueError("Search measurement cannot consume Holdout reveal evidence")
        elif self.holdout_reveal_evidence_hash is None:
            raise ValueError("Holdout measurement requires reveal evidence")
        return self


class M2ScriptedHarnessResult(FrozenMeasurementModel):
    """Synthetic output of the registered unique Harness used by Scripted M2a."""

    evidence_schema_version: Literal["m2a-scripted-phase-evidence-v1"] = (
        M2_SCRIPTED_PHASE_EVIDENCE_SCHEMA_VERSION
    )
    measurement_id: UUID
    raw_evidence_uri: str = Field(min_length=1, max_length=4000)
    raw_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_plan_hash: str = Field(pattern=SHA256_PATTERN)
    sample_count: int = Field(ge=1, le=10_000_000)
    baseline_sample_set_hash: str = Field(pattern=SHA256_PATTERN)
    process_identity_set_hash: str = Field(pattern=SHA256_PATTERN)
    cache_namespace_set_hash: str = Field(pattern=SHA256_PATTERN)
    cleanup_evidence: dict[str, Any]
    telemetry_attempt_count: int = Field(default=1, ge=1, le=3)
    telemetry_warnings: tuple[str, ...] = Field(default=(), max_length=16)
    adapter_provenance: AdapterProvenance
    producer_verdict: Literal[None] = None
    performance_conclusion: Literal["not_available"] = "not_available"
    synthetic: Literal[True] = True

    @field_validator("telemetry_warnings", mode="before")
    @classmethod
    def freeze_warnings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_scripted_harness_provenance(self) -> M2ScriptedHarnessResult:
        if (
            self.adapter_provenance.capability != "measurement_harness"
            or self.adapter_provenance.implementation_kind != "fake"
        ):
            raise ValueError("Scripted M2a requires explicit fake Harness provenance")
        if any(not item or len(item) > 2000 for item in self.telemetry_warnings):
            raise ValueError("Telemetry warnings must be bounded non-empty text")
        return self


class M2ScriptedPhaseEvidence(FrozenMeasurementModel):
    """Raw synthetic main file independently re-read by the Phase adapter and D."""

    schema_version: Literal["m2a-scripted-phase-evidence-v1"] = (
        M2_SCRIPTED_PHASE_EVIDENCE_SCHEMA_VERSION
    )
    round_id: UUID
    round_candidate_id: UUID
    candidate_id: UUID
    phase: RoundPhase
    phase_plan_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_plan_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_id: UUID
    sample_count: int = Field(ge=1, le=10_000_000)
    baseline_sample_set_hash: str = Field(pattern=SHA256_PATTERN)
    process_identity_set_hash: str = Field(pattern=SHA256_PATTERN)
    cache_namespace_set_hash: str = Field(pattern=SHA256_PATTERN)
    cleanup_evidence: dict[str, Any]
    telemetry_attempt_count: int = Field(default=1, ge=1, le=3)
    telemetry_warnings: tuple[str, ...] = Field(default=(), max_length=16)
    adapter_provenance: AdapterProvenance
    producer_verdict: Literal[None] = None
    performance_conclusion: Literal["not_available"] = "not_available"
    synthetic: Literal[True] = True

    @field_validator("telemetry_warnings", mode="before")
    @classmethod
    def freeze_evidence_warnings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_scripted_evidence_provenance(self) -> M2ScriptedPhaseEvidence:
        if (
            self.adapter_provenance.capability != "measurement_harness"
            or self.adapter_provenance.implementation_kind != "fake"
        ):
            raise ValueError("Scripted raw evidence requires explicit fake Harness provenance")
        if any(not item or len(item) > 2000 for item in self.telemetry_warnings):
            raise ValueError("Telemetry warnings must be bounded non-empty text")
        return self


class M2PhaseEvidenceBinding(FrozenMeasurementModel):
    """Fields shared by formal refs and non-authoritative Scripted receipts."""

    round_id: UUID
    round_candidate_id: UUID
    candidate_id: UUID
    phase: RoundPhase
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_id: UUID
    artifact_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_id: UUID
    evidence_schema_version: Literal["m2a-scripted-phase-evidence-v1"]
    raw_evidence_uri: str = Field(min_length=1, max_length=4000)
    raw_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_plan_hash: str = Field(pattern=SHA256_PATTERN)
    phase_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_reveal_evidence_hash: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    baseline_sample_set_hash: str = Field(pattern=SHA256_PATTERN)
    process_identity_set_hash: str = Field(pattern=SHA256_PATTERN)
    cache_namespace_set_hash: str = Field(pattern=SHA256_PATTERN)
    lease_id: UUID
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)

    @model_validator(mode="after")
    def require_phase_specific_bindings(self) -> M2PhaseEvidenceBinding:
        if self.phase is RoundPhase.SEARCH:
            if (
                self.holdout_family_hash is not None
                or self.holdout_reveal_evidence_hash is not None
            ):
                raise ValueError("Search reference cannot contain Holdout identities")
        elif (
            self.holdout_family_hash is None
            or self.holdout_reveal_evidence_hash is None
        ):
            raise ValueError("Holdout reference requires Family and reveal identities")
        return self


class RoundMeasurementRef(M2PhaseEvidenceBinding):
    """Formal authority over one complete, real M1 comparison evidence file."""

    schema_version: Literal["m2a-round-measurement-ref-v1"] = (
        M2_ROUND_MEASUREMENT_REF_SCHEMA_VERSION
    )
    round_measurement_ref_id: UUID
    evidence_schema_version: Literal["m1-kernel-performance-evidence-v1"] = (
        M1_KERNEL_PERFORMANCE_EVIDENCE_SCHEMA_VERSION
    )
    status: Literal["measured"] = "measured"
    synthetic: Literal[False] = False
    created_at: datetime

    @model_validator(mode="after")
    def require_formal_timestamp(self) -> RoundMeasurementRef:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Round Measurement Ref created_at must be timezone-aware")
        return self


class M2ScriptedPhaseReceipt(M2PhaseEvidenceBinding):
    """Synthetic control-flow receipt; never a formal measurement authority."""

    schema_version: Literal["m2a-scripted-phase-receipt-v1"] = (
        M2_SCRIPTED_PHASE_RECEIPT_SCHEMA_VERSION
    )
    scripted_phase_receipt_id: UUID
    evidence_schema_version: Literal["m2a-scripted-phase-evidence-v1"] = (
        M2_SCRIPTED_PHASE_EVIDENCE_SCHEMA_VERSION
    )
    status: Literal["not_measured"] = "not_measured"
    synthetic: Literal[True] = True
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def require_scripted_timestamp(self) -> M2ScriptedPhaseReceipt:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Scripted Phase Receipt created_at must be timezone-aware")
        return self


def validate_round_measurement_refs(
    references: Iterable[RoundMeasurementRef],
) -> tuple[RoundMeasurementRef, ...]:
    """Validate a formal authority view without inventing measurement or verdict state."""

    received = tuple(references)
    if any(not isinstance(item, RoundMeasurementRef) for item in received):
        raise TypeError("Formal authority accepts only RoundMeasurementRef")
    items = tuple(
        sorted(
            received,
            key=lambda item: (
                str(item.round_id),
                item.phase.value,
                str(item.candidate_id),
            ),
        )
    )
    per_round_families: dict[UUID, tuple[str, str]] = {}
    seen_member_phases: set[tuple[UUID, UUID, RoundPhase]] = set()
    unique_values: dict[str, set[object]] = {
        "round_measurement_ref_id": set(),
        "measurement_id": set(),
        "raw_evidence_uri": set(),
        "raw_evidence_hash": set(),
        "baseline_sample_set_hash": set(),
        "process_identity_set_hash": set(),
        "cache_namespace_set_hash": set(),
    }

    for item in items:
        families = (item.candidate_family_hash, item.artifact_family_hash)
        existing_families = per_round_families.setdefault(item.round_id, families)
        if existing_families != families:
            raise ValueError("Round Measurement Refs bind different frozen families")

        member_phase = (item.round_id, item.candidate_id, item.phase)
        if member_phase in seen_member_phases:
            raise ValueError("Candidate Phase already has a Round Measurement Ref")
        seen_member_phases.add(member_phase)

        for field_name, values in unique_values.items():
            value = getattr(item, field_name)
            if value in values:
                raise ValueError(
                    f"Round Measurement Ref reuses {field_name} across Candidate or Phase"
                )
            values.add(value)
    return items


class M2PhaseUsageEvidence(FrozenMeasurementModel):
    schema_version: Literal["m2a-phase-budget-usage-v1"] = (
        M2_PHASE_USAGE_EVIDENCE_SCHEMA_VERSION
    )
    round_id: UUID
    candidate_id: UUID
    phase: RoundPhase
    reservation_id: UUID
    status: Literal["not_measured", "failed", "released"]
    planned: BudgetUsage
    actual: BudgetUsage
    lease_held_seconds: float = Field(ge=0)
    harness_active_seconds: float = Field(ge=0)
    measurement_id: UUID | None = None
    raw_evidence_uri: str | None = Field(default=None, max_length=4000)
    raw_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    cleanup_evidence: dict[str, Any] | None = None
    telemetry_warnings: tuple[str, ...] = Field(default=(), max_length=16)
    error_code: str | None = Field(default=None, min_length=1, max_length=200)
    producer_verdict: Literal[None] = None
    synthetic: Literal[True] = True

    @field_validator("telemetry_warnings", mode="before")
    @classmethod
    def freeze_usage_warnings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_terminal_usage_shape(self) -> M2PhaseUsageEvidence:
        if self.harness_active_seconds > self.lease_held_seconds:
            raise ValueError("Harness time cannot exceed exclusive Lease time")
        measured_fields = (
            self.measurement_id,
            self.raw_evidence_uri,
            self.raw_evidence_hash,
            self.cleanup_evidence,
        )
        if self.status == "not_measured":
            if any(value is None for value in measured_fields) or self.error_code is not None:
                raise ValueError(
                    "not_measured Scripted usage requires complete synthetic evidence "
                    "and no error"
                )
        elif self.status == "failed":
            if self.error_code is None:
                raise ValueError("failed usage requires an error code")
        else:
            if not self.actual.is_zero() or any(value is not None for value in measured_fields):
                raise ValueError("released usage cannot report execution or measurement")
            if self.lease_held_seconds != 0 or self.harness_active_seconds != 0:
                raise ValueError("released usage cannot report runtime")
        return self
