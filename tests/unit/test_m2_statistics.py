# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.contracts.m2 import RoundBudget, SearchRound
from hcuopt.domain.enums import (
    ManualCandidateVerdict,
    ProjectMode,
    RoundCandidateState,
    SearchRoundState,
)
from hcuopt.evaluation.m2_models import BarrierMemberResult, RoundBarrierResult
from hcuopt.evaluation.m2_statistics import (
    M2EvaluationError,
    ScriptedCandidateStatisticsInput,
    bonferroni_fwer,
    close_scripted_holdout_barrier,
    close_scripted_search_barrier,
)

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _round(
    *,
    state: SearchRoundState = SearchRoundState.SEARCH_BARRIER,
    count: int = 3,
    max_promoted: int = 2,
    **updates: object,
) -> SearchRound:
    values: dict[str, object] = {
        "round_id": _uuid(1),
        "task_id": _uuid(2),
        "idempotency_key": "m2-statistics-round-idempotency",
        "state": state,
        "run_mode": "scripted",
        "project_mode": None,
        "target_snapshot_id": _uuid(3),
        "stage0_run_id": _uuid(4),
        "stage0_protocol_hash": _hash("stage0"),
        "baseline_epoch_id": _uuid(5),
        "hotspot_id": _uuid(6),
        "replacement_point": "sglang.srt.layers.fixture",
        "workload_id": "scripted-workload",
        "workload_hash": _hash("workload"),
        "configuration_hash": _hash("configuration"),
        "image_digest": _hash("image"),
        "adapter_profile": "m2-scripted-v1",
        "declared_candidate_count": count,
        "max_promoted": max_promoted,
        "family_alpha": 0.05,
        "search_plan_hash": _hash("search-plan"),
        "holdout_plan_commitment": _hash("holdout-commitment"),
        "holdout_plan_authority_id": "m2-scripted-holdout-store",
        "holdout_plan_authority_hash": _hash("holdout-authority"),
        "selection_rule_hash": _hash("selection-rule"),
        "budget": RoundBudget(
            max_candidates=count,
            max_build_attempts=8,
            max_correctness_attempts=8,
            max_search_samples=1_000,
            max_holdout_samples=1_000,
            max_wall_seconds=600,
            max_exclusive_lease_seconds=600,
        ),
        "candidate_family_hash": _hash("candidate-family"),
        "artifact_family_hash": _hash("artifact-family"),
        "version": 4,
        "created_at": NOW,
        "intake_closed_at": NOW,
    }
    values.update(updates)
    return SearchRound.model_validate(values)


def _member(
    ordinal: int,
    *,
    phase: str = "search",
    state: RoundCandidateState | None = None,
    artifact_hash: str | None = None,
) -> BarrierMemberResult:
    terminal = state or (
        RoundCandidateState.SEARCH_MEASURED
        if phase == "search"
        else RoundCandidateState.HOLDOUT_MEASURED
    )
    measured = terminal in {
        RoundCandidateState.SEARCH_MEASURED,
        RoundCandidateState.HOLDOUT_MEASURED,
    }
    phase_failed = terminal in {
        RoundCandidateState.SEARCH_FAILED,
        RoundCandidateState.HOLDOUT_FAILED,
    }
    has_artifact = terminal is not RoundCandidateState.BUILD_FAILED
    return BarrierMemberResult(
        round_candidate_id=_uuid(100 + ordinal),
        candidate_id=_uuid(200 + ordinal),
        candidate_state=terminal,
        artifact_id=_uuid(300 + ordinal) if has_artifact else None,
        artifact_hash=(artifact_hash or _hash(f"artifact-{ordinal}")) if has_artifact else None,
        correctness_evidence_hash=(
            _hash(f"correctness-{ordinal}") if measured or phase_failed else None
        ),
        scripted_phase_receipt_id=(
            _uuid((400 if phase == "search" else 500) + ordinal) if measured else None
        ),
        failure_evidence_hash=None if measured else _hash(f"failure-{ordinal}"),
        budget_usage_evidence_hash=_hash(f"budget-{phase}-{ordinal}"),
        cleanup_evidence_hash=(_hash(f"cleanup-{phase}-{ordinal}") if measured else None),
        synthetic=True,
    )


