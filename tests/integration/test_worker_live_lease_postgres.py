# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in real PostgreSQL checks in a unique schema; no existing tables are cleared."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from hcuopt.domain.errors import StaleFencingToken
from hcuopt.storage.repository import PostgresRepository

DSN = os.environ.get("HCUOPT_LEASE_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="explicit lease-test DSN required"),
]


@pytest.fixture
def isolated():
    schema = "hcuopt_lease_check_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(DSN, options=f"-c search_path={schema}")
            with psycopg.connect(dsn) as conn:
                conn.execute("""CREATE TABLE jobs (
                    job_id uuid PRIMARY KEY, state text, claim_token uuid, claimed_by text,
                    resource_id text, lease_id uuid)""")
                conn.execute("""CREATE TABLE resources (
                    resource_id text PRIMARY KEY, state text, owner_job_id uuid,
                    lease_id uuid, fencing_token integer, expires_at timestamptz,
                    cleanup_evidence jsonb, updated_at timestamptz DEFAULT now())""")
                job, claim, lease = uuid4(), uuid4(), uuid4()
                conn.execute("INSERT INTO jobs VALUES (%s,'running',%s,'worker','fixture',%s)",
                             (job, claim, lease))
                conn.execute("""INSERT INTO resources
                    (resource_id,state,owner_job_id,lease_id,fencing_token,expires_at) VALUES
                    ('fixture','active',%s,%s,1,clock_timestamp()+interval '90 seconds')""",
                             (job, lease))
            yield PostgresRepository(dsn), job, claim
        finally:
            # The generated schema is the only deletion target; no shared tables or migrations.
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_active_lease_is_not_renewed(isolated):
    repo, job, claim = isolated
    with repo.connection() as conn:
        before = conn.execute("SELECT expires_at FROM resources").fetchone()["expires_at"]
    repo.assert_live_job_lease("worker", job, claim, 1)
    with repo.connection() as conn:
        after = conn.execute("SELECT expires_at FROM resources").fetchone()["expires_at"]
    assert after == before


@pytest.mark.parametrize("mutation", [
    "expires_at=clock_timestamp()-interval '1 second'", "state='quarantined'",
    "fencing_token=2", "lease_id=NULL",
])
def test_real_database_rejects_invalid_lease(isolated, mutation):
    repo, job, claim = isolated
    with repo.connection() as conn:
        # Only hard-coded test cases above; never request-supplied SQL.
        conn.execute("UPDATE resources SET " + mutation)
    with pytest.raises(StaleFencingToken):
        repo.assert_live_job_lease("worker", job, claim, 1)


def test_expiry_is_rechecked_after_waiting_for_resource_lock(isolated):
    repo, job, claim = isolated
    app_name = "lease_wait_" + uuid4().hex
    waiting = PostgresRepository(make_conninfo(repo.database_url, application_name=app_name,
                                               options=psycopg.conninfo.conninfo_to_dict(
                                                   repo.database_url)["options"]
                                               + " -c statement_timeout=5000"))
    with psycopg.connect(repo.database_url) as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute("SELECT * FROM resources FOR UPDATE")
        future = pool.submit(waiting.assert_live_job_lease, "worker", job, claim, 1)
        try:
            deadline = time.monotonic() + 3
            with psycopg.connect(repo.database_url, autocommit=True) as observer:
                while True:
                    row = observer.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE application_name=%s",
                        (app_name,),
                    ).fetchone()
                    if row and row[0] == "Lock":
                        break
                    if time.monotonic() >= deadline:
                        pytest.fail("checker did not reach the real PostgreSQL lock wait")
                    time.sleep(.02)
            blocker.execute("UPDATE resources SET expires_at=clock_timestamp()-interval '1 second'")
            blocker.commit()
            with pytest.raises(StaleFencingToken):
                future.result(timeout=5)
        finally:
            blocker.rollback()


def test_http_client_checks_real_database_without_renewing(isolated, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from hcuopt.api.app import create_app
    from hcuopt.workers.sdk import ControlPlaneClient

    repo, job, claim = isolated
    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "false")
    client = ControlPlaneClient("http://testserver")
    client.client.close()
    wire_job = dict(job_id=str(job), claim_token=str(claim), fencing_token=1)
    with TestClient(create_app(repository=repo)) as api:
        client.client = api
        client.check_live_lease("worker", wire_job)
        with repo.connection() as conn:
            conn.execute("UPDATE resources SET state='quarantined'")
        with pytest.raises(httpx.HTTPStatusError) as exc:
            client.check_live_lease("worker", wire_job)
        assert exc.value.response.status_code == 409
        assert exc.value.response.json()["code"] == "stale_fencing_token"


