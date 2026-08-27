# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from hcuopt.contracts.m2 import RoundBudget, SearchRound
from hcuopt.domain.enums import ProjectMode, SearchRoundState
from hcuopt.evaluation.m2_authority import (
    HoldoutAuthorityError,
    HoldoutRevealResult,
    SyntheticHoldoutPlanAuthority,
)
from hcuopt.measurement.evidence import canonical_json_bytes

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)
NONCE = bytes(range(32))
PLAN = {
    "protocol_version": "m2-scripted-holdout-v1",
    "cases": [
        {"shape": [1, 2048], "dtype": "float16", "seed": 20260827},
        {"shape": [4, 4096], "dtype": "float16", "seed": 20260828},
    ],
}


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _authority() -> SyntheticHoldoutPlanAuthority:
    return SyntheticHoldoutPlanAuthority(
        authority_id="m2-scripted-holdout-store",
        authority_version="1.0.0",
    )


def _round(
    authority: SyntheticHoldoutPlanAuthority,
    commitment: str,
    *,
    state: SearchRoundState = SearchRoundState.SEARCH_BARRIER,
    **updates: object,
) -> SearchRound:
    values: dict[str, object] = {
        "round_id": _uuid(1),
        "task_id": _uuid(2),
        "idempotency_key": "m2-holdout-round-idempotency",
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
        "declared_candidate_count": 2,
        "max_promoted": 2,
        "family_alpha": 0.05,
        "search_plan_hash": _hash("search-plan"),
        "holdout_plan_commitment": commitment,
        "holdout_plan_authority_id": authority.authority_id,
        "holdout_plan_authority_hash": authority.authority_hash,
        "selection_rule_hash": _hash("selection"),
        "budget": RoundBudget(
            max_candidates=2,
            max_build_attempts=4,
            max_correctness_attempts=4,
            max_search_samples=100,
            max_holdout_samples=100,
            max_wall_seconds=600,
            max_exclusive_lease_seconds=600,
        ),
        "candidate_family_hash": _hash("candidate-family"),
        "artifact_family_hash": _hash("artifact-family"),
        "holdout_family_hash": _hash("holdout-family"),
        "version": 4,
        "created_at": NOW,
        "intake_closed_at": NOW,
    }
    values.update(updates)
    return SearchRound.model_validate(values)


def _lease(
    authority: SyntheticHoldoutPlanAuthority,
    round_authority: SearchRound,
):
    return authority.issue_reveal_lease(
        round_authority=round_authority,
        authorized_worker_id="m2-scripted-measurement-worker",
        execution_lease_id=_uuid(20),
        resource_id="scripted-hcu-7",
        fencing_token=9,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        reveal_lease_id=_uuid(21),
    )


def _reveal(authority: SyntheticHoldoutPlanAuthority, *, now: datetime = NOW):
    return authority.reveal(
        reveal_lease_id=_uuid(21),
        round_id=_uuid(1),
        authorized_worker_id="m2-scripted-measurement-worker",
        execution_lease_id=_uuid(20),
        resource_id="scripted-hcu-7",
        fencing_token=9,
        revealed_at=now,
    )


def test_commitment_uses_256_bit_nonce_without_exposing_plan() -> None:
    authority = _authority()
    record = authority.commit_plan(
        round_id=_uuid(1), plan=PLAN, nonce=NONCE, created_at=NOW
    )
    expected = "sha256:" + hashlib.sha256(
        NONCE + canonical_json_bytes(PLAN)
    ).hexdigest()

    assert record.commitment == expected
    assert record.authority_hash == authority.authority_hash
    assert "plan" not in record.model_dump()
    assert "nonce" not in record.model_dump()

    with pytest.raises(ValueError, match="256 bits"):
        _authority().commit_plan(round_id=_uuid(1), plan=PLAN, nonce=b"short")
    with pytest.raises(HoldoutAuthorityError) as repeated:
        authority.commit_plan(round_id=_uuid(1), plan=PLAN, nonce=NONCE)
    assert repeated.value.code == "holdout_plan_already_committed"


def test_holdout_plan_cannot_be_read_before_a_reveal_lease() -> None:
    authority = _authority()
    authority.commit_plan(round_id=_uuid(1), plan=PLAN, nonce=NONCE, created_at=NOW)

    with pytest.raises(HoldoutAuthorityError) as failure:
        _reveal(authority)
    assert failure.value.code == "holdout_reveal_lease_invalid"


