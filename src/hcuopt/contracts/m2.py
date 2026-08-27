# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import (
    ManualCandidateKind,
    ProjectMode,
    RoundBudgetEntryType,
    RoundBudgetReservationState,
    RoundCandidateState,
    RoundPhase,
    SearchRoundRunMode,
    SearchRoundState,
)

M2_SEARCH_ROUND_SCHEMA_VERSION = "m2a-search-round-v1"


class CandidateSourcePackageRef(ContractModel):
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    source_package_hash: str = Field(pattern=SHA256_PATTERN)
    manifest_hash: str = Field(pattern=SHA256_PATTERN)
    manifest_schema_version: Literal["m1-candidate-source-v1"]


class ScriptedCandidatePackageInput(ContractModel):
    ordinal: int = Field(ge=0, le=3)
    source_package_ref: CandidateSourcePackageRef
    optimization_intent: str = Field(min_length=1, max_length=2000)


class ScriptedCandidateFixtureSpec(ContractModel):
    fixture_id: Literal["noop", "known_faster", "known_slower", "build_failure"]
    build_outcome: Literal["built", "build_failed"]
    optimization_intent: str = Field(min_length=1, max_length=2000)
    terminal_failure_code: str | None = Field(default=None, min_length=1, max_length=200)
    synthetic: Literal[True] = True
    performance_conclusion: Literal["not_measured"] = "not_measured"

    @model_validator(mode="after")
    def bind_failure_to_build_outcome(self) -> ScriptedCandidateFixtureSpec:
        if (self.build_outcome == "build_failed") != (
            self.terminal_failure_code is not None
        ):
            raise ValueError("build failure Fixture requires one terminal failure code")
        return self


class RoundBudget(ContractModel):
    max_candidates: int = Field(ge=2, le=4)
    max_build_attempts: int = Field(ge=1, le=100)
    max_correctness_attempts: int = Field(ge=1, le=100)
    max_search_samples: int = Field(ge=1, le=10_000_000)
    max_holdout_samples: int = Field(ge=1, le=10_000_000)
    max_wall_seconds: int = Field(ge=1, le=604_800)
    max_exclusive_lease_seconds: int = Field(ge=1, le=604_800)


class BudgetUsage(ContractModel):
    candidates: int = Field(default=0, ge=0)
    build_attempts: int = Field(default=0, ge=0)
    correctness_attempts: int = Field(default=0, ge=0)
    search_samples: int = Field(default=0, ge=0)
    holdout_samples: int = Field(default=0, ge=0)
    wall_seconds: float = Field(default=0.0, ge=0)
    exclusive_lease_seconds: float = Field(default=0.0, ge=0)

    def is_zero(self) -> bool:
        return all(value == 0 for value in self.model_dump().values())

    def plus(self, other: BudgetUsage) -> BudgetUsage:
        return BudgetUsage.model_validate(
            {
                name: getattr(self, name) + getattr(other, name)
                for name in type(self).model_fields
            }
        )


