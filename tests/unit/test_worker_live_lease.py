# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import StaleClaimToken, StaleFencingToken
from hcuopt.storage.repository import PostgresRepository
from hcuopt.workers.sdk import ControlPlaneClient, Worker


@pytest.fixture
def repo_rows():
    job_id, lease_id, claim = uuid4(), uuid4(), uuid4()
    job = dict(job_id=job_id, state="running", claim_token=claim, claimed_by="worker",
               resource_id="bw20", lease_id=lease_id)
    resource = dict(state="active", live_now=True, owner_job_id=job_id,
                    lease_id=lease_id, fencing_token=3)
    calls = []

    class Connection:
        def execute(self, sql, args):
            calls.append(sql)
            assert sql.strip().startswith("SELECT")
            return SimpleNamespace(fetchone=lambda: job if "FROM jobs" in sql else resource)

    class Repository(PostgresRepository):
        def migrate(self):
            pass

        @contextmanager
        def connection(self):
            yield Connection()

    return Repository("unused"), job, resource, calls


def check(rows, worker="worker", fence=3):
    repo, job, _, _ = rows
    repo.assert_live_job_lease(worker, job["job_id"], job["claim_token"], fence)


def test_original_rows_checked_without_renewal(repo_rows):
    check(repo_rows)
    assert all("FOR UPDATE" in sql for sql in repo_rows[3])
    assert "clock_timestamp()" in repo_rows[3][-1]


@pytest.mark.parametrize("field,value", [
    ("state", "quarantined"), ("state", "available"), ("live_now", False),
    ("live_now", None), ("lease_id", uuid4()), ("owner_job_id", uuid4()),
    ("fencing_token", 4),
])
def test_inactive_expired_or_changed_resource_rejected(repo_rows, field, value):
    repo_rows[2][field] = value
    with pytest.raises(StaleFencingToken):
        check(repo_rows)


def test_wrong_worker_or_terminal_job_rejected(repo_rows):
    with pytest.raises(StaleClaimToken):
        check(repo_rows, worker="other")
    repo_rows[1]["state"] = "succeeded"
    with pytest.raises(StaleClaimToken):
        check(repo_rows)


def test_api_and_client_reuse_claim_contract(repo_rows):
    from fastapi.testclient import TestClient

    from hcuopt.api.app import create_app

    repo, job, _, _ = repo_rows
    app = create_app(repository=repo)
    client = ControlPlaneClient("http://testserver")
    client.client.close()
    wire_job = {**job, "claim_token": str(job["claim_token"]), "fencing_token": 3}
    with TestClient(app) as api:
        client.client = api
        client.check_live_lease("worker", wire_job)
        repo_rows[2]["live_now"] = False
        with pytest.raises(httpx.HTTPStatusError):
            client.check_live_lease("worker", wire_job)


@pytest.mark.parametrize("lost", [False, True])
def test_worker_injects_bound_capability_and_latches_failure(lost):
    checks, contexts = [], []

    class Handler:
        def handle(self, kind, payload):
            context = payload["_job_context"]
            contexts.append(context)
            context["fencing_token"] = 999  # Cannot retarget the SDK's captured authority.
            context["assert_live_lease"]()
            return {}

        def cleanup(self, *args):
            return {}

    class Client:
        def register(self, *args):
            pass

        def claim(self, *args):
            return dict(job_id="job", job_type="source_prepare", attempts=1,
                        payload={"_job_context": {"assert_live_lease": "untrusted"}},
                        claim_token="claim", fencing_token=3)

        def heartbeat(self, *args):
            pass

        def check_live_lease(self, worker, job):
            checks.append(dict(job))
            if lost:
                raise RuntimeError("authority unavailable")

        def complete(self, *args):
            pass

        def fail(self, *args):
            pass

    worker = Worker("worker", WorkerType.BUILD, "http://127.0.0.1:1", handlers=Handler())
    worker.client.client.close()
    worker.client = Client()
    assert worker.run_once() is (not lost)
    assert checks[0]["fencing_token"] == 3
    with pytest.raises(RuntimeError):
        contexts[0]["assert_live_lease"]()  # Unusable once its owning job has ended.
    assert len(checks) == 1
