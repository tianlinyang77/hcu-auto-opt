# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Actual PostgreSQL + subprocess + local HTTP; no model or HCU claims."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from hcuopt.adapters.agent_runner import AgentInputFile
from hcuopt.adapters.messages_generator import messages_profile_for_input
from hcuopt.agent.authority import ApexGenerationCoordinator
from hcuopt.agent.messages_dispatch import MessagesDispatchService
from hcuopt.agent.messages_worker import MessagesGenerationWorker
from hcuopt.contracts.agent_v1 import GenerationRunStartRequest
from hcuopt.domain.errors import SourceArtifactError, StaleClaimToken
from hcuopt.evaluation.agent_generation_inspection import AgentGenerationInspectionService
from hcuopt.generators import anthropic_messages
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.storage.repository import PostgresRepository
from tests.unit.test_messages_generator import _response
from tests.unit.test_messages_generator import setup as messages_setup  # noqa: F401

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.getenv("HCUOPT_DATABASE_URL"), reason="requires PostgreSQL"),
]


@pytest.fixture
def repository():
    """Never truncate shared tables; migrate and drop only our generated schema."""
    url = os.environ["HCUOPT_DATABASE_URL"]
    schema = "messages_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(url, options=f"-csearch_path={schema}")
    try:
        repo = PostgresRepository(scoped_url)
        repo.migrate()
        yield repo
    finally:
        # Exact, locally generated name; no public/default schema is touched.
        assert schema.startswith("messages_test_") and len(schema) == 46
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def dispatch_case(messages_setup, repository, tmp_path):  # noqa: F811
    fixture = messages_setup
    calls = []
    response = {"reply": _response(), "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            raw = canonical_json_bytes(response["reply"])
            self.send_response(response["status"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    envelope = anthropic_messages.strict_json(fixture["item"].content)
    envelope["provider"].update(base_url=f"http://127.0.0.1:{server.server_port}", allow_http=True)
    item = AgentInputFile(
        path=anthropic_messages.INPUT_NAME, content=canonical_json_bytes(envelope)
    )
    generator = (
        fixture["plan"]
        .generators[0]
        .model_copy(update={"adapter_profile": messages_profile_for_input(item)})
    )
    plan = fixture["plan"].model_copy(update={"generators": (generator,)})
    start = GenerationRunStartRequest(
        request=fixture["request"], plan=plan, actor="postgres-test", idempotency_key=fixture["key"]
    )
    ApexGenerationCoordinator().start(start, repository)
    root = tmp_path / "dispatch-store"
    worker = MessagesGenerationWorker(root)
    service = MessagesDispatchService(repository, worker)
    try:
        yield locals()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _execute(case):
    return case["service"].run_once(
        case["start"].request.generation_run_id,
        case["item"],
        worker_id="messages-postgres-test",
        api_key="test-only-not-a-secret",
        lease_seconds=15,
    )


@pytest.mark.parametrize("mode", ["valid", "bad_patch", "provider_failure"])
def test_dispatch_settles_actual_receipt_and_replays_without_network(dispatch_case, mode):
    case = dispatch_case
    if mode == "bad_patch":
        proposals = json.loads(case["response"]["reply"]["content"][0]["text"])["proposals"]
        proposals[0]["patch"] = proposals[0]["patch"].replace("-return value", "-return other")
        case["response"]["reply"] = _response(proposals)
    elif mode == "provider_failure":
        case["response"]["status"] = 503
    status = _execute(case)
    attempt = status.attempts[0]
    assert attempt.runner_receipt_hash is not None
    assert attempt.state == ("succeeded" if mode == "valid" else "failed")
    assert len(status.proposals) == (1 if mode == "valid" else 0)
    assert attempt.actual.tokens == (1000 if mode == "provider_failure" else 100)
    assert status.automatic_release_allowed is False
    recovered = MessagesDispatchService(
        PostgresRepository(case["repository"].database_url), MessagesGenerationWorker(case["root"])
    ).recover(status.run.generation_run_id, attempt.attempt_id)
    assert recovered == status
    assert len(case["calls"]) == 1
    assert _execute(case) == status  # terminal Run does not claim again
    assert len(case["calls"]) == 1


@pytest.mark.skipif(os.name != "posix", reason="D requires POSIX openat/O_NOFOLLOW")
@pytest.mark.parametrize("mode", ["valid", "bad_patch", "provider_failure"])
def test_postgres_to_native_d_inspection(dispatch_case, mode):
    case = dispatch_case
    if mode == "bad_patch":
        proposals = json.loads(case["response"]["reply"]["content"][0]["text"])["proposals"]
        proposals[0]["patch"] = proposals[0]["patch"].replace("-return value", "-return other")
        case["response"]["reply"] = _response(proposals)
    elif mode == "provider_failure":
        case["response"]["status"] = 503
    status = _execute(case)
    attempt = status.attempts[0]
    inspector = AgentGenerationInspectionService(case["repository"], case["root"])
    inspection = inspector.prepare(
        status.run.generation_run_id,
        knowledge_store=case["fixture"]["knowledge"],
        task_id=uuid4(),
        target_id="postgres-http-stub-not-hcu",
    )
    assert inspection.read_model.attempts[0].status == (
        "succeeded" if mode == "valid" else "failed"
    )
    assert inspection.read_model.attempts[0].output_tokens == (
        1000 if mode == "provider_failure" else 100
    )
    assert inspection.read_model.formal_intake_allowed is False
    assert inspection.read_model.human_review_status == "pending"
    assert case["repository"].generation_run_status(status.run.generation_run_id) == status
    assert (
        AgentGenerationInspectionService(
            PostgresRepository(case["repository"].database_url), case["root"]
        ).get(status.run.generation_run_id)
        == inspection
    )

    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from hcuopt.api.app import create_app

    with patch.dict(os.environ, HCUOPT_AGENT_INSPECTION_ROOT=str(case["root"]),
                    HCUOPT_AUTO_MIGRATE="false"), TestClient(
                        create_app(repository=case["repository"])) as client:
        response = client.get(
            f"/v1/operator/agent-generations/{status.run.generation_run_id}/inspection"
        )
        assert response.status_code == 200, response.text
        assert response.json() == inspection.model_dump(mode="json")
        # Every read revalidates D evidence; a corrupted artifact is not displayed.
        path = case["root"] / "attempts" / str(attempt.attempt_id) / "receipt-ref.json"
        from hcuopt.contracts.agent_runner_v1 import RunnerExecutionReceiptRef
        from hcuopt.source_hash import file_uri_to_path

        ref = RunnerExecutionReceiptRef.model_validate_json(path.read_bytes())
        file_uri_to_path(ref.uri).write_bytes(b"{}")
        assert (
            client.get(
                f"/v1/operator/agent-generations/{status.run.generation_run_id}/inspection"
            ).status_code
            != 200
        )


@pytest.mark.parametrize("expired", [False, True])
def test_crash_before_settlement_recovers_or_is_fenced(dispatch_case, monkeypatch, expired):
    case = dispatch_case
    repo = case["repository"]
    settle = repo.settle_generation_attempt

    def crash(*args, **kwargs):
        raise RuntimeError("simulated database disconnect after Receipt publication")

    monkeypatch.setattr(repo, "settle_generation_attempt", crash)
    with pytest.raises(RuntimeError, match="disconnect"):
        _execute(case)
    monkeypatch.setattr(repo, "settle_generation_attempt", settle)
    run_id = case["start"].request.generation_run_id
    status = repo.generation_run_status(run_id)
    attempt = status.attempts[0]
    assert attempt.state == "running"
    if expired:
        with repo.connection() as connection:
            connection.execute(
                "UPDATE agent_generator_attempts SET lease_expires_at = %s WHERE attempt_id = %s",
                (datetime.now(timezone.utc) - timedelta(seconds=1), attempt.attempt_id),
            )
    service = MessagesDispatchService(
        PostgresRepository(repo.database_url), MessagesGenerationWorker(case["root"])
    )
    if expired:
        with pytest.raises(StaleClaimToken):
            service.recover(run_id, attempt.attempt_id)
        assert not repo.generation_run_status(run_id).proposals
    else:
        assert service.recover(run_id, attempt.attempt_id).attempts[0].state == "succeeded"
    assert len(case["calls"]) == 1


def test_incompatible_input_refused_before_database_claim(dispatch_case):
    case = dispatch_case
    run_id = case["start"].request.generation_run_id
    before = case["repository"].generation_run_status(run_id)
    with pytest.raises(SourceArtifactError, match="authority"):
        case["service"].run_once(
            run_id,
            AgentInputFile(path=anthropic_messages.INPUT_NAME, content=b"{}"),
            worker_id="test",
            api_key="test-only",
            lease_seconds=60,
        )
    assert case["repository"].generation_run_status(run_id) == before
    assert not case["calls"]


def test_recovery_refuses_cross_run_and_modified_input(dispatch_case):
    case = dispatch_case
    status = _execute(case)
    attempt_id = status.attempts[0].attempt_id
    with pytest.raises(SourceArtifactError, match="crosses"):
        case["service"].recover(uuid4(), attempt_id)
    (case["root"] / "attempts" / str(attempt_id) / "input.json").write_bytes(b"{}")
    with pytest.raises(SourceArtifactError, match="changed"):
        case["service"].recover(status.run.generation_run_id, attempt_id)
    assert len(case["calls"]) == 1


def test_started_without_receipt_cannot_be_reexecuted(dispatch_case, monkeypatch):
    from hcuopt.adapters.agent_runner import LocalCommandAgentRunner

    case = dispatch_case

    def crash(*args, **kwargs):
        raise RuntimeError("uncertain runner interruption")

    monkeypatch.setattr(LocalCommandAgentRunner, "run", crash)
    with pytest.raises(RuntimeError, match="interruption"):
        _execute(case)
    run_id = case["start"].request.generation_run_id
    state = case["repository"].generation_run_status(run_id)
    attempt = state.attempts[0]
    with pytest.raises(FileNotFoundError):
        case["service"].recover(run_id, attempt.attempt_id)
    assert _execute(case) == state  # no pending Attempt exists; no implicit retry
    assert not case["calls"]


def test_lease_larger_than_frozen_budget_is_rejected_before_claim(dispatch_case):
    case = dispatch_case
    run_id = case["start"].request.generation_run_id
    with pytest.raises(SourceArtifactError, match="lease"):
        case["service"].run_once(
            run_id,
            case["item"],
            worker_id="test",
            api_key="test-only",
            lease_seconds=60,
        )
    assert case["repository"].generation_run_status(run_id).attempts[0].state == "pending"
    assert not case["calls"]