def _statistics(
    member: BarrierMemberResult,
    *,
    phase: str,
    effects: tuple[float, ...],
    valid: bool = True,
    **updates: object,
) -> ScriptedCandidateStatisticsInput:
    values: dict[str, object] = {
        "candidate_id": member.candidate_id,
        "scripted_phase_receipt_id": member.scripted_phase_receipt_id,
        "correctness_evidence_hash": member.correctness_evidence_hash,
        "raw_evidence_hash": _hash(f"raw-{phase}-{member.candidate_id}"),
        "baseline_sample_set_hash": _hash(f"baseline-{phase}-{member.candidate_id}"),
        "restart_effects": effects,
        "baseline_restart_means_ns": (100.0, 100.0, 100.0, 100.0),
        "stage0_mde_ratio": 0.03,
        "bindings_valid": valid,
        "failure_codes": () if valid else ("bindings_invalid",),
    }
    values.update(updates)
    return ScriptedCandidateStatisticsInput.model_validate(values)


def _close_search(
    round_authority: SearchRound,
    members: tuple[BarrierMemberResult, ...],
    statistics: tuple[ScriptedCandidateStatisticsInput, ...],
    *,
    closed_at: datetime = NOW,
):
    return close_scripted_search_barrier(
        round_authority=round_authority,
        expected_candidate_ids=(item.candidate_id for item in members),
        members=members,
        statistics=statistics,
        closed_by="m2-scripted-evaluation-authority",
        closed_at=closed_at,
        idempotency_key="m2-search-barrier-idempotency",
    )


def _holdout_round(search_round: SearchRound, holdout_family_hash: str) -> SearchRound:
    values = search_round.model_dump(mode="python")
    values.update(
        state=SearchRoundState.HOLDOUT_BARRIER,
        holdout_family_hash=holdout_family_hash,
        holdout_plan_hash=_hash("holdout-plan"),
        holdout_reveal_lease_id=_uuid(700),
        holdout_reveal_evidence_hash=_hash("holdout-reveal"),
        version=search_round.version + 1,
    )
    return SearchRound.model_validate(values)


def _close_holdout(
    round_authority: SearchRound,
    members: tuple[BarrierMemberResult, ...],
) -> RoundBarrierResult:
    return close_scripted_holdout_barrier(
        round_authority=round_authority,
        expected_candidate_ids=(item.candidate_id for item in members),
        members=members,
        closed_by="m2-scripted-evaluation-authority",
        closed_at=NOW,
        idempotency_key="m2-holdout-barrier-idempotency",
    )


def test_search_barrier_waits_for_the_exact_family_and_promotes_at_most_two() -> None:
    round_authority = _round()
    members = tuple(_member(index) for index in range(3))
    statistics = (
        _statistics(members[0], phase="search", effects=(0.10,) * 4),
        _statistics(members[1], phase="search", effects=(0.20,) * 4),
        _statistics(members[2], phase="search", effects=(-0.10,) * 4),
    )

    decision = _close_search(round_authority, members, statistics)

    assert decision.barrier.members == members
    assert decision.barrier.promoted_candidate_ids == (
        members[0].candidate_id,
        members[1].candidate_id,
    )
    assert decision.holdout_family_hash is not None

    with pytest.raises(M2EvaluationError) as missing:
        close_scripted_search_barrier(
            round_authority=round_authority,
            expected_candidate_ids=(item.candidate_id for item in members),
            members=members[:-1],
            statistics=statistics[:-1],
            closed_by="authority",
            closed_at=NOW,
            idempotency_key="missing-member-idempotency",
        )
    assert missing.value.code == "search_candidate_family_mismatch"


def test_search_rejects_duplicate_family_and_statistics_identities() -> None:
    round_authority = _round(count=2)
    members = (_member(0), _member(1))
    statistics = (
        _statistics(members[0], phase="search", effects=(0.10,) * 4),
        _statistics(members[1], phase="search", effects=(0.20,) * 4),
    )

    with pytest.raises(M2EvaluationError) as duplicate_family:
        close_scripted_search_barrier(
            round_authority=round_authority,
            expected_candidate_ids=(members[0].candidate_id,) * 2,
            members=members,
            statistics=statistics,
            closed_by="authority",
            closed_at=NOW,
            idempotency_key="duplicate-family-idempotency",
        )
    assert duplicate_family.value.code == "search_candidate_family_invalid"

    reused = statistics[1].model_copy(update={"raw_evidence_hash": statistics[0].raw_evidence_hash})
    with pytest.raises(M2EvaluationError) as duplicate_identity:
        _close_search(round_authority, members, (statistics[0], reused))
    assert duplicate_identity.value.code == "statistics_identity_reused"


