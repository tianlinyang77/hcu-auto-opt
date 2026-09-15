# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import copy
import json
from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_auto_clock_session import (
    BW20AutoClockSession,
    BW20AutoClockSessionFactory,
)
from hcuopt.deployment.bw20_clock_journal import ClockJournal
from hcuopt.deployment.bw20_clock_session import ClockReconciliationRequired
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE


class SimulatedBackend:
    def __init__(self):
        self.raw = dict(host="github-bw20", pci="0000:b1:00.0", boot_id=str(uuid4()),
                        header_sha256="sha256:" + "a" * 64,
                        scope="readonly_non_atomic_observation", files={
                            "numa_node": "4", "power_dpm_force_performance_level": "auto",
                            "pp_dpm_sclk": "0: 300Mhz\n1: 600Mhz *\n2: 1500Mhz",
                            "pp_dpm_mclk": "0: 1800Mhz * (DPM disabled)",
                            "pp_sclk_od": "0", "pp_mclk_od": "0", "pp_gfx_boost": "0"})
        self.calls = []

    def observe(self):
        return copy.deepcopy(self.raw)

    def apply_sclk_level(self, *, level_index):
        self.calls.append(("apply", level_index))
        self.raw["files"]["power_dpm_force_performance_level"] = "manual"
        self.raw["files"]["pp_dpm_sclk"] = "0: 300Mhz\n1: 600Mhz\n2: 1500Mhz *"

    def restore_default_auto(self):
        self.calls.append(("restore_auto",))
        self.raw["files"]["power_dpm_force_performance_level"] = "auto"
        # A different instantaneous idle clock does NOT mean restoration failed.
        self.raw["files"]["pp_dpm_sclk"] = "0: 300Mhz *\n1: 600Mhz\n2: 1500Mhz"


@pytest.fixture
def case(tmp_path):
    backend = SimulatedBackend()
    journal = ClockJournal(tmp_path / "clock.sqlite")
    return backend, journal


def session(case, *, authority=lambda: None, confirmation=lambda baseline: None):
    backend, journal = case
    return BW20AutoClockSession(backend=backend, journal=journal, resource_id=RESOURCE,
                               authorization_id="simulation-only", assert_control=authority,
                               assert_default_baseline=confirmation)


@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt])
def test_scoped_session_uses_existing_transaction_and_restores_changed_idle_clock(case, error):
    backend, journal = case
    transaction = session(case)

    def run():
        with transaction:
            record = journal.unresolved(RESOURCE)[0]
            original = json.loads(record["original_json"])
            assert original["restoration_scope"] == "confirmed_default_auto_v1"
            assert "sclk_levels" not in original  # Not pretending to capture an enabled mask.
            if error:
                raise error("simulated workload exception")

    if error:
        with pytest.raises(error):
            run()
    else:
        run()
    assert backend.calls == [("apply", 2), ("restore_auto",)]
    assert not journal.unresolved(RESOURCE)
    report = transaction.receipt()
    assert report["restored"]
    assert not report["enabled_mask_verified"]
    assert not report["memory_clock_write_requested"]
    assert not report["hardware_restore_verified"]
    assert not report["stage0_accepted"]


@pytest.mark.parametrize("field,value", [
    ("power_dpm_force_performance_level", "manual"), ("pp_sclk_od", "1"),
    ("pp_mclk_od", "1"), ("pp_gfx_boost", "1"),
    ("pp_dpm_mclk", "0: 1800Mhz *"), ("numa_node", "7"),
])
def test_unknown_initial_policy_never_creates_intent_or_writes(case, field, value):
    backend, journal = case
    backend.raw["files"][field] = value
    with pytest.raises(ValueError):
        session(case).__enter__()
    assert backend.calls == []
    assert journal.unresolved(RESOURCE) == []


def test_auto_observation_alone_is_not_default_baseline_confirmation(case):
    with pytest.raises(ValueError):
        session(case, confirmation=lambda baseline: False).__enter__()
    assert case[0].calls == []
    assert case[1].unresolved(RESOURCE) == []


def test_confirmation_callback_required(case):
    with pytest.raises(ValueError):
        session(case, confirmation=None)


def test_level_index_is_not_line_order_or_hardcoded_ten(case):
    case[0].raw["files"]["pp_dpm_sclk"] = "2: 1500Mhz\n0: 300Mhz\n1: 600Mhz *"
    with session(case):
        pass
    assert case[0].calls[0] == ("apply", 2)


def test_partial_apply_failure_uses_original_durable_restoration(case, monkeypatch):
    backend, journal = case
    apply = backend.apply_sclk_level

    def partial(**kwargs):
        assert journal.unresolved(RESOURCE)[0]["state"] == "mutation_possible"
        apply(**kwargs)
        raise RuntimeError("lost acknowledgement")

    monkeypatch.setattr(backend, "apply_sclk_level", partial)
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        session(case).__enter__()
    assert backend.calls[-1] == ("restore_auto",)
    assert journal.unresolved(RESOURCE) == []


def test_restore_acknowledgement_without_auto_observation_is_not_success(case, monkeypatch):
    monkeypatch.setattr(case[0], "restore_default_auto", lambda: None)
    with pytest.raises(ClockReconciliationRequired):
        with session(case):
            pass
    assert case[1].unresolved(RESOURCE)[0]["state"] == "reconciliation_required"


def test_lost_authority_prevents_restore_write(case):
    owned = [True]
    with pytest.raises(ClockReconciliationRequired):
        with session(case, authority=lambda: None if owned[0] else False):
            owned[0] = False
    assert case[0].calls == [("apply", 2)]
    assert case[1].unresolved(RESOURCE)


