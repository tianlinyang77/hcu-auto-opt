# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Original API/state-machine integration using ONLY a freshly created schema."""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from hcuopt.contracts.v1 import FrameworkSmokeCreate, FrameworkSmokeSummary, WorkerRegister
from hcuopt.deployment.framework_signoff_identity import (
    FrameworkSigningSession,
    FrameworkSignoffIdentity,
)
from hcuopt.deployment.framework_smoke_viewer import create_viewer
from hcuopt.deployment.framework_viewer_signing import ViewerSigning
from hcuopt.domain.enums import WorkerType
from hcuopt.orchestrator.router import WorkflowRouter
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import load_target
from hcuopt.workers.handlers import FakeJobHandlers

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).parents[2]
PROFILE = "fake-v1-control-flow-only"


@pytest.fixture
def repository():
    dsn = os.getenv("HCUOPT_SIGNOFF_TEST_DATABASE_URL") or os.getenv("HCUOPT_DATABASE_URL")
    if not dsn:
        pytest.skip("requires explicit test PostgreSQL DSN")
    schema = "signoff_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        isolated = make_conninfo(dsn, options=f"-c search_path={schema} -c statement_timeout=15000")
        repo = PostgresRepository(isolated)
        with repo.connection() as connection:
            assert (
                connection.execute("SELECT current_schema() AS name").fetchone()["name"] == schema
            )
        repo.migrate()
        yield repo, isolated
    finally:
        # This exact unpredictable name was created above; never public/shared schemas.
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def create_fixture(repo):
    target_path = ROOT / "config/targets/nmz36-sglang-0.5.12.yaml"
    target = load_target(target_path)
    task = repo.create_framework_smoke_task(
        FrameworkSmokeCreate(
            name="isolated synthetic signoff fixture",
            target_id=target.target_id,
            adapter_profile=PROFILE,
            idempotency_key=str(uuid4()),
        ),
        target,
        str(target_path),
    )
    for name, worker_type, adapters in (
        ("fixture-build", WorkerType.BUILD, ["source_manager", "builder", "artifact_store"]),
        ("fixture-gpu", WorkerType.GPU, ["executor", "evaluator", "resource_cleaner"]),
    ):
        repo.register_worker(
            WorkerRegister(
                worker_id=name,
                worker_type=worker_type,
                adapter_profile=PROFILE,
                capabilities={
                    "adapter_profile": PROFILE,
                    "adapters": adapters,
                    "resource_id": "fake-hcu-0",
                },
            )
        )
    router, handlers = WorkflowRouter(repo), FakeJobHandlers()
    for name in ("fixture-build", "fixture-build", "fixture-gpu"):
        job = repo.claim_job(name)
        assert job is not None
        payload = dict(job["payload"])
        payload["_job_context"] = {
            "job_id": str(job["job_id"]),
            "attempt_number": job["attempts"],
            "resource_id": job["resource_id"],
            "fencing_token": job["fencing_token"],
        }
        completed = repo.complete_job(
            job["job_id"],
            job["claim_token"],
            job["fencing_token"],
            handlers.handle(job["job_type"], payload),
        )
        router.advance(completed)
    return task["task_id"]


@pytest.mark.parametrize("decision,state", [("approved", "completed"), ("rejected", "rejected")])
def test_http_signoff_replay_and_readback_in_isolated_schema(repository, decision, state):
    repo, dsn = repository
    task = create_fixture(repo)
    summary = repo.framework_smoke_summary(task)
    evidence = summary["evidence_bundles"][-1]["evidence_id"]
    token = "fixture-independent-signing-" + uuid4().hex
    identity = FrameworkSignoffIdentity(
        task_id=task,
        actor="fixture-only-reviewer",
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    signing = ViewerSigning(
        FrameworkSigningSession(identity), repo.signoff_framework_task, repo.framework_signoff
    )
    app = create_viewer(
        task_id=task,
        credential="r" * 43,
        signing=signing,
        read_summary=lambda: FrameworkSmokeSummary.model_validate(
            {**repo.framework_smoke_summary(task), "adapter_mode": "fake"}
        ),
    )
    body = {
        "actor": identity.actor,
        "decision": decision,
        "reason": "synthetic fixture only",
        "evidence_bundle_id": str(evidence),
        "idempotency_key": str(uuid4()),
    }
    headers = {"Authorization": "Bearer " + token, "Origin": identity.browser_origin}
    url = f"/v1/framework-smoke/tasks/{task}/signoff"
    with TestClient(app, base_url=identity.browser_origin) as client:
        assert client.get(url, headers=headers).json()["signoff"] is None
        assert repo.get_task(task)["state"] == "awaiting_signoff"
        assert (
            client.post(url, headers={"Authorization": "Bearer " + "r" * 43}, json=body).status_code
            == 403
        )
        assert (
            client.post(url, headers=headers, json={**body, "actor": "forged"}).status_code == 403
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(
                pool.map(lambda _: client.post(url, headers=headers, json=body), range(2))
            )
        assert [reply.status_code for reply in replies] == [200, 200]
        assert replies[0].json() == replies[1].json()
        # Simulates a lost POST response: query persisted result with a NEW repository connection.
        persisted = PostgresRepository(dsn).framework_signoff(task)
        assert persisted["idempotency_key"] == body["idempotency_key"]
        assert persisted["task_state"] == state
        assert client.get(url, headers=headers).json()["signoff"] == replies[0].json()
        assert (
            client.post(url, headers=headers, json={**body, "reason": "changed"}).status_code == 409
        )
        assert (
            client.post(
                url, headers=headers, json={**body, "idempotency_key": str(uuid4())}
            ).status_code
            == 409
        )
        view = client.get(
            f"/v1/framework-smoke-inspection/{task}",
            headers={"Authorization": "Bearer " + "r" * 43},
        ).json()
        assert view["task"]["state"] == state and view["automatic_release_allowed"] is False
        assert view["performance_conclusion"] == "not_measured"
        signing.session.revoke()
        assert client.post(url, headers=headers, json=body).status_code == 403
    events = repo.list_task_events(task)
    assert sum(event["event_type"] == "framework_smoke_signed_off" for event in events) == 1