def test_search_statistics_must_bind_member_receipt_and_correctness() -> None:
    round_authority = _round(count=2)
    members = (_member(0), _member(1))
    statistics = (
        _statistics(members[0], phase="search", effects=(0.10,) * 4),
        _statistics(members[1], phase="search", effects=(0.20,) * 4),
    )

    for update in (
        {"scripted_phase_receipt_id": _uuid(999)},
        {"correctness_evidence_hash": _hash("wrong-correctness")},
    ):
        tampered = statistics[1].model_copy(update=update)
        with pytest.raises(M2EvaluationError) as mismatch:
            _close_search(round_authority, members, (statistics[0], tampered))
        assert mismatch.value.code == "statistics_evidence_binding_mismatch"


def test_search_zero_promotion_skips_holdout_family_and_ties_use_uuid() -> None:
    round_authority = _round(count=2, max_promoted=1)
    members = (_member(0), _member(1))
    zero = _close_search(
        round_authority,
        members,
        (
            _statistics(members[0], phase="search", effects=(-0.10,) * 4),
            _statistics(members[1], phase="search", effects=(0.0,) * 4),
        ),
    )
    tied = _close_search(
        round_authority,
        members,
        tuple(_statistics(member, phase="search", effects=(0.10,) * 4) for member in members),
    )

    assert zero.barrier.promoted_candidate_ids == ()
    assert zero.holdout_family_hash is None
    assert tied.barrier.promoted_candidate_ids == (members[0].candidate_id,)


def test_holdout_barrier_recomputes_family_and_rejects_artifact_drift() -> None:
    search_round = _round(count=2)
    search_members = (_member(0), _member(1))
    search = _close_search(
        search_round,
        search_members,
        tuple(
            _statistics(member, phase="search", effects=(0.10,) * 4) for member in search_members
        ),
    )
    assert search.holdout_family_hash is not None
    holdout_round = _holdout_round(search_round, search.holdout_family_hash)
    holdout_members = (_member(0, phase="holdout"), _member(1, phase="holdout"))

    barrier = _close_holdout(holdout_round, holdout_members)
    assert barrier.expected_member_count == 2

    drifted = (
        holdout_members[0],
        _member(1, phase="holdout", artifact_hash=_hash("drifted-artifact")),
    )
    with pytest.raises(M2EvaluationError) as mismatch:
        _close_holdout(holdout_round, drifted)
    assert mismatch.value.code == "holdout_family_hash_mismatch"


def test_holdout_failure_stays_in_m_and_bonferroni_alpha_is_frozen() -> None:
    search_round = _round(count=2)
    search_members = (_member(0), _member(1))
    search = _close_search(
        search_round,
        search_members,
        tuple(
            _statistics(member, phase="search", effects=(0.10,) * 4) for member in search_members
        ),
    )
    assert search.holdout_family_hash is not None
    holdout_round = _holdout_round(search_round, search.holdout_family_hash)
    measured = _member(0, phase="holdout")
    failed = _member(
        1,
        phase="holdout",
        state=RoundCandidateState.HOLDOUT_FAILED,
    )
    barrier = _close_holdout(holdout_round, (measured, failed))

    result = bonferroni_fwer(
        round_authority=holdout_round,
        holdout_barrier=barrier,
        statistics=(_statistics(measured, phase="holdout", effects=(0.20,) * 4),),
        created_at=NOW,
    )

    assert result.m == 2
    assert result.alpha_candidate == pytest.approx(0.025)
    assert tuple(item.verdict for item in result.candidate_results) == (
        ManualCandidateVerdict.FASTER,
        ManualCandidateVerdict.INVALID,
    )


