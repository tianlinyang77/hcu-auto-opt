# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.domain.enums import (
    ManualCandidateVerdict,
    RoundBarrierOutcome,
    RoundCandidateState,
    RoundPhase,
    RoundTerminalReason,
)
from hcuopt.evaluation.m2_models import (
    AdjustedCandidateResult,
    BarrierMemberResult,
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundCandidateEvidence,
    RoundEvidenceBundle,
)

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _hash(digit: str) -> str:
    return "sha256:" + digit * 64


def _member(
    ordinal: int,
    state: RoundCandidateState = RoundCandidateState.SEARCH_MEASURED,
) -> BarrierMemberResult:
    measured = state in {
        RoundCandidateState.SEARCH_MEASURED,
        RoundCandidateState.HOLDOUT_MEASURED,
    }
    phase_failed = state in {
        RoundCandidateState.SEARCH_FAILED,
        RoundCandidateState.HOLDOUT_FAILED,
    }
    return BarrierMemberResult(
        round_candidate_id=_uuid(100 + ordinal),
        candidate_id=_uuid(200 + ordinal),
        candidate_state=state,
        artifact_id=_uuid(300 + ordinal) if state is not RoundCandidateState.BUILD_FAILED else None,
        artifact_hash=_hash(str(ordinal + 1))
        if state is not RoundCandidateState.BUILD_FAILED
        else None,
        correctness_evidence_hash=_hash("a") if measured or phase_failed else None,
        scripted_phase_receipt_id=_uuid(400 + ordinal) if measured else None,
        failure_evidence_hash=None if measured else _hash("b"),
        budget_usage_evidence_hash=_hash("c"),
        cleanup_evidence_hash=_hash("d") if measured else None,
        synthetic=True,
    )


def _barrier(
    members: tuple[BarrierMemberResult, ...],
    *,
    phase: RoundPhase = RoundPhase.SEARCH,
    promoted: tuple[UUID, ...] = (),
    outcome: RoundBarrierOutcome | None = None,
    **updates: object,
) -> RoundBarrierResult:
    values: dict[str, object] = {
        "barrier_id": _uuid(1),
        "round_id": _uuid(2),
        "run_mode": "scripted",
        "synthetic": True,
        "phase": phase,
        "input_family_hash": _hash("1"),
        "expected_member_count": len(members),
        "members": members,
        "rule_version": "m2-search-selection-v1",
        "rule_hash": _hash("2"),
        "promoted_candidate_ids": promoted,
        "outcome": outcome
        or (
            RoundBarrierOutcome.MEMBERS_PROMOTED
            if promoted
            else RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
        ),
        "input_summary_hash": _hash("3"),
        "closed_by": "m2-scripted-evaluation-authority",
        "closed_at": NOW,
        "idempotency_key": "m2-barrier-idempotency",
    }
    values.update(updates)
    return RoundBarrierResult.model_validate(values)


def _adjusted(
    ordinal: int,
    verdict: ManualCandidateVerdict,
    *,
    lower: float | None = None,
    upper: float | None = None,
) -> AdjustedCandidateResult:
    invalid = verdict is ManualCandidateVerdict.INVALID
    return AdjustedCandidateResult(
        candidate_id=_uuid(200 + ordinal),
        scripted_phase_receipt_id=None if invalid else _uuid(400 + ordinal),
        synthetic=True,
        correctness_evidence_hash=_hash(str(ordinal + 4)),
        verdict=verdict,
        adjusted_ci_lower=None if invalid else lower,
        adjusted_ci_upper=None if invalid else upper,
        stage0_mde_ratio=None if invalid else 0.03,
        workload_mde_ratio=None if invalid else 0.04,
        credible_threshold=None if invalid else 0.04,
        failure_codes=("evidence_invalid",) if invalid else (),
    )


def _multiple(
    results: tuple[AdjustedCandidateResult, ...],
    *,
    recommended: UUID | None,
    **updates: object,
) -> MultipleComparisonResult:
    values: dict[str, object] = {
        "multiple_comparison_id": _uuid(10),
        "round_id": _uuid(2),
        "run_mode": "scripted",
        "synthetic": True,
        "holdout_barrier_id": _uuid(11),
        "holdout_family_hash": _hash("4"),
        "protocol_version": "m2-bonferroni-bootstrap-v1",
        "protocol_hash": _hash("5"),
        "family_alpha": 0.05,
        "m": len(results),
        "alpha_candidate": 0.05 / len(results),
        "candidate_results": results,
        "recommended_candidate_id": recommended,
        "result_hash": _hash("6"),
        "created_at": NOW,
    }
    values.update(updates)
    return MultipleComparisonResult.model_validate(values)


