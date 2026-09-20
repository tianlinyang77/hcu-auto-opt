# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from dataclasses import asdict
from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_auto_clock_session import _observe
from hcuopt.deployment.bw20_clock_journal import ClockJournal, ClockJournalError
from hcuopt.deployment.bw20_clock_recovery import (
    BW20AutoClockRecovery,
    ClockRecoverySettlementError,
)
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from tests.unit.test_bw20_auto_clock_session import SimulatedBackend


def unresolved_case(tmp_path, *, mode="manual"):
    backend = SimulatedBackend()
    baseline, _ = _observe(backend.observe(), mode="auto")
    journal = ClockJournal(tmp_path / "clock.sqlite")
    operation = journal.begin(
        resource_id=RESOURCE,
        authorization_id="original-window",
        original=asdict(baseline),
    )
    journal.transition(
        operation,
        expected="mutation_possible",
        state="reconciliation_required",
    )
    if mode == "manual":
        backend.raw["files"]["power_dpm_force_performance_level"] = "manual"
        backend.raw["files"]["pp_dpm_sclk"] = "0: 300Mhz\n1: 600Mhz\n2: 1500Mhz *"
    return backend, journal, operation


def recovery(backend, journal, **kwargs):
    return BW20AutoClockRecovery(
        backend=backend,
        journal=journal,
        authorization_id="fresh-recovery-window",
        fencing_token=11,
        assert_recovery_control=kwargs.get("assert_recovery_control", lambda *args: None),
        assert_resource_quarantined=kwargs.get(
            "assert_resource_quarantined", lambda *args: None
        ),
    )


@pytest.mark.parametrize("mode,expected_writes", [("manual", 1), ("auto", 0)])
def test_recovery_requires_fresh_claim_and_verified_default_auto(
    tmp_path, mode, expected_writes
):
    backend, journal, operation = unresolved_case(tmp_path, mode=mode)
    receipt = recovery(backend, journal).recover()
    assert receipt.operation_id == operation
    assert receipt.restored is True
    assert receipt.write_requested is (expected_writes == 1)
    assert receipt.hardware_restore_verified is False
    assert len([call for call in backend.calls if call == ("restore_auto",)]) == expected_writes
    assert journal.unresolved(RESOURCE) == []
    attempts = journal.recovery_attempts(RESOURCE)
    assert len(attempts) == 1 and attempts[0]["state"] == "restored"
    assert attempts[0]["authorization_id"] == "fresh-recovery-window"
    assert attempts[0]["fencing_token"] == 11


@pytest.mark.parametrize("guard", ["control", "quarantine"])
def test_recovery_guard_failure_never_claims_or_writes(tmp_path, guard):
    backend, journal, _ = unresolved_case(tmp_path)
    if guard == "control":
        kwargs = {"assert_recovery_control": lambda *args: False}
    else:
        kwargs = {"assert_resource_quarantined": lambda *args: False}
    with pytest.raises(RuntimeError):
        recovery(backend, journal, **kwargs).recover()
    assert backend.calls == []
    assert journal.recovery_attempts(RESOURCE) == []
    assert journal.unresolved(RESOURCE)[0]["state"] == "reconciliation_required"


def test_target_drift_before_claim_never_writes_or_changes_journal(tmp_path):
    backend, journal, _ = unresolved_case(tmp_path)
    backend.raw["boot_id"] = str(uuid4())
    with pytest.raises(ClockJournalError, match="differs"):
        recovery(backend, journal).recover()
    assert backend.calls == []
    assert journal.recovery_attempts(RESOURCE) == []
    assert journal.unresolved(RESOURCE)[0]["state"] == "reconciliation_required"


def test_original_window_authorization_cannot_take_over_recovery(tmp_path):
    backend, journal, _ = unresolved_case(tmp_path)
    coordinator = BW20AutoClockRecovery(
        backend=backend,
        journal=journal,
        authorization_id="original-window",
        fencing_token=11,
        assert_recovery_control=lambda *args: None,
        assert_resource_quarantined=lambda *args: None,
    )
    with pytest.raises(ClockJournalError, match="distinct"):
        coordinator.recover()
    assert backend.calls == []
    assert journal.recovery_attempts(RESOURCE) == []