def test_fwer_classifies_valid_and_invalid_measured_members() -> None:
    search_round = _round(count=2)
    search_members = (_member(0), _member(1))
    search = _close_search(
        search_round,
        search_members,
        tuple(
            _statistics(member, phase="search", effects=(0.10,) * 4) for member in search_members
        ),
    )
    assert search.holdout_family_hash is not None
    holdout_round = _holdout_round(search_round, search.holdout_family_hash)
    holdout_members = (_member(0, phase="holdout"), _member(1, phase="holdout"))
    barrier = _close_holdout(holdout_round, holdout_members)

    slower = bonferroni_fwer(
        round_authority=holdout_round,
        holdout_barrier=barrier,
        statistics=(
            _statistics(holdout_members[0], phase="holdout", effects=(-0.20,) * 4),
            _statistics(holdout_members[1], phase="holdout", effects=(0.0,) * 4),
        ),
        created_at=NOW,
    )
    invalid = bonferroni_fwer(
        round_authority=holdout_round,
        holdout_barrier=barrier,
        statistics=(
            _statistics(holdout_members[0], phase="holdout", effects=(0.01,) * 4),
            _statistics(
                holdout_members[1],
                phase="holdout",
                effects=(0.20,) * 4,
                valid=False,
            ),
        ),
        created_at=NOW,
    )

    assert tuple(item.verdict for item in slower.candidate_results) == (
        ManualCandidateVerdict.SLOWER,
        ManualCandidateVerdict.INCONCLUSIVE,
    )
    assert tuple(item.verdict for item in invalid.candidate_results) == (
        ManualCandidateVerdict.INCONCLUSIVE,
        ManualCandidateVerdict.INVALID,
    )


def test_fwer_keeps_all_faster_and_recommends_deterministically() -> None:
    search_round = _round(count=2)
    search_members = (_member(0), _member(1))
    search = _close_search(
        search_round,
        search_members,
        tuple(
            _statistics(member, phase="search", effects=(0.10,) * 4) for member in search_members
        ),
    )
    assert search.holdout_family_hash is not None
    holdout_round = _holdout_round(search_round, search.holdout_family_hash)
    holdout_members = (_member(0, phase="holdout"), _member(1, phase="holdout"))
    barrier = _close_holdout(holdout_round, holdout_members)
    statistics = tuple(
        _statistics(member, phase="holdout", effects=(0.20,) * 4) for member in holdout_members
    )

    first = bonferroni_fwer(
        round_authority=holdout_round,
        holdout_barrier=barrier,
        statistics=statistics,
        created_at=NOW,
    )
    repeated = bonferroni_fwer(
        round_authority=holdout_round,
        holdout_barrier=barrier,
        statistics=reversed(statistics),
        created_at=NOW + timedelta(seconds=1),
    )
    changed_statistics = (
        statistics[0].model_copy(update={"raw_evidence_hash": _hash("changed-raw")}),
        statistics[1],
    )
    changed = bonferroni_fwer(
        round_authority=holdout_round,
        holdout_barrier=barrier,
        statistics=changed_statistics,
        created_at=NOW,
    )

    assert all(item.verdict is ManualCandidateVerdict.FASTER for item in first.candidate_results)
    assert first.recommended_candidate_id == holdout_members[0].candidate_id
    assert repeated.recommended_candidate_id == first.recommended_candidate_id
    assert repeated.result_hash == first.result_hash
    assert repeated.multiple_comparison_id == first.multiple_comparison_id
    assert changed.result_hash != first.result_hash
    assert changed.multiple_comparison_id != first.multiple_comparison_id


def test_barrier_and_fwer_reject_wrong_authority_naive_time_and_zero_m() -> None:
    scripted = _round(count=2)
    members = (_member(0), _member(1))
    statistics = tuple(
        _statistics(member, phase="search", effects=(0.10,) * 4) for member in members
    )
    formal_values = scripted.model_dump(mode="python")
    formal_values.update(
        run_mode="formal",
        project_mode=ProjectMode.DEGRADED_MANUAL_INTAKE,
    )
    formal = SearchRound.model_validate(formal_values)

    with pytest.raises(M2EvaluationError) as wrong_authority:
        _close_search(formal, members, statistics)
    assert wrong_authority.value.code == "round_authority_mismatch"

    with pytest.raises(ValidationError, match="timezone-aware"):
        _close_search(
            scripted,
            members,
            statistics,
            closed_at=datetime(2026, 8, 27),
        )

    search = _close_search(scripted, members, statistics)
    assert search.holdout_family_hash is not None
    holdout_round = _holdout_round(scripted, search.holdout_family_hash)
    holdout_members = (_member(0, phase="holdout"), _member(1, phase="holdout"))
    barrier = _close_holdout(holdout_round, holdout_members)
    invalid_barrier = barrier.model_copy(update={"expected_member_count": 0, "members": ()})
    with pytest.raises(M2EvaluationError) as zero_m:
        bonferroni_fwer(
            round_authority=holdout_round,
            holdout_barrier=invalid_barrier,
            statistics=(),
            created_at=NOW,
        )
    assert zero_m.value.code == "holdout_m_invalid"
