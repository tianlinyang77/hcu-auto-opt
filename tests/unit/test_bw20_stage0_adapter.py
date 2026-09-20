# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.deployment.bw20_stage0_adapter import BW20Stage0ProbeAdapter
from hcuopt.deployment.bw20_stage0_harness import HOST_CLOCK, PROFILE
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.domain.enums import Stage0ProbeType, WorkerType
from hcuopt.evaluation.stage0_verifier import Stage0ProbeEvidenceReference
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.measurement.stage0 import Stage0ProbeOutput
from hcuopt.targets import target_fingerprint
from hcuopt.workers.sdk import Worker
from tests.unit.test_bw20_stage0_runtime import TARGET
from tests.unit.test_bw20_stage0_telemetry import Runner


class Host(Runner):
    def __init__(self):
        super().__init__()
        self.ticks = 100
        self.clock_failure = False

    def run(self, argv, timeout):
        if argv[-1] == HOST_CLOCK:
            if self.clock_failure:
                raise RuntimeError("host clock failed")
            self.ticks += 1
            return SimpleNamespace(
                returncode=0,
                stderr=b"",
                stdout=json.dumps(
                    dict(host="github-bw20", boot_id=self.raw["boot_id"], monotonic_ns=self.ticks)
                ).encode(),
            )
        return super().run(argv, timeout)


class Clock:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.restored = False

    def __enter__(self):
        self.events.append("clock_enter")
        return self

    def __exit__(self, *args):
        self.events.append("clock_restore")
        self.restored = True

    def receipt(self):
        return {"state": "restored" if self.restored else "active",
                "restored": self.restored, "quarantined": not self.restored,
                "stage0_accepted": False, "automatic_release_allowed": False,
                "hardware_restore_verified": False}


def payload():
    return dict(
        probe_type="fingerprint",
        mode="formal",
        protocol_version="s0-g0-v2",
        adapter_profile=PROFILE,
        task_id=str(uuid4()),
        stage0_run_id=str(uuid4()),
        target_snapshot_id=str(uuid4()),
        workload_id="fixture",
        target=TARGET.model_dump(mode="json"),
        target_fingerprint=target_fingerprint(TARGET),
        _job_context=dict(
            job_id=str(uuid4()),
            lease_id=str(uuid4()),
            fencing_token=7,
            resource_id=RESOURCE,
            lease_scope="exclusive",
            assert_live_lease=lambda: None,
        ),
    )


def adapter(host, admission=lambda p: None):
    return BW20Stage0ProbeAdapter(
        target=TARGET,
        runner=host,
        assert_admission=admission,
        session_builder=lambda *args: lambda *a: pytest.fail("fingerprint must not start HCU"),
        clock_session_factory=lambda *args: Clock(),
    )


@pytest.mark.parametrize("failure", [None, "clock", "admission", "health"])
def test_original_worker_routes_completion_or_failure_to_job_cleanup(tmp_path, failure):
    host = Host()
    host.clock_failure = failure == "clock"
    if failure == "health":
        host.raw["files"]["mem_info_vram_used"] = str(40 * 1024**3)

    def admission(p):
        if failure == "admission":
            raise MeasurementSafetyError("target blocked")

    probe = adapter(host, admission)
    registry = AdapterRegistry(profile=PROFILE, stage0_probe=probe)
    p = payload()
    context = p.pop("_job_context")
    job = dict(
        job_type="stage0_probe",
        payload=p,
        attempts=1,
        claim_token=str(uuid4()),
        **{k: v for k, v in context.items() if k != "assert_live_lease"},
    )

    class Client:
        result = error = cleanup = None

        def register(self, *args):
            pass

        def claim(self, *args):
            return job

        def heartbeat(self, *args):
            pass

        def check_live_lease(self, *args):
            pass

        def complete(self, job, result):
            self.result = result

        def fail(self, job, exc, cleanup):
            self.error, self.cleanup = exc, cleanup

    client = Client()
    worker = Worker(
        "bw20-fixture",
        WorkerType.BUILD,
        "http://127.0.0.1:1",
        adapters=registry,
        output_dir=tmp_path,
    )
    worker.client.client.close()
    worker.client = client
    assert worker.run_once() is (failure is None)
    if failure:
        assert client.result is None and client.error is not None
        assert client.cleanup["health"]["healthy"] is (failure == "clock")
    else:
        assert client.result["cleanup_evidence"]["health"]["healthy"]
        assert client.result["cleanup_evidence"]["diagnostics"]["sha256"].startswith("sha256:")
        reference = Stage0ProbeEvidenceReference(
            probe_record_id=uuid4(),
            probe_type=Stage0ProbeType(client.result["probe_type"]),
            lease_id=UUID(context["lease_id"]),
            **{key: client.result[key] for key in (
                "raw_evidence_uri", "raw_evidence_hash",
                "adapter_provenance", "synthetic", "cleanup_evidence",
            )},
            **{key: context[key] for key in ("resource_id", "fencing_token")},
        )
        assert reference.cleanup_evidence["health"]["resource_id"] == RESOURCE
    files = list(tmp_path.rglob("diagnostics.json"))
    assert len(files) == 1
    evidence = json.loads(files[0].read_text())
    assert evidence["stage0_accepted"] is False
    assert evidence["automatic_release_allowed"] is False
    assert not evidence.get("sessions")


