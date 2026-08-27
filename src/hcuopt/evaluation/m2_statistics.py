# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterable
from datetime import datetime
from statistics import NormalDist, fmean, stdev
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.m2 import SearchRound
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import (
    ManualCandidateVerdict,
    RoundBarrierOutcome,
    RoundCandidateState,
    RoundPhase,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.evaluation.m2_models import (
    AdjustedCandidateResult,
    BarrierMemberResult,
    FrozenEvaluationModel,
    MultipleComparisonResult,
    RoundBarrierResult,
)
from hcuopt.measurement.evidence import canonical_json_bytes

M2_SEARCH_SELECTION_RULE_VERSION = "m2-scripted-search-mean-v1"
M2_HOLDOUT_BARRIER_RULE_VERSION = "m2-scripted-holdout-completeness-v1"
M2_FWER_PROTOCOL_VERSION = "m2-bonferroni-bootstrap-v1"
M2_FWER_BOOTSTRAP_RESAMPLES = 2_000
M2_FWER_POWER = 0.8


class M2EvaluationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScriptedCandidateStatisticsInput(FrozenEvaluationModel):
    """Synthetic D input; never a formal performance-evidence reference."""

    candidate_id: UUID
    scripted_phase_receipt_id: UUID
    correctness_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    raw_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_sample_set_hash: str = Field(pattern=SHA256_PATTERN)
    restart_effects: tuple[float, ...] = Field(min_length=4, max_length=1_000)
    baseline_restart_means_ns: tuple[float, ...] = Field(min_length=4, max_length=1_000)
    stage0_mde_ratio: float = Field(gt=0, lt=1)
    bindings_valid: bool = True
    correctness_passed: bool = True
    cleanup_healthy: bool = True
    failure_codes: tuple[str, ...] = Field(default=(), max_length=32)
    synthetic: Literal[True] = True

    @field_validator("restart_effects", "baseline_restart_means_ns", "failure_codes", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_valid_statistics_shape(self) -> ScriptedCandidateStatisticsInput:
        if len(self.restart_effects) != len(self.baseline_restart_means_ns):
            raise ValueError("restart effects and Baseline restart means must align")
        if any(value <= 0 for value in self.baseline_restart_means_ns):
            raise ValueError("Baseline restart means must be positive")
        valid = self.bindings_valid and self.correctness_passed and self.cleanup_healthy
        if valid == bool(self.failure_codes):
            raise ValueError("statistics validity and failure codes disagree")
        return self


class SearchBarrierDecision(FrozenEvaluationModel):
    barrier: RoundBarrierResult
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def bind_conditional_holdout_family(self) -> SearchBarrierDecision:
        promoted = bool(self.barrier.promoted_candidate_ids)
        if promoted != (self.holdout_family_hash is not None):
            raise ValueError("Holdout Family must exist exactly when Search promotes members")
        return self


def close_scripted_search_barrier(
    *,
    round_authority: SearchRound,
    expected_candidate_ids: Iterable[UUID],
    members: Iterable[BarrierMemberResult],
    statistics: Iterable[ScriptedCandidateStatisticsInput],
    closed_by: str,
    closed_at: datetime,
    idempotency_key: str,
) -> SearchBarrierDecision:
    _require_scripted_round(round_authority, SearchRoundState.SEARCH_BARRIER)
    if (
        round_authority.candidate_family_hash is None
        or round_authority.artifact_family_hash is None
    ):
        raise M2EvaluationError(
            "artifact_family_not_frozen",
            "Search Barrier requires Candidate and Artifact Families",
        )
    expected = _expected_candidate_family(
        expected_candidate_ids,
        maximum=round_authority.declared_candidate_count,
        error_code="search_candidate_family_invalid",
    )
    if len(expected) != round_authority.declared_candidate_count:
        raise M2EvaluationError(
            "barrier_member_count_mismatch",
            "Search Barrier must retain every declared Candidate",
        )
    member_items = _ordered_members(members)
    if tuple(item.candidate_id for item in member_items) != expected:
        raise M2EvaluationError(
            "search_candidate_family_mismatch",
            "Search Barrier members differ from the frozen Candidate Family",
        )
    if any(not item.synthetic for item in member_items):
        raise M2EvaluationError(
            "barrier_mode_mismatch", "Scripted Barrier accepts only synthetic members"
        )
    stats = _statistics_by_candidate(statistics)
    measured_ids = {
        item.candidate_id
        for item in member_items
        if item.candidate_state is RoundCandidateState.SEARCH_MEASURED
    }
    if set(stats) != measured_ids:
        raise M2EvaluationError(
            "search_statistics_family_mismatch",
            "Search statistics must cover exactly the measured members",
        )
    _bind_statistics_to_members(stats, member_items)
    _reject_reused_statistics_identities(stats.values())
    eligible = [
        (candidate_id, fmean(item.restart_effects))
        for candidate_id, item in stats.items()
        if _statistics_valid(item) and fmean(item.restart_effects) > 0
    ]
    selected = tuple(
        candidate_id
        for candidate_id, _score in sorted(eligible, key=lambda item: (-item[1], str(item[0])))[
            : round_authority.max_promoted
        ]
    )
    promoted = tuple(sorted(selected, key=str))
    input_payload = {
        "round_id": str(round_authority.round_id),
        "artifact_family_hash": round_authority.artifact_family_hash,
        "selection_rule_hash": round_authority.selection_rule_hash,
        "members": [item.model_dump(mode="json") for item in member_items],
        "statistics": [stats[key].model_dump(mode="json") for key in sorted(stats, key=str)],
        "promoted_candidate_ids": [str(item) for item in promoted],
    }
    input_hash = _digest(input_payload)
    barrier = RoundBarrierResult(
        barrier_id=uuid5(NAMESPACE_URL, f"hcuopt:m2-search-barrier:{input_hash}"),
        round_id=round_authority.round_id,
        run_mode=SearchRoundRunMode.SCRIPTED,
        synthetic=True,
        phase=RoundPhase.SEARCH,
        input_family_hash=round_authority.artifact_family_hash,
        expected_member_count=len(member_items),
        members=member_items,
        rule_version=M2_SEARCH_SELECTION_RULE_VERSION,
        rule_hash=round_authority.selection_rule_hash,
        promoted_candidate_ids=promoted,
        outcome=(
            RoundBarrierOutcome.MEMBERS_PROMOTED
            if promoted
            else RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
        ),
        input_summary_hash=input_hash,
        closed_by=closed_by,
        closed_at=closed_at,
        idempotency_key=idempotency_key,
    )
    by_id = {item.candidate_id: item for item in member_items}
    holdout_family_hash = None
    if promoted:
        holdout_family_hash = _holdout_family_hash(
            round_authority=round_authority,
            members=tuple(by_id[candidate_id] for candidate_id in promoted),
        )
    return SearchBarrierDecision(barrier=barrier, holdout_family_hash=holdout_family_hash)


def close_scripted_holdout_barrier(
    *,
    round_authority: SearchRound,
    expected_candidate_ids: Iterable[UUID],
    members: Iterable[BarrierMemberResult],
    closed_by: str,
    closed_at: datetime,
    idempotency_key: str,
) -> RoundBarrierResult:
    _require_scripted_round(round_authority, SearchRoundState.HOLDOUT_BARRIER)
    if (
        round_authority.holdout_family_hash is None
        or round_authority.holdout_plan_hash is None
        or round_authority.holdout_reveal_evidence_hash is None
    ):
        raise M2EvaluationError(
            "holdout_plan_not_revealed",
            "Holdout Barrier requires a revealed Plan and frozen Family",
        )
    expected = _expected_candidate_family(
        expected_candidate_ids,
        maximum=round_authority.max_promoted,
        error_code="holdout_family_invalid",
    )
    member_items = _ordered_members(members)
    if tuple(item.candidate_id for item in member_items) != expected:
        raise M2EvaluationError(
            "holdout_family_mismatch",
            "Holdout Barrier must retain every frozen promoted Candidate",
        )
    recomputed_family_hash = _holdout_family_hash(
        round_authority=round_authority,
        members=member_items,
    )
    if recomputed_family_hash != round_authority.holdout_family_hash:
        raise M2EvaluationError(
            "holdout_family_hash_mismatch",
            "Holdout members or Artifacts drifted from the frozen Family",
        )
    input_hash = _digest(
        {
            "round_id": str(round_authority.round_id),
            "holdout_family_hash": round_authority.holdout_family_hash,
            "holdout_plan_hash": round_authority.holdout_plan_hash,
            "holdout_reveal_evidence_hash": round_authority.holdout_reveal_evidence_hash,
            "members": [item.model_dump(mode="json") for item in member_items],
        }
    )
    return RoundBarrierResult(
        barrier_id=uuid5(NAMESPACE_URL, f"hcuopt:m2-holdout-barrier:{input_hash}"),
        round_id=round_authority.round_id,
        run_mode=SearchRoundRunMode.SCRIPTED,
        synthetic=True,
        phase=RoundPhase.HOLDOUT,
        input_family_hash=round_authority.holdout_family_hash,
        expected_member_count=len(expected),
        members=member_items,
        rule_version=M2_HOLDOUT_BARRIER_RULE_VERSION,
        rule_hash=_digest(
            {
                "rule_version": M2_HOLDOUT_BARRIER_RULE_VERSION,
                "holdout_plan_hash": round_authority.holdout_plan_hash,
            }
        ),
        outcome=RoundBarrierOutcome.COMPLETED,
        input_summary_hash=input_hash,
        closed_by=closed_by,
        closed_at=closed_at,
        idempotency_key=idempotency_key,
    )


def bonferroni_fwer(
    *,
    round_authority: SearchRound,
    holdout_barrier: RoundBarrierResult,
    statistics: Iterable[ScriptedCandidateStatisticsInput],
    created_at: datetime,
) -> MultipleComparisonResult:
    _require_scripted_round(round_authority, SearchRoundState.HOLDOUT_BARRIER)
    if (
        holdout_barrier.round_id != round_authority.round_id
        or holdout_barrier.phase is not RoundPhase.HOLDOUT
        or holdout_barrier.outcome is not RoundBarrierOutcome.COMPLETED
        or not holdout_barrier.synthetic
        or holdout_barrier.input_family_hash != round_authority.holdout_family_hash
    ):
        raise M2EvaluationError(
            "holdout_barrier_binding_mismatch",
            "FWER input is not the closed Holdout Barrier for this Round",
        )
    m = holdout_barrier.expected_member_count
    if not 1 <= m <= round_authority.max_promoted:
        raise M2EvaluationError("holdout_m_invalid", "Holdout m must stay frozen at 1..2")
    stats = _statistics_by_candidate(statistics)
    member_by_id = {item.candidate_id: item for item in holdout_barrier.members}
    measured_ids = {
        item.candidate_id
        for item in holdout_barrier.members
        if item.candidate_state is RoundCandidateState.HOLDOUT_MEASURED
    }
    if set(stats) != measured_ids:
        raise M2EvaluationError(
            "holdout_statistics_family_mismatch",
            "Holdout statistics must cover exactly the measured members",
        )
    _bind_statistics_to_members(stats, holdout_barrier.members)
    _reject_reused_statistics_identities(stats.values())
    alpha_candidate = round_authority.family_alpha / m
    adjusted: list[AdjustedCandidateResult] = []
    for candidate_id in sorted(member_by_id, key=str):
        member = member_by_id[candidate_id]
        item = stats.get(candidate_id)
        if member.candidate_state is not RoundCandidateState.HOLDOUT_MEASURED:
            if item is not None:
                raise M2EvaluationError(
                    "holdout_statistics_for_failed_member",
                    "failed Holdout member cannot carry successful statistics",
                )
            if member.correctness_evidence_hash is None:
                raise M2EvaluationError(
                    "holdout_failure_correctness_missing",
                    "failed Holdout member must retain correctness evidence",
                )
            adjusted.append(
                AdjustedCandidateResult(
                    candidate_id=candidate_id,
                    synthetic=True,
                    correctness_evidence_hash=member.correctness_evidence_hash,
                    verdict=ManualCandidateVerdict.INVALID,
                    failure_codes=("holdout_member_failed",),
                )
            )
            continue
        if item is None:
            raise M2EvaluationError(
                "holdout_statistics_family_mismatch",
                "measured Holdout member requires its own statistics",
            )
        if not _statistics_valid(item):
            adjusted.append(
                AdjustedCandidateResult(
                    candidate_id=candidate_id,
                    scripted_phase_receipt_id=item.scripted_phase_receipt_id,
                    synthetic=True,
                    correctness_evidence_hash=item.correctness_evidence_hash,
                    verdict=ManualCandidateVerdict.INVALID,
                    failure_codes=item.failure_codes,
                )
            )
            continue
        lower, upper = _bootstrap_mean_ci(
            item.restart_effects,
            seed_material=(
                f"{item.raw_evidence_hash}:{round_authority.holdout_family_hash}:{alpha_candidate}"
            ),
            iterations=M2_FWER_BOOTSTRAP_RESAMPLES,
            confidence=1.0 - alpha_candidate,
        )
        workload_mde = _workload_baseline_mde(
            item.baseline_restart_means_ns,
            alpha=alpha_candidate,
            power=M2_FWER_POWER,
        )
        threshold = max(item.stage0_mde_ratio, workload_mde)
        if lower > threshold:
            verdict = ManualCandidateVerdict.FASTER
        elif upper < -threshold:
            verdict = ManualCandidateVerdict.SLOWER
        else:
            verdict = ManualCandidateVerdict.INCONCLUSIVE
        adjusted.append(
            AdjustedCandidateResult(
                candidate_id=candidate_id,
                scripted_phase_receipt_id=item.scripted_phase_receipt_id,
                synthetic=True,
                correctness_evidence_hash=item.correctness_evidence_hash,
                verdict=verdict,
                adjusted_ci_lower=lower,
                adjusted_ci_upper=upper,
                stage0_mde_ratio=item.stage0_mde_ratio,
                workload_mde_ratio=workload_mde,
                credible_threshold=threshold,
            )
        )
    adjusted_items = tuple(adjusted)
    faster = tuple(item for item in adjusted_items if item.verdict is ManualCandidateVerdict.FASTER)
    recommended = None
    if faster:
        recommended = min(
            faster,
            key=lambda item: (-float(item.adjusted_ci_lower), str(item.candidate_id)),
        ).candidate_id
    protocol_hash = _digest(
        {
            "protocol_version": M2_FWER_PROTOCOL_VERSION,
            "bootstrap_resamples": M2_FWER_BOOTSTRAP_RESAMPLES,
            "power": M2_FWER_POWER,
            "method": "bonferroni_fwer",
        }
    )
    result_payload = {
        "round_id": str(round_authority.round_id),
        "holdout_barrier_id": str(holdout_barrier.barrier_id),
        "holdout_family_hash": round_authority.holdout_family_hash,
        "protocol_hash": protocol_hash,
        "family_alpha": round_authority.family_alpha,
        "m": m,
        "alpha_candidate": alpha_candidate,
        "candidate_results": [item.model_dump(mode="json") for item in adjusted_items],
        "recommended_candidate_id": str(recommended) if recommended else None,
        "synthetic": True,
    }
    result_hash = _digest(result_payload)
    return MultipleComparisonResult(
        multiple_comparison_id=uuid5(NAMESPACE_URL, f"hcuopt:m2-bonferroni:{result_hash}"),
        round_id=round_authority.round_id,
        run_mode=SearchRoundRunMode.SCRIPTED,
        synthetic=True,
        holdout_barrier_id=holdout_barrier.barrier_id,
        holdout_family_hash=round_authority.holdout_family_hash,
        protocol_version=M2_FWER_PROTOCOL_VERSION,
        protocol_hash=protocol_hash,
        family_alpha=round_authority.family_alpha,
        m=m,
        alpha_candidate=alpha_candidate,
        candidate_results=adjusted_items,
        recommended_candidate_id=recommended,
        result_hash=result_hash,
        created_at=created_at,
    )


def _require_scripted_round(round_authority: SearchRound, state: SearchRoundState) -> None:
    if (
        round_authority.run_mode is not SearchRoundRunMode.SCRIPTED
        or round_authority.state is not state
    ):
        raise M2EvaluationError(
            "round_authority_mismatch",
            f"operation requires a Scripted Round in {state.value}",
        )


def _ordered_members(
    members: Iterable[BarrierMemberResult],
) -> tuple[BarrierMemberResult, ...]:
    items = tuple(sorted(members, key=lambda item: str(item.candidate_id)))
    if not items or len({item.candidate_id for item in items}) != len(items):
        raise M2EvaluationError(
            "barrier_member_family_invalid", "Barrier members must be non-empty and unique"
        )
    return items


def _statistics_by_candidate(
    statistics: Iterable[ScriptedCandidateStatisticsInput],
) -> dict[UUID, ScriptedCandidateStatisticsInput]:
    result: dict[UUID, ScriptedCandidateStatisticsInput] = {}
    for item in statistics:
        if item.candidate_id in result:
            raise M2EvaluationError("statistics_candidate_duplicate", "Candidate statistics repeat")
        result[item.candidate_id] = item
    return result


def _bind_statistics_to_members(
    statistics: dict[UUID, ScriptedCandidateStatisticsInput],
    members: Iterable[BarrierMemberResult],
) -> None:
    members_by_id = {item.candidate_id: item for item in members}
    for candidate_id, item in statistics.items():
        member = members_by_id.get(candidate_id)
        if member is None or (
            item.scripted_phase_receipt_id != member.scripted_phase_receipt_id
            or item.correctness_evidence_hash != member.correctness_evidence_hash
        ):
            raise M2EvaluationError(
                "statistics_evidence_binding_mismatch",
                "statistics must bind the member Receipt and correctness evidence",
            )


def _expected_candidate_family(
    candidate_ids: Iterable[UUID], *, maximum: int, error_code: str
) -> tuple[UUID, ...]:
    items = tuple(sorted(candidate_ids, key=str))
    if not 1 <= len(items) <= maximum or len(set(items)) != len(items):
        raise M2EvaluationError(
            error_code,
            f"Candidate Family must contain 1..{maximum} unique members",
        )
    return items


def _holdout_family_hash(
    *, round_authority: SearchRound, members: Iterable[BarrierMemberResult]
) -> str:
    member_items = _ordered_members(members)
    if (
        round_authority.candidate_family_hash is None
        or round_authority.artifact_family_hash is None
    ):
        raise M2EvaluationError(
            "artifact_family_not_frozen",
            "Holdout Family requires Candidate and Artifact Families",
        )
    if any(item.artifact_id is None or item.artifact_hash is None for item in member_items):
        raise M2EvaluationError(
            "holdout_artifact_missing",
            "every Holdout member must retain its immutable Artifact",
        )
    return _digest(
        {
            "schema_version": "m2a-holdout-family-v1",
            "round_id": str(round_authority.round_id),
            "candidate_family_hash": round_authority.candidate_family_hash,
            "artifact_family_hash": round_authority.artifact_family_hash,
            "selection_rule_hash": round_authority.selection_rule_hash,
            "m": len(member_items),
            "members": [
                {
                    "candidate_id": str(item.candidate_id),
                    "artifact_id": str(item.artifact_id),
                    "artifact_hash": item.artifact_hash,
                }
                for item in member_items
            ],
        }
    )


def _reject_reused_statistics_identities(
    statistics: Iterable[ScriptedCandidateStatisticsInput],
) -> None:
    items = tuple(statistics)
    for field in (
        "scripted_phase_receipt_id",
        "raw_evidence_hash",
        "baseline_sample_set_hash",
    ):
        values = [getattr(item, field) for item in items]
        if len(set(values)) != len(values):
            raise M2EvaluationError(
                "statistics_identity_reused", f"Candidate statistics reuse {field}"
            )


def _statistics_valid(item: ScriptedCandidateStatisticsInput) -> bool:
    return item.bindings_valid and item.correctness_passed and item.cleanup_healthy


def _workload_baseline_mde(
    baseline_means: tuple[float, ...], *, alpha: float, power: float
) -> float:
    overall_mean = fmean(baseline_means)
    sigma = stdev(baseline_means)
    value = (
        (NormalDist().inv_cdf(1 - alpha / 2) + NormalDist().inv_cdf(power))
        * sigma
        * math.sqrt(2 / len(baseline_means))
        / overall_mean
    )
    if not math.isfinite(value) or value < 0:
        raise M2EvaluationError("workload_mde_invalid", "Baseline restarts produced an invalid MDE")
    return value


def _bootstrap_mean_ci(
    values: tuple[float, ...],
    *,
    seed_material: str,
    iterations: int,
    confidence: float,
) -> tuple[float, float]:
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
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


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()
