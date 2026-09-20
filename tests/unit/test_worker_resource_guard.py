# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import sqlite3
from types import SimpleNamespace

import pytest

from hcuopt.deployment.bw20_clock_journal import ClockJournal
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.domain.enums import WorkerType
from hcuopt.storage.repository import PostgresRepository, _cleanup_is_healthy
from hcuopt.workers.sdk import Worker


def healthy():
    return {"fence": {"fenced": True}, "health": {"healthy": True}}


@pytest.fixture
def case(tmp_path):
    journal = ClockJournal(tmp_path / "clock.sqlite")
    calls = []
    job = dict(job_id="job", claim_token="claim", attempts=1, fencing_token=3,
               resource_id=RESOURCE, job_type="stage0_probe", payload={})

    def begin():
        return journal.begin(resource_id=RESOURCE, authorization_id="fixture",
                             original={"mode": "auto"})

    class Handler:
        def handle(self, *args):
            calls.append("handle")
            return {"cleanup_evidence": healthy()}

        def cleanup(self, *args):
            calls.append("cleanup")
            return healthy()

    class Client:
        def register(self, *args):
            calls.append("register")

        def claim(self, *args):
            calls.append("claim")
            return job

        def heartbeat(self, *args):
            pass

        def complete(self, *args):
            calls.append("complete")

        def fail(self, job, exc, cleanup):
            calls.append(("fail", cleanup))

        def report_cleanup(self, resource_id, fencing_token, cleanup):
            calls.append(("report", cleanup))

    worker = Worker("fixture", WorkerType.GPU, "http://127.0.0.1:1",
                    capabilities={"resource_id": RESOURCE}, handlers=Handler(),
                    resource_guard=journal.require_clear)
    worker.client.client.close()
    worker.client = Client()
    return SimpleNamespace(journal=journal, calls=calls, job=job, begin=begin, worker=worker)


def test_clear_journal_keeps_original_worker_path(case):
    assert case.worker.run_once()
    assert case.calls == ["register", "claim", "handle", "complete"]


def test_unresolved_intent_blocks_registration_and_claim_after_restart(case):
    case.begin()
    case.worker.resource_guard = ClockJournal(case.journal.path).require_clear
    assert not case.worker.run_once()
    assert case.calls == []


def test_intent_created_during_claim_blocks_handler_and_reports_unhealthy(case, monkeypatch):
    def claim(*args):
        case.begin()
        return case.job

    monkeypatch.setattr(case.worker.client, "claim", claim)
    assert not case.worker.run_once()
    assert "handle" not in case.calls
    assert "complete" not in case.calls
    assert not _cleanup_is_healthy(case.calls[-1][1])


@pytest.mark.parametrize("handler_raises", [False, True])
def test_handler_cannot_hide_pending_clock_with_healthy_cleanup(case, monkeypatch, handler_raises):
    def handle(*args):
        case.begin()
        if handler_raises:
            raise RuntimeError("workload failed")
        return {"cleanup_evidence": healthy()}

    monkeypatch.setattr(case.worker.handlers, "handle", handle)
    assert not case.worker.run_once()
    assert "complete" not in case.calls
    cleanup = case.calls[-1][1]
    assert cleanup["health"] == {"healthy": False, "quarantined": True}
    assert cleanup["fence"]["fenced"] is True
    assert cleanup["resource_guard"]["clear"] is False

    # Exercise the original repository's settlement method, without a DB server.
    # This checks state selection only; it is not PostgreSQL concurrency acceptance.
    statements = []

    class Connection:
        def execute(self, sql, parameters):
            statements.append((sql, parameters))
            return SimpleNamespace(rowcount=1)

    PostgresRepository._release_resource(None, Connection(), case.job, "failed", cleanup)
    assert statements[-1][1][0] == "quarantined"


def test_original_policy_restored_before_completion_is_allowed(case, monkeypatch):
    def handle(*args):
        operation = case.begin()
        case.journal.transition(operation, expected="mutation_possible", state="restoring")
        case.journal.transition(operation, expected="restoring", state="restored")
        return {"cleanup_evidence": healthy()}

    monkeypatch.setattr(case.worker.handlers, "handle", handle)
    assert case.worker.run_once()
    assert case.calls[-1] == "complete"


def test_late_cleanup_report_rechecks_journal(case):
    cached = healthy()
    case.begin()
    case.worker._report_cleanup_after_handler_exit(case.job, cached)
    assert case.calls[-1][0] == "report"
    assert not _cleanup_is_healthy(case.calls[-1][1])
    assert _cleanup_is_healthy(cached)  # Never mutate the handler's cached evidence.


def test_missing_journal_is_not_recreated_by_guard(case):
    case.journal.path.unlink()
    assert not case.worker.run_once()
    assert case.calls == []
    assert not case.journal.path.exists()
    with pytest.raises(sqlite3.OperationalError):
        case.journal.require_clear(RESOURCE)


def test_unknown_or_nonconfirming_guard_fails_closed(case):
    case.worker.resource_guard = lambda resource_id: False
    assert not case.worker.run_once()
    assert case.calls == []


def test_payload_guard_cannot_override_deployment_guard(case, monkeypatch):
    case.job["payload"]["resource_guard"] = lambda resource_id: None

    def claim(*args):
        case.begin()
        return case.job

    monkeypatch.setattr(case.worker.client, "claim", claim)
    assert not case.worker.run_once()
    assert "handle" not in case.calls


def test_lost_lease_cleanup_cannot_promote_health(case):
    case.begin()
    # Same cleanup function used by heartbeat loss; no automatic restore writes.
    cleanup = case.worker._cleanup_job(case.job, {})
    assert not _cleanup_is_healthy(cleanup)
    assert cleanup["health"]["quarantined"] is True


def test_failed_failure_report_falls_back_with_unhealthy_evidence(case, monkeypatch):
    def handle(*args):
        case.begin()
        raise RuntimeError("lost lease")

    def fail(*args):
        raise RuntimeError("stale fencing token")

    monkeypatch.setattr(case.worker.handlers, "handle", handle)
    monkeypatch.setattr(case.worker.client, "fail", fail)
    assert not case.worker.run_once()
    assert case.calls[-1][0] == "report"
    assert not _cleanup_is_healthy(case.calls[-1][1])


def test_journal_error_during_cleanup_is_unhealthy(case):
    case.journal.path.unlink()
    cleanup = case.worker._cleanup_job(case.job, {})
    assert not _cleanup_is_healthy(cleanup)
    assert cleanup["resource_guard"]["error_type"] == "OperationalError"


def test_guard_never_promotes_failed_handler_cleanup(case, monkeypatch):
    monkeypatch.setattr(case.worker.handlers, "cleanup", lambda *args: {
        "fence": {"fenced": False}, "health": {"healthy": False}})
    cleanup = case.worker._cleanup_job(case.job, {})
    assert cleanup["resource_guard"]["clear"] is True
    assert not _cleanup_is_healthy(cleanup)