class SearchRound(ContractModel):
    schema_version: Literal["m2a-search-round-v1"] = M2_SEARCH_ROUND_SCHEMA_VERSION
    round_id: UUID
    task_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=300)
    state: SearchRoundState
    run_mode: SearchRoundRunMode
    project_mode: ProjectMode | None = None
    target_snapshot_id: UUID
    stage0_run_id: UUID
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_epoch_id: UUID
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    configuration_hash: str = Field(pattern=SHA256_PATTERN)
    image_digest: str = Field(pattern=SHA256_PATTERN)
    adapter_profile: str = Field(min_length=1, max_length=200)
    declared_candidate_count: int = Field(ge=2, le=4)
    max_promoted: int = Field(ge=1, le=2)
    family_alpha: float = Field(gt=0.0, lt=1.0)
    search_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_commitment: str = Field(pattern=SHA256_PATTERN)
    holdout_commitment_scheme: Literal["sha256-nonce-v1"] = "sha256-nonce-v1"
    holdout_plan_authority_id: str = Field(min_length=1, max_length=300)
    holdout_plan_authority_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    holdout_reveal_lease_id: UUID | None = None
    holdout_reveal_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    budget: RoundBudget
    candidate_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    automatic_release_allowed: Literal[False] = False
    version: int = Field(ge=1)
    created_at: datetime
    intake_closed_at: datetime | None = None

    @model_validator(mode="after")
    def require_safe_mode_and_limits(self) -> SearchRound:
        if self.run_mode is SearchRoundRunMode.SCRIPTED and self.project_mode is not None:
            raise ValueError("scripted Round project_mode must be null")
        if self.run_mode is SearchRoundRunMode.SCRIPTED and self.state in {
            SearchRoundState.AWAITING_SIGNOFF,
            SearchRoundState.COMPLETED,
            SearchRoundState.REJECTED,
        }:
            raise ValueError("scripted Round cannot enter Formal terminal/signoff states")
        if self.run_mode is SearchRoundRunMode.FORMAL and (
            self.project_mode is not ProjectMode.DEGRADED_MANUAL_INTAKE
        ):
            raise ValueError("formal M2a Round requires degraded_manual_intake")
        if (
            self.run_mode is SearchRoundRunMode.FORMAL
            and self.state is SearchRoundState.SCRIPTED_COMPLETED
        ):
            raise ValueError("formal Round cannot enter scripted_completed")
        if self.max_promoted > self.declared_candidate_count:
            raise ValueError("max_promoted cannot exceed declared_candidate_count")
        if self.budget.max_candidates != self.declared_candidate_count:
            raise ValueError("budget max_candidates must equal declared_candidate_count")
        reveal_values = (
            self.holdout_plan_hash,
            self.holdout_reveal_lease_id,
            self.holdout_reveal_evidence_hash,
        )
        if any(value is not None for value in reveal_values) and not all(
            value is not None for value in reveal_values
        ):
            raise ValueError("Holdout reveal hash, Lease, and evidence must be written together")
        if any(value is not None for value in reveal_values) and self.holdout_family_hash is None:
            raise ValueError("Holdout reveal requires a frozen holdout_family_hash")
        return self


class SearchRoundView(SearchRound):
    model_config = ConfigDict(extra="ignore")

    updated_at: datetime


class RoundCandidate(ContractModel):
    round_candidate_id: UUID
    round_id: UUID
    candidate_id: UUID
    ordinal: int = Field(ge=0, le=3)
    source_package_store_id: str = Field(min_length=1, max_length=300)
    source_package_store_hash: str = Field(pattern=SHA256_PATTERN)
    source_package_hash: str = Field(pattern=SHA256_PATTERN)
    source_manifest_version: Literal["m1-candidate-source-v1"] = "m1-candidate-source-v1"
    source_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    optimization_intent: str = Field(min_length=1, max_length=2000)
    replacement_point: str = Field(min_length=1, max_length=1000)
    track: Literal["triton"] = "triton"
    release_mode: Literal["overlay"] = "overlay"
    candidate_kind: ManualCandidateKind
    artifact_id: UUID | None = None
    artifact_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    terminal_failure_code: str | None = Field(default=None, min_length=1, max_length=200)
    failure_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    state: RoundCandidateState
    idempotency_key: str = Field(min_length=8, max_length=300)

    @model_validator(mode="after")
    def require_artifact_or_failure_pairs(self) -> RoundCandidate:
        if (self.artifact_id is None) != (self.artifact_hash is None):
            raise ValueError("artifact_id and artifact_hash must be written together")
        if (self.terminal_failure_code is None) != (self.failure_evidence_hash is None):
            raise ValueError("failure code and failure evidence must be written together")
        if self.artifact_id is not None and self.terminal_failure_code is not None:
            raise ValueError("Candidate cannot bind an Artifact and terminal failure together")
        if self.baseline_source_hash == self.candidate_source_hash:
            raise ValueError("M2 Candidate source must differ from its Baseline")
        return self


class RoundCandidateView(RoundCandidate):
    model_config = ConfigDict(extra="ignore")

    created_at: datetime
    updated_at: datetime


class RoundCandidateBuildTerminal(ContractModel):
    round_id: UUID
    round_candidate_id: UUID
    candidate_id: UUID
    state: RoundCandidateState
    artifact_id: UUID | None = None
    artifact_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    terminal_failure_code: str | None = Field(default=None, min_length=1, max_length=200)
    failure_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_one_build_terminal(self) -> RoundCandidateBuildTerminal:
        has_artifact = self.artifact_id is not None and self.artifact_hash is not None
        has_failure = (
            self.terminal_failure_code is not None
            and self.failure_evidence_hash is not None
        )
        if (self.artifact_id is None) != (self.artifact_hash is None) or (
            self.terminal_failure_code is None
        ) != (self.failure_evidence_hash is None):
            raise ValueError("Build terminal identities must be written in pairs")
        if has_artifact == has_failure:
            raise ValueError("Build terminal must bind exactly one Artifact or failure")
        if has_artifact and self.state is not RoundCandidateState.BUILT:
            raise ValueError("Artifact Build terminal must be built")
        if has_failure and self.state not in {
            RoundCandidateState.BUILD_FAILED,
            RoundCandidateState.INVALID,
        }:
            raise ValueError("failure Build terminal must be build_failed or invalid")
        return self