@pytest.mark.parametrize("change", ["boot", "driver", "table", "memory", "overdrive", "pci"])
def test_changed_target_or_untouched_setting_is_quarantined_without_restore_write(case, change):
    backend, journal = case
    with pytest.raises(ClockReconciliationRequired):
        with session(case):
            if change == "boot":
                backend.raw["boot_id"] = str(uuid4())
            elif change == "driver":
                backend.raw["header_sha256"] = "sha256:" + "b" * 64
            elif change == "table":
                backend.raw["files"]["pp_dpm_sclk"] = "0: 400Mhz\n1: 600Mhz\n2: 1500Mhz *"
            elif change == "memory":
                backend.raw["files"]["pp_dpm_mclk"] = "0: 1700Mhz * (DPM disabled)"
            elif change == "overdrive":
                backend.raw["files"]["pp_sclk_od"] = "1"
            else:
                backend.raw["pci"] = "0000:b2:00.0"
    assert backend.calls == [("apply", 2)]
    assert journal.unresolved(RESOURCE)


def test_baseline_changed_during_intent_commit_never_receives_apply(case, monkeypatch):
    backend, journal = case
    begin = journal.begin

    def changing(**kwargs):
        operation = begin(**kwargs)
        backend.raw["boot_id"] = str(uuid4())
        return operation

    monkeypatch.setattr(journal, "begin", changing)
    with pytest.raises(ClockReconciliationRequired):
        session(case).__enter__()
    assert backend.calls == []
    assert journal.unresolved(RESOURCE)


@pytest.mark.parametrize("outcome", ["success", "workload_error", "lease_loss"])
def test_auto_strategy_composes_with_original_worker_guard(case, outcome):
    from hcuopt.domain.enums import WorkerType
    from hcuopt.storage.repository import _cleanup_is_healthy
    from hcuopt.workers.sdk import Worker

    backend, journal = case
    owned, reports = [True], []
    job = dict(job_id="simulated-job", claim_token="simulated-claim", attempts=1,
               job_type="stage0_probe", fencing_token=1, resource_id=RESOURCE, payload={})

    class Handler:
        def handle(self, kind, payload):
            authority = payload["_job_context"]["assert_live_lease"]
            with session(case, authority=authority):
                if outcome == "workload_error":
                    raise RuntimeError("simulated workload failure")
                if outcome == "lease_loss":
                    owned[0] = False
            return {"cleanup_evidence": self.cleanup()}

        def cleanup(self, *args):
            # Worker must override this when clock restoration was not confirmed.
            return {"fence": {"fenced": True}, "health": {"healthy": True}}

    class Client:
        def register(self, *args):
            pass

        def claim(self, *args):
            return job

        def heartbeat(self, *args):
            if not owned[0]:
                raise RuntimeError("simulated lost lease")

        def check_live_lease(self, *args):
            if not owned[0]:
                raise RuntimeError("simulated lost lease")

        def complete(self, job, result):
            reports.append(("complete", result["cleanup_evidence"]))

        def fail(self, job, error, cleanup):
            reports.append(("fail", cleanup))

    worker = Worker("simulated-worker", WorkerType.GPU, "http://127.0.0.1:1",
                    capabilities={"resource_id": RESOURCE}, handlers=Handler(),
                    resource_guard=journal.require_clear)
    worker.client.client.close()
    worker.client = Client()
    assert worker.run_once() is (outcome == "success")
    assert reports[0][0] == ("complete" if outcome == "success" else "fail")
    assert _cleanup_is_healthy(reports[0][1]) is (outcome != "lease_loss")
    assert bool(journal.unresolved(RESOURCE)) is (outcome == "lease_loss")
    assert backend.calls == (
        [("apply", 2)] if outcome == "lease_loss" else [("apply", 2), ("restore_auto",)]
    )


def test_factory_binds_live_lease_clock_authority_and_same_journal(case, tmp_path):
    backend, journal = case
    calls = []
    context = {
        "resource_id": RESOURCE,
        "lease_scope": "exclusive",
        "fencing_token": 7,
        "assert_live_lease": lambda: calls.append("lease"),
    }
    factory = BW20AutoClockSessionFactory(
        backend=backend,
        journal=journal,
        authorization_id="fixture-authorization",
        assert_clock_authority=lambda actual: calls.append(
            ("authority", actual["fencing_token"])
        ),
        assert_default_baseline=lambda baseline: calls.append(("baseline", baseline.pci)),
    )
    transaction = factory(context, tmp_path)
    assert transaction.journal is journal
    with transaction:
        pass
    assert calls.count("lease") >= 1
    assert ("authority", 7) in calls
    assert ("baseline", "0000:b1:00.0") in calls


@pytest.mark.parametrize(
    "change",
    [
        {"resource_id": "other"},
        {"lease_scope": "shared"},
        {"fencing_token": 0},
        {"fencing_token": True},
        {"assert_live_lease": None},
    ],
)
def test_factory_rejects_untrusted_job_context(case, tmp_path, change):
    context = {
        "resource_id": RESOURCE,
        "lease_scope": "exclusive",
        "fencing_token": 7,
        "assert_live_lease": lambda: None,
    }
    context.update(change)
    factory = BW20AutoClockSessionFactory(
        backend=case[0],
        journal=case[1],
        authorization_id="fixture-authorization",
        assert_clock_authority=lambda actual: None,
        assert_default_baseline=lambda baseline: None,
    )
    with pytest.raises(ValueError):
        factory(context, tmp_path)
