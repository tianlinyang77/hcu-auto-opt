# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import (
    ManualCandidateVerdict,
    RoundBarrierOutcome,
    RoundCandidateState,
    RoundPhase,
    RoundTerminalReason,
    SearchRoundRunMode,
)

M2_BARRIER_SCHEMA_VERSION = "m2a-round-barrier-v1"
M2_MULTIPLE_COMPARISON_SCHEMA_VERSION = "m2a-bonferroni-fwer-v1"
M2_ROUND_EVIDENCE_SCHEMA_VERSION = "m2a-round-evidence-v1"


class FrozenEvaluationModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class BarrierMemberResult(FrozenEvaluationModel):
    """One frozen family member at a Search or Holdout batch barrier."""

    round_candidate_id: UUID
    candidate_id: UUID
    candidate_state: RoundCandidateState
    artifact_id: UUID | None = None
    artifact_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    correctness_evidence_hash: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    round_measurement_ref_id: UUID | None = None
    failure_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    budget_usage_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    cleanup_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_terminal_evidence_shape(self) -> BarrierMemberResult:
        if (self.artifact_id is None) != (self.artifact_hash is None):
            raise ValueError("Barrier member Artifact identity must be written as one pair")
        measured = self.candidate_state in {
            RoundCandidateState.SEARCH_MEASURED,
            RoundCandidateState.HOLDOUT_MEASURED,
        }
        failed = self.candidate_state in {
            RoundCandidateState.BUILD_FAILED,
            RoundCandidateState.CORRECTNESS_FAILED,
            RoundCandidateState.SEARCH_FAILED,
            RoundCandidateState.HOLDOUT_FAILED,
            RoundCandidateState.INVALID,
        }
        if not measured and not failed:
            raise ValueError("Barrier member must be in a measurement or failure terminal")
        if measured and (
            self.artifact_id is None
            or self.correctness_evidence_hash is None
            or self.round_measurement_ref_id is None
            or self.cleanup_evidence_hash is None
            or self.failure_evidence_hash is not None
        ):
            raise ValueError("measured Barrier member requires complete success evidence")
        if failed and (
            self.failure_evidence_hash is None
            or self.round_measurement_ref_id is not None
        ):
            raise ValueError("failed Barrier member requires failure evidence and no Ref")
        return self