class ArtifactFamilyFreezeRequest(ContractModel):
    round_id: UUID
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    expected_artifact_family_hash: str = Field(pattern=SHA256_PATTERN)


class RoundBudgetReservation(ContractModel):
    reservation_id: UUID
    round_id: UUID
    job_id: UUID
    attempt: int = Field(ge=1)
    candidate_id: UUID | None = None
    phase: RoundPhase | None = None
    planned: BudgetUsage
    state: RoundBudgetReservationState
    idempotency_key: str = Field(min_length=8, max_length=300)


class RoundBudgetReservationView(RoundBudgetReservation):
    model_config = ConfigDict(extra="ignore")

    created_at: datetime
    updated_at: datetime


class RoundBudgetLedgerEntry(ContractModel):
    ledger_entry_id: UUID
    reservation_id: UUID
    round_id: UUID
    entry_type: RoundBudgetEntryType
    reserved: BudgetUsage
    actual: BudgetUsage
    lease_held_seconds: float = Field(ge=0)
    harness_active_seconds: float = Field(ge=0)
    raw_usage_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    created_at: datetime

    @model_validator(mode="after")
    def require_valid_ledger_semantics(self) -> RoundBudgetLedgerEntry:
        if self.harness_active_seconds > self.lease_held_seconds:
            raise ValueError("Harness active time cannot exceed Lease held time")
        if self.entry_type is RoundBudgetEntryType.SETTLE and not math.isclose(
            self.actual.exclusive_lease_seconds,
            self.lease_held_seconds,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("settle actual Lease charge must equal Lease held time")
        if self.entry_type is RoundBudgetEntryType.RELEASE and not self.actual.is_zero():
            raise ValueError("release entry cannot report actual consumption")
        if self.entry_type is RoundBudgetEntryType.RESERVE and not self.actual.is_zero():
            raise ValueError("reserve entry cannot report actual consumption")
        if self.entry_type is not RoundBudgetEntryType.SETTLE and (
            self.lease_held_seconds != 0 or self.harness_active_seconds != 0
        ):
            raise ValueError("reserve/release entries cannot report runtime seconds")
        return self


class RoundBudgetReserveRequest(ContractModel):
    reservation: RoundBudgetReservation
    ledger_entry: RoundBudgetLedgerEntry

    @model_validator(mode="after")
    def require_matching_reserve_pair(self) -> RoundBudgetReserveRequest:
        if self.ledger_entry.entry_type is not RoundBudgetEntryType.RESERVE:
            raise ValueError("Budget reserve request requires a reserve ledger entry")
        if (
            self.reservation.reservation_id != self.ledger_entry.reservation_id
            or self.reservation.round_id != self.ledger_entry.round_id
            or self.reservation.planned != self.ledger_entry.reserved
        ):
            raise ValueError("Budget reservation and reserve ledger entry must match")
        return self


class RoundBudgetFinalizeRequest(ContractModel):
    ledger_entry: RoundBudgetLedgerEntry

    @model_validator(mode="after")
    def require_terminal_entry(self) -> RoundBudgetFinalizeRequest:
        if self.ledger_entry.entry_type not in {
            RoundBudgetEntryType.SETTLE,
            RoundBudgetEntryType.RELEASE,
        }:
            raise ValueError("Budget finalize request requires settle or release")
        return self


class RoundBudgetMutationResult(ContractModel):
    reservation: RoundBudgetReservationView
    ledger_entry: RoundBudgetLedgerEntry


class SearchRoundSummary(ContractModel):
    round: SearchRoundView
    candidates: list[RoundCandidateView]
    budget_reservations: list[RoundBudgetReservationView]
    budget_ledger: list[RoundBudgetLedgerEntry]
    barriers: list[dict] = Field(default_factory=list)
    holdout_reveal: dict | None = None
    multiple_comparison: dict | None = None
    evidence_bundle: dict | None = None
    automatic_release_allowed: Literal[False] = False
