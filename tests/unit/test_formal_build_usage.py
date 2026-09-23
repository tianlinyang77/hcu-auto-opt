# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.contracts.m2 import BudgetUsage
from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_build_journal import PostgresFormalBuildJournal


@pytest.fixture
def usage_case(monkeypatch):
    candidate_id, reservation_id, round_id = uuid4(), uuid4(), uuid4()
    budget = {
        "round_id": round_id, "candidate_id": candidate_id,
        "planned": BudgetUsage(build_attempts=1, wall_seconds=20).model_dump(mode="json"),
    }
    row = {
        "state": "result_recorded", "finished_at": datetime.now(timezone.utc),
        "result": {"build": {"explicit": "unit fixture"}, "usage": BudgetUsage(
            build_attempts=1, wall_seconds=12.5,
        ).model_dump(mode="json")},
    }
    finalized = []
    locked = []

    @contextmanager
    def connection():
        locked.append(True)
        try:
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: budget))
        finally:
            locked.pop()

    def finalize(entry):
        assert not locked, "must not nest Round budget locks under Intent transaction"
        finalized.append(entry)
        return {"ledger_entry": entry}

    repo = SimpleNamespace(connection=connection, finalize_round_budget=finalize)
    claims = SimpleNamespace(dispatcher=SimpleNamespace(repository=repo))
    journal = PostgresFormalBuildJournal(claims, uuid4(), "unit-worker", uuid4())
    monkeypatch.setattr(
        journal, "_locked", lambda *args: (SimpleNamespace(round_id=round_id), row),
    )
    return journal, (candidate_id, reservation_id, "sha256:" + "a" * 64), row, budget, finalized


def test_settlement_uses_persisted_usage_and_stable_identity(usage_case):
    journal, args, row, budget, finalized = usage_case
    first = journal.settle_recorded_result(*args)
    assert journal.settle_recorded_result(*args) == first
    entry = finalized[0]
    assert entry == finalized[1]
    assert entry.entry_type == "settle"
    assert entry.actual == BudgetUsage(build_attempts=1, wall_seconds=12.5)
    assert entry.reserved == BudgetUsage.model_validate(budget["planned"])
    assert entry.created_at == row["finished_at"]
    assert entry.harness_active_seconds == entry.lease_held_seconds == 0
    assert entry.raw_usage_evidence_hash.startswith("sha256:")


@pytest.mark.parametrize("state", ["invoking", "recovery_required"])
def test_uncertain_outcome_does_not_settle(usage_case, state):
    journal, args, row, _, finalized = usage_case
    row["state"] = state
    with pytest.raises(Conflict, match="reconciliation"):
        journal.settle_recorded_result(*args)
    assert finalized == []


def test_legacy_missing_usage_does_not_settle(usage_case):
    journal, args, row, _, finalized = usage_case
    del row["result"]["usage"]
    with pytest.raises(Conflict, match="reconciliation"):
        journal.settle_recorded_result(*args)
    assert finalized == []


@pytest.mark.parametrize("change", [
    {"wall_seconds": float("inf")}, {"build_attempts": 2},
    {"exclusive_lease_seconds": 3}, {"search_samples": 1},
])
def test_non_cpu_build_usage_refused(usage_case, change):
    journal, args, row, _, finalized = usage_case
    row["result"]["usage"].update(change)
    with pytest.raises(Conflict, match="single CPU build"):
        journal.settle_recorded_result(*args)
    assert finalized == []


def test_wrong_candidate_budget_cannot_settle(usage_case):
    journal, args, _, budget, finalized = usage_case
    budget["candidate_id"] = uuid4()
    with pytest.raises(Conflict, match="binding differs"):
        journal.settle_recorded_result(*args)
    assert finalized == []