def test_reveal_lease_requires_matching_barrier_round_and_commitment() -> None:
    authority = _authority()
    commitment = authority.commit_plan(
        round_id=_uuid(1), plan=PLAN, nonce=NONCE, created_at=NOW
    )

    with pytest.raises(HoldoutAuthorityError) as early:
        _lease(
            authority,
            _round(
                authority,
                commitment.commitment,
                state=SearchRoundState.SEARCH_MEASURING,
            ),
        )
    assert early.value.code == "holdout_plan_not_revealable"

    with pytest.raises(HoldoutAuthorityError) as mismatch:
        _lease(authority, _round(authority, _hash("another-commitment")))
    assert mismatch.value.code == "holdout_commitment_mismatch"

    with pytest.raises(HoldoutAuthorityError) as no_family:
        _lease(
            authority,
            _round(authority, commitment.commitment, holdout_family_hash=None),
        )
    assert no_family.value.code == "holdout_family_not_frozen"

    with pytest.raises(HoldoutAuthorityError) as wrong_authority:
        _lease(
            authority,
            _round(
                authority,
                commitment.commitment,
                holdout_plan_authority_hash=_hash("wrong-authority"),
            ),
        )
    assert wrong_authority.value.code == "holdout_plan_authority_unavailable"

    with pytest.raises(HoldoutAuthorityError) as formal:
        _lease(
            authority,
            _round(
                authority,
                commitment.commitment,
                run_mode="formal",
                project_mode=ProjectMode.DEGRADED_MANUAL_INTAKE,
            ),
        )
    assert formal.value.code == "holdout_plan_forbidden"


def test_reveal_is_bound_to_round_worker_execution_lease_resource_and_fence() -> None:
    authority = _authority()
    commitment = authority.commit_plan(
        round_id=_uuid(1), plan=PLAN, nonce=NONCE, created_at=NOW
    )
    _lease(authority, _round(authority, commitment.commitment))

    invalid_calls = (
        {"round_id": _uuid(99)},
        {"authorized_worker_id": "another-worker"},
        {"execution_lease_id": _uuid(99)},
        {"resource_id": "another-resource"},
        {"fencing_token": 10},
    )
    base = {
        "reveal_lease_id": _uuid(21),
        "round_id": _uuid(1),
        "authorized_worker_id": "m2-scripted-measurement-worker",
        "execution_lease_id": _uuid(20),
        "resource_id": "scripted-hcu-7",
        "fencing_token": 9,
        "revealed_at": NOW,
    }
    for update in invalid_calls:
        with pytest.raises(HoldoutAuthorityError) as failure:
            authority.reveal(**(base | update))
        assert failure.value.code == "holdout_reveal_lease_invalid"

    result = authority.reveal(**base)
    assert json.loads(result.canonical_plan_json) == PLAN
    assert result.nonce_hex == NONCE.hex()
    assert result.commitment == commitment.commitment
    assert result.synthetic is True

    tampered = result.model_dump(mode="python")
    tampered["canonical_plan_json"] = '{"cases":[]}'
    with pytest.raises(ValueError, match="plan_hash"):
        HoldoutRevealResult.model_validate(tampered)


def test_reveal_is_one_time_and_expired_lease_stays_closed() -> None:
    authority = _authority()
    commitment = authority.commit_plan(
        round_id=_uuid(1), plan=PLAN, nonce=NONCE, created_at=NOW
    )
    _lease(authority, _round(authority, commitment.commitment))
    _reveal(authority)

    with pytest.raises(HoldoutAuthorityError) as repeated:
        _reveal(authority)
    assert repeated.value.code == "holdout_reveal_lease_consumed"

    expired = _authority()
    expired_commitment = expired.commit_plan(
        round_id=_uuid(1), plan=PLAN, nonce=NONCE, created_at=NOW
    )
    _lease(expired, _round(expired, expired_commitment.commitment))
    with pytest.raises(HoldoutAuthorityError) as expiry:
        _reveal(expired, now=NOW + timedelta(minutes=5))
    assert expiry.value.code == "holdout_reveal_lease_expired"


def test_reveal_returns_a_copy_of_the_frozen_plan() -> None:
    mutable_plan = {"cases": [{"shape": [1, 2], "seed": 1}]}
    authority = _authority()
    commitment = authority.commit_plan(
        round_id=_uuid(1), plan=mutable_plan, nonce=NONCE, created_at=NOW
    )
    mutable_plan["cases"][0]["shape"][0] = 99
    _lease(authority, _round(authority, commitment.commitment))

    result = _reveal(authority)

    assert json.loads(result.canonical_plan_json) == {
        "cases": [{"shape": [1, 2], "seed": 1}]
    }