def test_generic_or_incomplete_policy_is_not_default_auto_recovery_input(tmp_path):
    backend = SimulatedBackend()
    journal = ClockJournal(tmp_path / "clock.sqlite")
    operation = journal.begin(
        resource_id=RESOURCE,
        authorization_id="original-window",
        original={"mode": "auto", "sclk_levels": [1500], "mclk_levels": [1800]},
    )
    journal.transition(
        operation,
        expected="mutation_possible",
        state="reconciliation_required",
    )
    with pytest.raises(ClockJournalError, match="incomplete"):
        recovery(backend, journal).recover()
    assert backend.calls == []
    assert journal.recovery_attempts(RESOURCE) == []


def test_unconfirmed_restore_returns_to_reconciliation_required(tmp_path, monkeypatch):
    backend, journal, _ = unresolved_case(tmp_path)
    monkeypatch.setattr(backend, "restore_default_auto", lambda: None)
    with pytest.raises(ValueError):
        recovery(backend, journal).recover()
    assert journal.unresolved(RESOURCE)[0]["state"] == "reconciliation_required"
    assert journal.recovery_attempts(RESOURCE)[0]["state"] == "failed"


def test_active_recovery_claim_blocks_second_actor(tmp_path, monkeypatch):
    backend, journal, _ = unresolved_case(tmp_path)
    blocked = []
    restore = backend.restore_default_auto

    def attempt_second_claim():
        with pytest.raises(ClockJournalError, match="not available"):
            journal.claim_recovery(
                resource_id=RESOURCE,
                authorization_id="other-recovery-window",
                fencing_token=12,
            )
        blocked.append(True)
        restore()

    monkeypatch.setattr(backend, "restore_default_auto", attempt_second_claim)
    assert recovery(backend, journal).recover().restored
    assert blocked == [True]


def test_lost_recovery_authority_after_write_keeps_resource_quarantined(tmp_path):
    backend, journal, _ = unresolved_case(tmp_path)
    checks = 0

    def authority(*args):
        nonlocal checks
        checks += 1
        return None if checks < 5 else False

    with pytest.raises(RuntimeError, match="fresh recovery control"):
        recovery(backend, journal, assert_recovery_control=authority).recover()
    assert backend.calls == [("restore_auto",)]
    assert journal.unresolved(RESOURCE)[0]["state"] == "reconciliation_required"
    assert journal.recovery_attempts(RESOURCE)[0]["state"] == "failed"


def test_crashed_recovery_claim_remains_unresolved_and_blocks_takeover(tmp_path):
    _, journal, _ = unresolved_case(tmp_path)
    journal.claim_recovery(
        resource_id=RESOURCE,
        authorization_id="crashed-recovery-window",
        fencing_token=11,
    )
    with pytest.raises(ClockJournalError, match="not available"):
        journal.claim_recovery(
            resource_id=RESOURCE,
            authorization_id="second-window",
            fencing_token=12,
        )
    with pytest.raises(ClockJournalError, match="reconciliation"):
        journal.require_clear(RESOURCE)


def test_restore_completion_log_failure_falls_back_to_reconciliation(tmp_path, monkeypatch):
    backend, journal, _ = unresolved_case(tmp_path)
    finish = journal.finish_recovery

    def fail_restored(attempt_id, *, restored):
        if restored:
            raise OSError("durable restored receipt unavailable")
        return finish(attempt_id, restored=False)

    monkeypatch.setattr(journal, "finish_recovery", fail_restored)
    with pytest.raises(OSError, match="durable restored receipt"):
        recovery(backend, journal).recover()
    assert backend.calls == [("restore_auto",)]
    assert journal.unresolved(RESOURCE)[0]["state"] == "reconciliation_required"
    assert journal.recovery_attempts(RESOURCE)[0]["state"] == "failed"


def test_recovery_and_settlement_failures_are_both_preserved(tmp_path, monkeypatch):
    backend, journal, _ = unresolved_case(tmp_path)

    def fail_restore():
        raise ValueError("backend restore failed")

    def fail_settlement(*args, **kwargs):
        raise OSError("journal settlement failed")

    monkeypatch.setattr(backend, "restore_default_auto", fail_restore)
    monkeypatch.setattr(journal, "finish_recovery", fail_settlement)
    with pytest.raises(ClockRecoverySettlementError) as caught:
        recovery(backend, journal).recover()
    assert isinstance(caught.value.recovery_error, ValueError)
    assert str(caught.value.recovery_error) == "backend restore failed"
    assert isinstance(caught.value.settlement_error, OSError)
    assert str(caught.value.settlement_error) == "journal settlement failed"
    assert caught.value.__cause__ is caught.value.recovery_error
    assert journal.unresolved(RESOURCE)[0]["state"] == "recovery_claimed"