def _candidate_evidence(ordinal: int, *, holdout: bool) -> RoundCandidateEvidence:
    return RoundCandidateEvidence(
        round_candidate_id=_uuid(100 + ordinal),
        candidate_id=_uuid(200 + ordinal),
        search_member_state=RoundCandidateState.SEARCH_MEASURED,
        search_evidence_hash=_hash(str(ordinal + 1)),
        holdout_member_state=(
            RoundCandidateState.HOLDOUT_MEASURED if holdout else None
        ),
        holdout_evidence_hash=_hash(str(ordinal + 4)) if holdout else None,
        budget_evidence_hashes=(_hash("8"),),
        cleanup_evidence_hashes=(_hash("9"),),
    )


def _bundle(*, holdout: bool, synthetic: bool = True, **updates: object) -> RoundEvidenceBundle:
    values: dict[str, object] = {
        "round_evidence_bundle_id": _uuid(20),
        "round_id": _uuid(2),
        "task_id": _uuid(21),
        "run_mode": "scripted" if synthetic else "formal",
        "terminal_reason": (
            RoundTerminalReason.HOLDOUT_COMPLETED
            if holdout
            else RoundTerminalReason.NO_PROMOTABLE_CANDIDATE
        ),
        "candidate_family_hash": _hash("1"),
        "artifact_family_hash": _hash("2"),
        "holdout_family_hash": _hash("3") if holdout else None,
        "search_plan_hash": _hash("4"),
        "holdout_plan_commitment": _hash("5"),
        "holdout_plan_hash": _hash("6") if holdout else None,
        "holdout_reveal_evidence_hash": _hash("7") if holdout else None,
        "candidate_evidence": (_candidate_evidence(0, holdout=holdout),),
        "search_barrier_id": _uuid(22),
        "holdout_barrier_id": _uuid(23) if holdout else None,
        "multiple_comparison_id": _uuid(24) if holdout else None,
        "budget_ledger_hash": _hash("8"),
        "evidence_index_uri": "file:///evidence/index.json",
        "evidence_index_hash": _hash("9"),
        "summary": {"performance_conclusion": "not_available"},
        "synthetic": synthetic,
        "created_at": NOW,
    }
    values.update(updates)
    return RoundEvidenceBundle.model_validate(values)


def test_search_barrier_preserves_failures_and_promotes_only_measured() -> None:
    measured = _member(0)
    failed = _member(1, RoundCandidateState.BUILD_FAILED)
    barrier = _barrier((measured, failed), promoted=(measured.candidate_id,))

    assert barrier.outcome is RoundBarrierOutcome.MEMBERS_PROMOTED
    assert len(barrier.members) == 2

    with pytest.raises(ValidationError, match="only measured"):
        _barrier((measured, failed), promoted=(failed.candidate_id,))


def test_search_zero_promotion_and_holdout_barrier_are_discriminated() -> None:
    search = _barrier((_member(0),))
    holdout_member = _member(0, RoundCandidateState.HOLDOUT_MEASURED)
    holdout = _barrier(
        (holdout_member,),
        phase=RoundPhase.HOLDOUT,
        outcome=RoundBarrierOutcome.COMPLETED,
    )

    assert search.outcome is RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
    assert holdout.promoted_candidate_ids == ()
    with pytest.raises(ValidationError, match="complete without promotions"):
        _barrier(
            (holdout_member,),
            phase=RoundPhase.HOLDOUT,
            promoted=(holdout_member.candidate_id,),
            outcome=RoundBarrierOutcome.COMPLETED,
        )


def test_barrier_rejects_missing_duplicate_and_non_terminal_members() -> None:
    member = _member(0)
    with pytest.raises(ValidationError, match="every frozen family member"):
        _barrier((member,), expected_member_count=2)
    with pytest.raises(ValidationError, match="unique Candidates"):
        _barrier((member, member))
    with pytest.raises(ValidationError, match="measurement or failure terminal"):
        _member(0, RoundCandidateState.CORRECTNESS_PASSED)
    with pytest.raises(ValidationError, match="run mode and synthetic"):
        _barrier((member,), run_mode="formal")