def test_unknown_or_stale_context_cannot_report_healthy(tmp_path):
    probe = adapter(Host())
    p = payload()
    assert not probe.cleanup_probe(p)["health"]["healthy"]
    probe.run_probe(p, tmp_path)
    p["_job_context"]["fencing_token"] = 8
    assert not probe.cleanup_probe(p)["health"]["healthy"]


def test_duplicate_context_does_not_reexecute_probe(tmp_path):
    host = Host()
    probe = adapter(host)
    p = payload()
    probe.run_probe(p, tmp_path)
    calls = len(host.calls)
    with pytest.raises(MeasurementSafetyError, match="already exists"):
        probe.run_probe(p, tmp_path)
    assert len(host.calls) == calls


def test_lease_loss_during_original_worker_handler_is_reported_with_scoped_cleanup(tmp_path):
    host = Host()
    probe = adapter(host)
    p = payload()
    context = p.pop("_job_context")
    job = dict(job_type="stage0_probe", payload=p, attempts=1, claim_token=str(uuid4()),
               **{k: v for k, v in context.items() if k != "assert_live_lease"})
    class Client:
        checks = 0
        failed = None
        def register(self, *args):
            pass
        def claim(self, *args):
            return job
        def heartbeat(self, *args):
            pass
        def check_live_lease(self, *args):
            self.checks += 1
            if self.checks > 1:
                raise RuntimeError("live lease expired")
        def complete(self, *args):
            pytest.fail("lost lease must not complete")
        def fail(self, job, exc, cleanup):
            self.failed = cleanup
    worker = Worker("bw20-fixture", WorkerType.BUILD, "http://127.0.0.1:1",
        adapters=AdapterRegistry(profile=PROFILE, stage0_probe=probe), output_dir=tmp_path)
    worker.client.client.close()
    client = Client()
    worker.client = client
    assert not worker.run_once()
    assert client.checks == 2 and client.failed["fence"]["fenced"]
    assert client.failed["health"]["healthy"]
    # No resource-wide cleaner was installed, so success proves the scoped route.
    assert not worker.handlers.adapters.resource_cleaner


def _stub_timing_probe(tmp_path, clock):
    probe = BW20Stage0ProbeAdapter(
        target=TARGET,
        runner=Host(),
        assert_admission=lambda p: None,
        session_builder=lambda *args: None,
        clock_session_factory=lambda *args: clock,
    )
    p = payload()
    p["probe_type"] = "timer"
    artifact = tmp_path / "raw.json"
    artifact.write_text("{}")
    return probe, p, Stage0ProbeOutput(
        summary={},
        raw_evidence_uri=artifact.as_uri(),
        raw_evidence_hash="sha256:" + "a" * 64,
        cleanup_evidence=None,
    )


def test_timing_probe_cleans_workload_before_restoring_clock(tmp_path, monkeypatch):
    events = []
    clock = Clock(events)
    probe, p, output = _stub_timing_probe(tmp_path, clock)

    def execute(*args):
        events.append("probe")
        return output

    def cleanup(*args):
        events.append("container_cleanup")
        return {"fence": {"fenced": True}, "health": {"healthy": True}}

    monkeypatch.setattr(probe, "_execute_probe", execute)
    monkeypatch.setattr(probe, "cleanup_probe", cleanup)
    result = probe.run_probe(p, tmp_path)
    assert events == ["clock_enter", "probe", "container_cleanup", "clock_restore"]
    assert result.cleanup_evidence["clock"]["restored"] is True