class RoundBarrierResult(FrozenEvaluationModel):
    schema_version: Literal["m2a-round-barrier-v1"] = M2_BARRIER_SCHEMA_VERSION
    barrier_id: UUID
    round_id: UUID
    phase: RoundPhase
    input_family_hash: str = Field(pattern=SHA256_PATTERN)
    expected_member_count: int = Field(ge=1, le=4)
    members: tuple[BarrierMemberResult, ...] = Field(min_length=1, max_length=4)
    rule_version: str = Field(min_length=1, max_length=200)
    rule_hash: str = Field(pattern=SHA256_PATTERN)
    promoted_candidate_ids: tuple[UUID, ...] = Field(default=(), max_length=2)
    outcome: RoundBarrierOutcome
    input_summary_hash: str = Field(pattern=SHA256_PATTERN)
    closed_by: str = Field(min_length=1, max_length=300)
    closed_at: datetime
    idempotency_key: str = Field(min_length=8, max_length=300)

    @field_validator("members", "promoted_candidate_ids", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_complete_phase_barrier(self) -> RoundBarrierResult:
        if self.closed_at.tzinfo is None or self.closed_at.utcoffset() is None:
            raise ValueError("Barrier close time must be timezone-aware")
        if len(self.members) != self.expected_member_count:
            raise ValueError("Barrier requires every frozen family member")
        candidate_ids = tuple(item.candidate_id for item in self.members)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("Barrier members must bind unique Candidates")
        if candidate_ids != tuple(sorted(candidate_ids, key=str)):
            raise ValueError("Barrier members must use deterministic Candidate order")
        if len(set(self.promoted_candidate_ids)) != len(self.promoted_candidate_ids):
            raise ValueError("Barrier promoted Candidate IDs must be unique")
        if self.promoted_candidate_ids != tuple(
            sorted(self.promoted_candidate_ids, key=str)
        ):
            raise ValueError("Barrier promotions must use deterministic Candidate order")
        members_by_id = {item.candidate_id: item for item in self.members}
        if any(candidate_id not in members_by_id for candidate_id in self.promoted_candidate_ids):
            raise ValueError("Barrier cannot promote a Candidate outside the frozen family")

        if self.phase is RoundPhase.SEARCH:
            allowed = {
                RoundCandidateState.BUILD_FAILED,
                RoundCandidateState.CORRECTNESS_FAILED,
                RoundCandidateState.SEARCH_FAILED,
                RoundCandidateState.SEARCH_MEASURED,
                RoundCandidateState.INVALID,
            }
            if any(item.candidate_state not in allowed for item in self.members):
                raise ValueError("Search Barrier contains a non-Search terminal")
            expected_outcome = (
                RoundBarrierOutcome.MEMBERS_PROMOTED
                if self.promoted_candidate_ids
                else RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
            )
            if self.outcome is not expected_outcome:
                raise ValueError("Search Barrier outcome does not match promotions")
            if any(
                members_by_id[candidate_id].candidate_state
                is not RoundCandidateState.SEARCH_MEASURED
                for candidate_id in self.promoted_candidate_ids
            ):
                raise ValueError("Search Barrier can promote only measured Candidates")
        else:
            allowed = {
                RoundCandidateState.HOLDOUT_FAILED,
                RoundCandidateState.HOLDOUT_MEASURED,
                RoundCandidateState.INVALID,
            }
            if any(item.candidate_state not in allowed for item in self.members):
                raise ValueError("Holdout Barrier contains a non-Holdout terminal")
            if (
                self.outcome is not RoundBarrierOutcome.COMPLETED
                or self.promoted_candidate_ids
            ):
                raise ValueError("Holdout Barrier must complete without promotions")
        return self


class AdjustedCandidateResult(FrozenEvaluationModel):
    candidate_id: UUID
    round_measurement_ref_id: UUID | None = None
    correctness_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    verdict: ManualCandidateVerdict
    adjusted_ci_lower: float | None = None
    adjusted_ci_upper: float | None = None
    stage0_mde_ratio: float | None = Field(default=None, gt=0, lt=1)
    workload_mde_ratio: float | None = Field(default=None, ge=0)
    credible_threshold: float | None = Field(default=None, gt=0)
    failure_codes: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("failure_codes", mode="before")
    @classmethod
    def freeze_failure_codes(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_adjusted_verdict_evidence(self) -> AdjustedCandidateResult:
        values = (
            self.adjusted_ci_lower,
            self.adjusted_ci_upper,
            self.stage0_mde_ratio,
            self.workload_mde_ratio,
            self.credible_threshold,
        )
        if self.verdict is ManualCandidateVerdict.INVALID:
            if not self.failure_codes:
                raise ValueError("invalid adjusted result requires failure codes")
            if any(value is not None for value in values):
                raise ValueError("invalid adjusted result cannot claim a confidence interval")
            return self
        if self.round_measurement_ref_id is None or any(
            value is None for value in values
        ):
            raise ValueError("adjusted conclusion requires complete verified evidence")
        assert self.adjusted_ci_lower is not None
        assert self.adjusted_ci_upper is not None
        assert self.stage0_mde_ratio is not None
        assert self.workload_mde_ratio is not None
        assert self.credible_threshold is not None
        if self.adjusted_ci_lower > self.adjusted_ci_upper:
            raise ValueError("adjusted confidence interval is not ordered")
        if not math.isclose(
            self.credible_threshold,
            max(self.stage0_mde_ratio, self.workload_mde_ratio),
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("credible threshold must use the conservative MDE")
        if self.failure_codes:
            raise ValueError("valid adjusted conclusion cannot carry failure codes")
        if self.verdict is ManualCandidateVerdict.FASTER:
            valid = self.adjusted_ci_lower > self.credible_threshold
        elif self.verdict is ManualCandidateVerdict.SLOWER:
            valid = self.adjusted_ci_upper < -self.credible_threshold
        else:
            valid = not (
                self.adjusted_ci_lower > self.credible_threshold
                or self.adjusted_ci_upper < -self.credible_threshold
            )
        if not valid:
            raise ValueError("adjusted verdict does not match its confidence interval")
        return self


class MultipleComparisonResult(FrozenEvaluationModel):
    schema_version: Literal["m2a-bonferroni-fwer-v1"] = (
        M2_MULTIPLE_COMPARISON_SCHEMA_VERSION
    )
    multiple_comparison_id: UUID
    round_id: UUID
    holdout_barrier_id: UUID
    holdout_family_hash: str = Field(pattern=SHA256_PATTERN)
    method: Literal["bonferroni_fwer"] = "bonferroni_fwer"
    protocol_version: str = Field(min_length=1, max_length=200)
    protocol_hash: str = Field(pattern=SHA256_PATTERN)
    family_alpha: float = Field(gt=0, lt=1)
    m: int = Field(ge=1, le=2)
    alpha_candidate: float = Field(gt=0, lt=1)
    candidate_results: tuple[AdjustedCandidateResult, ...] = Field(
        min_length=1, max_length=2
    )
    recommended_candidate_id: UUID | None = None
    result_hash: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime

    @field_validator("candidate_results", mode="before")
    @classmethod
    def freeze_results(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_frozen_bonferroni_family(self) -> MultipleComparisonResult:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Multiple Comparison time must be timezone-aware")
        if not math.isclose(
            self.alpha_candidate,
            self.family_alpha / self.m,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("Bonferroni alpha_candidate must equal family_alpha / m")
        if len(self.candidate_results) != self.m:
            raise ValueError("Multiple Comparison must retain every Holdout member")
        candidate_ids = tuple(item.candidate_id for item in self.candidate_results)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("Multiple Comparison Candidates must be unique")
        if candidate_ids != tuple(sorted(candidate_ids, key=str)):
            raise ValueError("Multiple Comparison results must use deterministic order")
        faster = tuple(
            item
            for item in self.candidate_results
            if item.verdict is ManualCandidateVerdict.FASTER
        )
        expected_recommendation = None
        if faster:
            expected_recommendation = min(
                faster,
                key=lambda item: (-float(item.adjusted_ci_lower), str(item.candidate_id)),
            ).candidate_id
        if self.recommended_candidate_id != expected_recommendation:
            raise ValueError("recommendation must use adjusted lower bound and UUID tie-break")
        return self


class RoundCandidateEvidence(FrozenEvaluationModel):
    round_candidate_id: UUID
    candidate_id: UUID
    search_member_state: RoundCandidateState
    search_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_member_state: RoundCandidateState | None = None
    holdout_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    budget_evidence_hashes: tuple[str, ...] = Field(min_length=1, max_length=16)
    cleanup_evidence_hashes: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("budget_evidence_hashes", "cleanup_evidence_hashes", mode="before")
    @classmethod
    def freeze_hashes(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_holdout_pair(self) -> RoundCandidateEvidence:
        if (self.holdout_member_state is None) != (self.holdout_evidence_hash is None):
            raise ValueError("Holdout Candidate evidence must be written as one pair")
        return self


class RoundEvidenceBundle(FrozenEvaluationModel):
    schema_version: Literal["m2a-round-evidence-v1"] = (
        M2_ROUND_EVIDENCE_SCHEMA_VERSION
    )
    round_evidence_bundle_id: UUID
    round_id: UUID
    task_id: UUID
    run_mode: SearchRoundRunMode
    terminal_reason: RoundTerminalReason
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    search_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_commitment: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    holdout_reveal_evidence_hash: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    candidate_evidence: tuple[RoundCandidateEvidence, ...] = Field(
        min_length=1, max_length=4
    )
    search_barrier_id: UUID
    holdout_barrier_id: UUID | None = None
    multiple_comparison_id: UUID | None = None
    budget_ledger_hash: str = Field(pattern=SHA256_PATTERN)
    evidence_index_uri: str = Field(min_length=1, max_length=4000)
    evidence_index_hash: str = Field(pattern=SHA256_PATTERN)
    summary: dict[str, Any] = Field(default_factory=dict)
    synthetic: bool
    automatic_release_allowed: Literal[False] = False
    created_at: datetime

    @field_validator("candidate_evidence", mode="before")
    @classmethod
    def freeze_candidate_evidence(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_terminal_bundle_shape(self) -> RoundEvidenceBundle:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Round Evidence time must be timezone-aware")
        if (self.run_mode is SearchRoundRunMode.SCRIPTED) != self.synthetic:
            raise ValueError("Round Evidence run mode and synthetic flag disagree")
        candidate_ids = tuple(item.candidate_id for item in self.candidate_evidence)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("Round Evidence Candidates must be unique")
        if candidate_ids != tuple(sorted(candidate_ids, key=str)):
            raise ValueError("Round Evidence Candidates must use deterministic order")
        holdout_fields = (
            self.holdout_family_hash,
            self.holdout_plan_hash,
            self.holdout_reveal_evidence_hash,
            self.holdout_barrier_id,
            self.multiple_comparison_id,
        )
        if self.terminal_reason is RoundTerminalReason.NO_PROMOTABLE_CANDIDATE:
            if any(value is not None for value in holdout_fields):
                raise ValueError("zero-promotion Evidence must omit Holdout and FWER")
            if any(
                item.holdout_member_state is not None for item in self.candidate_evidence
            ):
                raise ValueError("zero-promotion Evidence cannot contain Holdout members")
        elif any(value is None for value in holdout_fields):
            raise ValueError("completed Holdout Evidence requires every Holdout and FWER ref")
        return self
