# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from hcuopt.domain.errors import Conflict
from hcuopt.storage import formal_build_jobs
from hcuopt.storage.formal_build_jobs import PostgresFormalBuildJobs
from hcuopt.storage.migrations import migration_sql


@pytest.fixture
def claim_case(monkeypatch):
    jobs = PostgresFormalBuildJobs(SimpleNamespace(), uuid4(), "builder", uuid4())
    candidate_id, job_id = uuid4(), uuid4()
    job = {
        "job_id": job_id, "execution_lane": "formal", "job_type": "manual_build",
        "accepted_worker_type": "build",
        "payload": jobs.binding(candidate_id), "state": "queued", "attempts": 0,
        "max_attempts": 1, "lease_scope": "none", "adapter_profile": "test-profile",
    }
    worker = {"worker_type": "build", "adapter_profile": "test-profile"}
    conn = Mock()
    conn.execute.side_effect = [
        SimpleNamespace(fetchone=lambda: job), SimpleNamespace(fetchone=lambda: worker), None,
    ]
    insert = Mock()
    monkeypatch.setattr(formal_build_jobs, "_insert", insert)
    reservation = {"job_id": job_id, "candidate_id": candidate_id, "attempt": 1}
    return jobs, conn, reservation, job, worker, insert


def test_claim_uses_same_transaction_and_bound_owner(claim_case):
    jobs, conn, reservation, _, _, insert = claim_case
    jobs._claim_reserved(conn, reservation)
    sql, parameters = conn.execute.call_args.args
    assert "state = 'running'" in sql and "attempts = 1" in sql
    assert parameters == (jobs.worker_id, jobs.claim_token, reservation["job_id"])
    assert insert.call_args.args[0] is conn
    assert insert.call_args.args[2]["event_type"] == "formal_build_claimed"


@pytest.mark.parametrize("change", [
    {"execution_lane": "general"}, {"state": "running"}, {"attempts": 1},
    {"max_attempts": 3}, {"payload": {}}, {"lease_scope": "exclusive"},
    {"accepted_worker_type": "gpu"},
])
def test_nonmatching_job_is_not_claimed(claim_case, change):
    jobs, conn, reservation, job, _, insert = claim_case
    job.update(change)
    with pytest.raises(Conflict, match="isolated queued Job"):
        jobs._claim_reserved(conn, reservation)
    assert conn.execute.call_count == 1
    insert.assert_not_called()


@pytest.mark.parametrize("change", [{"worker_type": "gpu"}, {"adapter_profile": "other"}])
def test_nonmatching_worker_is_not_claimed(claim_case, change):
    jobs, conn, reservation, _, worker, insert = claim_case
    worker.update(change)
    with pytest.raises(Conflict, match="matching CPU Worker"):
        jobs._claim_reserved(conn, reservation)
    assert conn.execute.call_count == 2
    insert.assert_not_called()


def test_migration_keeps_old_jobs_general_and_freezes_lane():
    sql = migration_sql(32)
    assert "DEFAULT 'general'" in sql
    assert "NEW.execution_lane IS DISTINCT FROM OLD.execution_lane" in sql
    assert "NEW.payload IS DISTINCT FROM OLD.payload" in sql
