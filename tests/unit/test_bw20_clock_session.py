# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import pytest

from hcuopt.deployment.bw20_clock_journal import ClockJournal
from hcuopt.deployment.bw20_clock_session import (
    BW20ClockSession,
    ClockPolicy,
    ClockReconciliationRequired,
)
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE


class Backend:
    def __init__(self):
        self.policy = ClockPolicy("auto", (600, 1500), (1800,))
        self.original = self.policy
        self.calls = []

    def capture_policy(self):
        return self.policy

    def apply_manual(self, **kw):
        self.calls.append("apply")
        self.policy = ClockPolicy("manual", (kw["sclk_mhz"],), (kw["mclk_mhz"],))

    def restore_policy(self, policy):
        self.calls.append("restore")
        self.policy = policy


@pytest.fixture
def journal(tmp_path):
    return ClockJournal(tmp_path / "clock.sqlite")


def session(backend, journal, guard=lambda: None):
    return BW20ClockSession(backend=backend, assert_control=guard,
                            resource_id=RESOURCE, authorization_id="cpu-fixture-only",
                            journal=journal)


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_normal_and_exception_exit_restore_complete_policy(failure, journal):
    backend = Backend()
    transaction = session(backend, journal)

    def run():
        with transaction:
            assert backend.policy == ClockPolicy("manual", (1500,), (1800,))
            if failure:
                raise failure("fixture")

    if failure:
        with pytest.raises(failure):
            run()
    else:
        run()
    assert backend.policy == backend.original
    assert transaction.receipt()["restored"]
    assert not transaction.receipt()["stage0_accepted"]
    transaction.restore()
    assert backend.calls == ["apply", "restore"]
    assert not journal.unresolved(RESOURCE)


def test_partial_apply_failure_restores(monkeypatch, journal):
    backend = Backend()
    original_apply = backend.apply_manual

    def partial(**kw):
        original_apply(**kw)
        raise RuntimeError("partial apply")

    monkeypatch.setattr(backend, "apply_manual", partial)
    transaction = session(backend, journal)
    with pytest.raises(RuntimeError, match="partial"):
        with transaction:
            pytest.fail("must not execute workload")
    assert backend.policy == backend.original
    assert transaction.receipt()["restored"]


def test_lease_loss_refuses_restore_write_and_requires_reconciliation(journal):
    backend = Backend()
    owned = {"yes": True}
    transaction = session(backend, journal, lambda: None if owned["yes"] else False)
    with pytest.raises(ClockReconciliationRequired):
        with transaction:
            owned["yes"] = False
    assert backend.calls == ["apply"]
    assert transaction.receipt()["quarantined"]
    assert journal.unresolved(RESOURCE)[0]["state"] == "reconciliation_required"
    with pytest.raises(ClockReconciliationRequired):
        transaction.restore()


def test_restore_ack_is_not_restoration(monkeypatch, journal):
    backend = Backend()
    monkeypatch.setattr(backend, "restore_policy", lambda policy: None)
    transaction = session(backend, journal)
    with pytest.raises(ClockReconciliationRequired):
        with transaction:
            pass
    assert not transaction.receipt()["restored"]


def test_unowned_session_never_mutates(journal):
    backend = Backend()
    transaction = session(backend, journal, lambda: False)
    with pytest.raises(RuntimeError):
        transaction.__enter__()
    assert not backend.calls


@pytest.mark.parametrize("levels", [(), (True,), (0,), (1500, 1500)])
def test_incomplete_policy_rejected(levels):
    with pytest.raises(ValueError):
        ClockPolicy("auto", levels, (1800,))


def test_no_scope_or_authority_default(journal):
    with pytest.raises(ValueError):
        BW20ClockSession(backend=Backend(), assert_control=lambda: None,
                         resource_id=RESOURCE, authorization_id="", journal=journal)


def test_apply_sees_durable_intent_and_original_policy(journal, monkeypatch):
    import json

    backend = Backend()
    apply = backend.apply_manual

    def inspect(**kw):
        record = ClockJournal(journal.path).unresolved(RESOURCE)[0]
        assert record["state"] == "mutation_possible"
        assert json.loads(record["original_json"])["sclk_levels"] == [600, 1500]
        return apply(**kw)

    monkeypatch.setattr(backend, "apply_manual", inspect)
    with session(backend, journal):
        assert journal.unresolved(RESOURCE)[0]["state"] == "active"


def test_crashed_session_blocks_next_session_without_writes(journal):
    backend = Backend()
    abandoned = session(backend, journal)
    abandoned.__enter__()  # Simulate process disappearing without __exit__.
    other_backend = Backend()
    with pytest.raises(ClockReconciliationRequired, match="blocks reuse"):
        with session(other_backend, ClockJournal(journal.path)):
            pytest.fail("must not run")
    assert other_backend.calls == []
    assert journal.unresolved(RESOURCE)[0]["operation_id"] == abandoned.operation_id


def test_intent_storage_failure_never_writes_hardware(journal, monkeypatch):
    def fail(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "begin", fail)
    backend = Backend()
    with pytest.raises(OSError):
        with session(backend, journal):
            pytest.fail("must not run")
    assert backend.calls == []


def test_failed_restore_record_remains_unresolved(journal, monkeypatch):
    transition = journal.transition

    def fail(operation_id, *, expected, state):
        if state in {"restored", "reconciliation_required"}:
            raise OSError("disk full")
        transition(operation_id, expected=expected, state=state)

    monkeypatch.setattr(journal, "transition", fail)
    backend = Backend()
    with pytest.raises(ClockReconciliationRequired):
        with session(backend, journal):
            pass
    assert backend.policy == backend.original
    assert journal.unresolved(RESOURCE)[0]["state"] == "restoring"