def test_barrier_rejects_authority_mix_and_incomplete_holdout_failure() -> None:
    measured = _member(0)
    values = measured.model_dump(mode="python")
    values["round_measurement_ref_id"] = _uuid(999)
    with pytest.raises(ValidationError, match="complete success evidence"):
        BarrierMemberResult.model_validate(values)

    failed = _member(1, RoundCandidateState.HOLDOUT_FAILED)
    values = failed.model_dump(mode="python")
    values["correctness_evidence_hash"] = None
    with pytest.raises(ValidationError, match="correctness evidence"):
        BarrierMemberResult.model_validate(values)

    holdout_members = tuple(
        _member(index, RoundCandidateState.HOLDOUT_MEASURED) for index in range(3)
    )
    with pytest.raises(ValidationError, match="at most two"):
        _barrier(
            holdout_members,
            phase=RoundPhase.HOLDOUT,
            outcome=RoundBarrierOutcome.COMPLETED,
        )


def test_adjusted_verdict_uses_conservative_threshold() -> None:
    faster = _adjusted(0, ManualCandidateVerdict.FASTER, lower=0.08, upper=0.12)
    slower = _adjusted(1, ManualCandidateVerdict.SLOWER, lower=-0.12, upper=-0.08)
    inconclusive = _adjusted(
        2, ManualCandidateVerdict.INCONCLUSIVE, lower=-0.01, upper=0.07
    )

    assert faster.credible_threshold == 0.04
    assert slower.verdict is ManualCandidateVerdict.SLOWER
    assert inconclusive.verdict is ManualCandidateVerdict.INCONCLUSIVE
    with pytest.raises(ValidationError, match="does not match"):
        _adjusted(3, ManualCandidateVerdict.FASTER, lower=0.02, upper=0.10)


def test_bonferroni_keeps_invalid_in_m_and_recommends_deterministically() -> None:
    faster = _adjusted(0, ManualCandidateVerdict.FASTER, lower=0.08, upper=0.12)
    invalid = _adjusted(1, ManualCandidateVerdict.INVALID)
    result = _multiple((faster, invalid), recommended=faster.candidate_id)

    assert result.m == 2
    assert result.alpha_candidate == pytest.approx(0.025)
    assert tuple(item.verdict for item in result.candidate_results) == (
        ManualCandidateVerdict.FASTER,
        ManualCandidateVerdict.INVALID,
    )
    with pytest.raises(ValidationError, match="family_alpha / m"):
        _multiple(
            (faster, invalid),
            recommended=faster.candidate_id,
            alpha_candidate=0.05,
        )


def test_bonferroni_preserves_all_faster_and_uses_uuid_tie_break() -> None:
    first = _adjusted(0, ManualCandidateVerdict.FASTER, lower=0.08, upper=0.11)
    second = _adjusted(1, ManualCandidateVerdict.FASTER, lower=0.08, upper=0.12)

    result = _multiple((first, second), recommended=first.candidate_id)

    assert len(result.candidate_results) == 2
    with pytest.raises(ValidationError, match="UUID tie-break"):
        _multiple((first, second), recommended=second.candidate_id)
    with pytest.raises(ValidationError, match="mode and synthetic"):
        _multiple((first, second), recommended=first.candidate_id, run_mode="formal")


def test_round_evidence_discriminates_zero_promotion_and_holdout_paths() -> None:
    zero = _bundle(holdout=False)
    completed = _bundle(holdout=True)

    assert zero.holdout_family_hash is None
    assert completed.multiple_comparison_id is not None
    assert completed.synthetic is True

    with pytest.raises(ValidationError, match="omit Holdout and FWER"):
        _bundle(holdout=False, holdout_family_hash=_hash("a"))
    with pytest.raises(ValidationError, match="requires every Holdout"):
        _bundle(holdout=True, multiple_comparison_id=None)


def test_round_evidence_rejects_synthetic_formal_mismatch_and_naive_time() -> None:
    with pytest.raises(ValidationError, match="run mode and synthetic"):
        _bundle(holdout=False, run_mode="formal", synthetic=True)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _bundle(holdout=False, created_at=datetime(2026, 8, 27))
