# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hcuopt.contracts.m2 import (
    BudgetUsage,
    RoundBudget,
    RoundBudgetLedgerEntry,
    RoundCandidate,
    SearchRound,
)

NOW = datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return "sha256:" + value * 64


def _budget() -> RoundBudget:
    return RoundBudget(
        max_candidates=2,
        max_build_attempts=4,
        max_correctness_attempts=4,
        max_search_samples=200,
        max_holdout_samples=200,
        max_wall_seconds=600,
        max_exclusive_lease_seconds=300,
    )


def _round(**updates) -> SearchRound:
    payload = {
        "round_id": uuid4(),
        "task_id": uuid4(),
        "state": "intake_open",
        "run_mode": "scripted",
        "project_mode": None,
        "target_snapshot_id": uuid4(),
        "stage0_run_id": uuid4(),
        "stage0_protocol_hash": _hash("1"),
        "baseline_epoch_id": uuid4(),
        "hotspot_id": uuid4(),
        "replacement_point": "sglang.fixture.layer_norm",
        "workload_id": "m2-scripted-fixture-v1",
        "workload_hash": _hash("2"),
        "configuration_hash": _hash("3"),
        "image_digest": _hash("4"),
        "adapter_profile": "m2-scripted-v1",
        "declared_candidate_count": 2,
        "max_promoted": 2,
        "family_alpha": 0.05,
        "search_plan_hash": _hash("5"),
        "holdout_plan_commitment": _hash("6"),
        "holdout_plan_authority_id": "synthetic-holdout-v1",
        "holdout_plan_authority_hash": _hash("7"),
        "selection_rule_hash": _hash("8"),
        "budget": _budget(),
        "version": 1,
        "created_at": NOW,
    }
    payload.update(updates)
    return SearchRound.model_validate(payload)


def _candidate(**updates) -> RoundCandidate:
    payload = {
        "round_candidate_id": uuid4(),
        "round_id": uuid4(),
        "candidate_id": uuid4(),
        "ordinal": 0,
        "source_package_store_id": "synthetic-package-store-v1",
        "source_package_store_hash": _hash("1"),
        "source_package_hash": _hash("2"),
        "source_manifest_version": "m1-candidate-source-v1",
        "source_manifest_hash": _hash("3"),
        "baseline_source_hash": _hash("4"),
        "candidate_source_hash": _hash("5"),
        "optimization_intent": "fixture known signal",
        "replacement_point": "sglang.fixture.layer_norm",
        "candidate_kind": "fixture",
        "state": "intake_accepted",
        "idempotency_key": "m2-candidate-fixture",
    }
    payload.update(updates)
    return RoundCandidate.model_validate(payload)


def test_search_round_is_strict_and_binds_scripted_mode() -> None:
    result = _round()
    assert result.schema_version == "m2a-search-round-v1"
    assert result.budget.max_candidates == result.declared_candidate_count
    assert result.automatic_release_allowed is False

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _round(unreviewed_runtime_override=True)
    with pytest.raises(ValidationError, match="scripted Round project_mode must be null"):
        _round(project_mode="degraded_manual_intake")
    with pytest.raises(ValidationError, match="requires degraded_manual_intake"):
        _round(run_mode="formal", project_mode=None)
    with pytest.raises(ValidationError, match="cannot enter Formal"):
        _round(state="awaiting_signoff")
    with pytest.raises(ValidationError, match="cannot enter scripted_completed"):
        _round(
            run_mode="formal",
            project_mode="degraded_manual_intake",
            state="scripted_completed",
        )
    with pytest.raises(ValidationError, match="budget max_candidates"):
        _round(declared_candidate_count=3)
    with pytest.raises(ValidationError, match="Input should be False"):
        _round(automatic_release_allowed=True)


def test_search_round_reveal_fields_are_atomic() -> None:
    with pytest.raises(ValidationError, match="written together"):
        _round(holdout_plan_hash=_hash("9"))
    with pytest.raises(ValidationError, match="frozen holdout_family_hash"):
        _round(
            holdout_plan_hash=_hash("9"),
            holdout_reveal_lease_id=uuid4(),
            holdout_reveal_evidence_hash=_hash("a"),
        )

    result = _round(
        holdout_plan_hash=_hash("9"),
        holdout_reveal_lease_id=uuid4(),
        holdout_reveal_evidence_hash=_hash("a"),
        holdout_family_hash=_hash("b"),
    )
    assert result.holdout_reveal_lease_id is not None


def test_round_candidate_rejects_partial_or_ambiguous_terminal_identity() -> None:
    assert _candidate().track == "triton"
    with pytest.raises(ValidationError, match="artifact_id and artifact_hash"):
        _candidate(artifact_id=uuid4())
    with pytest.raises(ValidationError, match="failure code and failure evidence"):
        _candidate(terminal_failure_code="build_failed")
    with pytest.raises(ValidationError, match="Artifact and terminal failure"):
        _candidate(
            artifact_id=uuid4(),
            artifact_hash=_hash("6"),
            terminal_failure_code="build_failed",
            failure_evidence_hash=_hash("7"),
        )
    with pytest.raises(ValidationError, match="must differ"):
        _candidate(candidate_source_hash=_hash("4"))
    with pytest.raises(ValidationError, match="m1-candidate-source-v1"):
        _candidate(source_manifest_version="m2-unreviewed-manifest-v2")


def test_budget_ledger_uses_lease_time_and_release_has_no_actual_charge() -> None:
    entry = RoundBudgetLedgerEntry(
        ledger_entry_id=uuid4(),
        reservation_id=uuid4(),
        round_id=uuid4(),
        entry_type="settle",
        reserved=BudgetUsage(search_samples=20, exclusive_lease_seconds=10),
        actual=BudgetUsage(search_samples=18, exclusive_lease_seconds=8),
        lease_held_seconds=8,
        harness_active_seconds=6,
        raw_usage_evidence_hash=_hash("1"),
        idempotency_key="m2-budget-settle",
        created_at=NOW,
    )
    assert entry.actual.exclusive_lease_seconds == 8

    payload = entry.model_dump(mode="json")
    with pytest.raises(ValidationError, match="cannot exceed Lease"):
        RoundBudgetLedgerEntry.model_validate({**payload, "harness_active_seconds": 9})
    with pytest.raises(ValidationError, match="release entry cannot report actual"):
        RoundBudgetLedgerEntry.model_validate({**payload, "entry_type": "release"})
    with pytest.raises(ValidationError, match="must equal Lease held time"):
        RoundBudgetLedgerEntry.model_validate(
            {
                **payload,
                "actual": BudgetUsage(
                    search_samples=18, exclusive_lease_seconds=7
                ).model_dump(mode="json"),
            }
        )