@pytest.mark.parametrize("missing_journal", [False, True])
def test_clock_guard_worker_settlement_and_http_cleanup_use_real_database(
    isolated, tmp_path, monkeypatch, missing_journal
):
    """Real Worker cleanup -> repository settlement -> HTTP late-report, no hardware.

    The minimal schema tests resource transitions, not full task orchestration.
    Clock changes/restoration are simulated; no GPU or driver API is called.
    """
    from fastapi.testclient import TestClient

    from hcuopt.api.app import create_app
    from hcuopt.deployment.bw20_clock_journal import ClockJournal
    from hcuopt.domain.enums import WorkerType
    from hcuopt.workers.sdk import Worker

    repo, job_id, claim = isolated
    journal = ClockJournal(tmp_path / "clock.sqlite")
    operation = journal.begin(resource_id="fixture", authorization_id="cpu-test-only",
                              original={"mode": "auto"})
    if missing_journal:
        journal.path.unlink()  # Test-owned temporary file only.

    class Handler:
        def cleanup(self, *args):
            return {"fence": {"fenced": True}, "health": {"healthy": True}}

    worker = Worker("worker", WorkerType.GPU, "http://testserver", handlers=Handler(),
                    capabilities={"resource_id": "fixture"},
                    resource_guard=journal.require_clear)
    worker.client.client.close()
    job = dict(job_id=job_id, claim_token=claim, fencing_token=1,
               resource_id="fixture", job_type="stage0_probe")
    cleanup = worker._cleanup_job(job, {})
    assert cleanup["health"]["healthy"] is False
    with repo.connection() as conn:
        repo._release_resource(conn, job, "cpu-clock-test", cleanup)

    def row():
        with repo.connection() as conn:
            return conn.execute("SELECT * FROM resources WHERE resource_id='fixture'").fetchone()

    assert row()["state"] == "quarantined"
    assert row()["owner_job_id"] is None
    assert row()["cleanup_evidence"]["resource_guard"]["clear"] is False
    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "false")
    with TestClient(create_app(repository=repo, auto_migrate=False)) as api:
        worker.client.client = api
        cached_healthy = Handler().cleanup()
        # Failed primary reports use this path; it must not forward cached healthy evidence.
        worker._report_cleanup_after_handler_exit(job, cached_healthy)
        assert row()["state"] == "quarantined"
        assert row()["cleanup_evidence"]["health"]["healthy"] is False
        assert cached_healthy["health"]["healthy"] is True
        if missing_journal:
            assert not journal.path.exists()
            return
        # Simulates an independently verified restoration; NOT hardware recovery acceptance.
        journal.transition(operation, expected="mutation_possible", state="restoring")
        journal.transition(operation, expected="restoring", state="restored")
        worker._report_cleanup_after_handler_exit(job, cached_healthy)
        assert row()["state"] == "available"
        assert row()["cleanup_evidence"]["resource_guard"]["clear"] is True


def test_clock_cleanup_cannot_release_a_newer_fence(isolated, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from hcuopt.api.app import create_app
    from hcuopt.workers.sdk import ControlPlaneClient

    repo, _, _ = isolated
    with repo.connection() as conn:
        conn.execute("UPDATE resources SET state='quarantined',fencing_token=2")
    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "false")
    client = ControlPlaneClient("http://testserver")
    client.client.close()
    with TestClient(create_app(repository=repo, auto_migrate=False)) as api:
        client.client = api
        with pytest.raises(httpx.HTTPStatusError) as error:
            client.report_cleanup("fixture", 1, {
                "fence": {"fenced": True}, "health": {"healthy": True}})
        assert error.value.response.status_code == 409
        assert error.value.response.json()["code"] == "stale_fencing_token"
    with repo.connection() as conn:
        row = conn.execute("SELECT state,fencing_token FROM resources").fetchone()
    assert row == {"state": "quarantined", "fencing_token": 2}