def test_fingerprint_never_constructs_clock_session(tmp_path, monkeypatch):
    probe = adapter(Host())
    monkeypatch.setattr(
        probe,
        "clock_session_factory",
        lambda *args: pytest.fail("fingerprint must remain read only"),
    )
    result = probe.run_probe(payload(), tmp_path)
    assert result.cleanup_evidence["health"]["healthy"] is True


@pytest.mark.parametrize("receipt", [None, {}, {"restored": False, "quarantined": True}])
def test_unconfirmed_clock_receipt_cannot_report_success(tmp_path, monkeypatch, receipt):
    events = []
    clock = Clock(events)
    clock.receipt = lambda: receipt
    probe, p, output = _stub_timing_probe(tmp_path, clock)
    monkeypatch.setattr(probe, "_execute_probe", lambda *args: output)
    monkeypatch.setattr(
        probe,
        "cleanup_probe",
        lambda *args: {"fence": {"fenced": True}, "health": {"healthy": True}},
    )
    with pytest.raises(MeasurementSafetyError, match="requires reconciliation"):
        probe.run_probe(p, tmp_path)
    diagnostic = json.loads(next(tmp_path.rglob("diagnostics.json")).read_text())
    assert diagnostic["cleanup_evidence"]["health"]["healthy"] is False
    assert diagnostic["cleanup_evidence"]["health"]["quarantined"] is True
    monkeypatch.undo()
    assert probe.cleanup_probe(p)["health"]["healthy"] is False


def test_workload_failure_still_cleans_before_clock_restore(tmp_path, monkeypatch):
    events = []
    probe, p, _ = _stub_timing_probe(tmp_path, Clock(events))

    def fail(*args):
        events.append("probe_failed")
        raise RuntimeError("fixture workload failed")

    monkeypatch.setattr(probe, "_execute_probe", fail)
    monkeypatch.setattr(
        probe,
        "cleanup_probe",
        lambda *args: events.append("container_cleanup")
        or {"fence": {"fenced": True}, "health": {"healthy": True}},
    )
    with pytest.raises(RuntimeError, match="fixture workload failed"):
        probe.run_probe(p, tmp_path)
    assert events == [
        "clock_enter",
        "probe_failed",
        "container_cleanup",
        "clock_restore",
    ]


def test_clock_restore_failure_preserves_unhealthy_diagnostics(tmp_path, monkeypatch):
    events = []

    class RestoreFails(Clock):
        def __exit__(self, *args):
            self.events.append("clock_restore_failed")
            raise RuntimeError("restore not confirmed")

    probe, p, output = _stub_timing_probe(tmp_path, RestoreFails(events))
    monkeypatch.setattr(probe, "_execute_probe", lambda *args: output)
    monkeypatch.setattr(
        probe,
        "cleanup_probe",
        lambda *args: events.append("container_cleanup")
        or {"fence": {"fenced": True}, "health": {"healthy": True}},
    )
    with pytest.raises(RuntimeError, match="restore not confirmed"):
        probe.run_probe(p, tmp_path)
    assert events == ["clock_enter", "container_cleanup", "clock_restore_failed"]
    diagnostic = json.loads(next(tmp_path.rglob("diagnostics.json")).read_text())
    assert diagnostic["clock"]["restored"] is False
    assert diagnostic["clock"]["quarantined"] is True
    assert diagnostic["cleanup_evidence"]["health"]["healthy"] is False


def test_clock_receipt_failure_is_recorded_as_reconciliation(tmp_path, monkeypatch):
    class ReceiptFails(Clock):
        def receipt(self):
            raise RuntimeError("receipt unavailable")

    probe, p, output = _stub_timing_probe(tmp_path, ReceiptFails())
    monkeypatch.setattr(probe, "_execute_probe", lambda *args: output)
    monkeypatch.setattr(
        probe,
        "cleanup_probe",
        lambda *args: {"fence": {"fenced": True}, "health": {"healthy": True}},
    )
    with pytest.raises(MeasurementSafetyError, match="requires reconciliation"):
        probe.run_probe(p, tmp_path)
    diagnostic = json.loads(next(tmp_path.rglob("diagnostics.json")).read_text())
    assert diagnostic["clock"]["state"] == "receipt_failed"
    assert diagnostic["clock"]["error_type"] == "RuntimeError"
    assert diagnostic["cleanup_evidence"]["health"]["healthy"] is False
