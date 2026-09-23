# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Rehearse the observed v26 deployment upgrade in a random isolated schema."""

import os
from uuid import uuid4

import psycopg
import pytest

from hcuopt.deployment.formal_schema_check import inspect_formal_schema
from hcuopt.storage import repository as module
from hcuopt.storage.repository import PostgresRepository
from tests.integration.test_formal_start_management_postgres import isolated_dsn  # noqa: F401

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


@pytest.mark.parametrize("injected_failure", [False, True])
def test_v26_upgrade_preserves_legacy_jobs_and_rolls_back_atomically(
    request, monkeypatch, injected_failure,
):
    plan = list(module.migration_plan())
    with monkeypatch.context() as patch:
        patch.setattr(module, "migration_plan", lambda: [(v, s) for v, s in plan if v <= 26])
        dsn = request.getfixturevalue("isolated_dsn")
    repo = PostgresRepository(dsn)
    preflight = inspect_formal_schema(dsn)
    assert preflight["schema_checks_passed"] is False
    assert "migration_sequence_mismatch" in preflight["blockers"]
    task_id = uuid4()
    with repo.connection() as conn:
        conn.execute(
            "INSERT INTO tasks(task_id,name,workload_id,idempotency_key,state) "
            "VALUES (%s,'upgrade fixture','fixture',%s,'completed')", (task_id, str(task_id)),
        )
        for state in ("queued", "running", "succeeded", "failed"):
            job_id = uuid4()
            conn.execute(
                "INSERT INTO jobs(job_id,task_id,job_type,accepted_worker_type,payload,"
                "idempotency_key,state) VALUES (%s,%s,'manual_correctness','gpu',"
                "'{\"fixture\": true}',%s,%s)", (job_id, task_id, str(job_id), state),
            )
        before = conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
        assert conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"] == 26
    if injected_failure:
        with monkeypatch.context() as patch:
            patch.setattr(module, "migration_plan", lambda: [
                (v, s + ("\nSELECT 1 / 0;" if v == 30 else "")) for v, s in plan
            ])
            with pytest.raises(psycopg.errors.DivisionByZero):
                repo.migrate()
        with repo.connection() as conn:
            assert conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()[
                "v"
            ] == 26
            assert conn.execute("SELECT to_regclass('formal_round_dispatches') AS t").fetchone()[
                "t"
            ] is None
            assert conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall() == before
    repo.migrate()
    repo.migrate()
    preflight = inspect_formal_schema(dsn)
    assert preflight["schema_checks_passed"] is True
    assert preflight["execution_authorized"] is False
    with repo.connection() as conn:
        after = conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
        assert all(row.pop("execution_lane") == "general" for row in after)
        assert after == before
        versions = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert [row["version"] for row in versions] == [v for v, _ in plan]
        for table in ("formal_round_dispatches", "formal_dispatch_claims", "formal_build_journal"):
            assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
    with pytest.raises(psycopg.errors.RaiseException, match="lane is immutable"):
        with repo.connection() as conn:
            conn.execute("UPDATE jobs SET execution_lane='formal' WHERE task_id=%s", (task_id,))
