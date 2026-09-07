# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Native D reads on Linux; fixture repository never claims PostgreSQL coverage."""

import importlib
import os
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.contracts.agent_v1 import GenerationRunStatusView
from hcuopt.evaluation.agent_generation_inspection import AgentGenerationInspectionService
from hcuopt.evaluation.agent_generation_read_model import AgentGenerationReadModelError
from hcuopt.source_hash import file_uri_to_path
from tests.unit.test_agent_proposal_verifier import _build, _knowledge


@pytest.mark.skipif(os.name != "posix", reason="requires native secure evidence reader")
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "timed_out"])
def test_native_inspection_is_read_only_and_api_rejects_tampering(tmp_path, monkeypatch, outcome):
    context, _ = _build(tmp_path, attempt_status=outcome)
    status = GenerationRunStatusView.model_validate_json((tmp_path / "status.json").read_bytes())
    repository = Mock()
    repository.generation_run_status.return_value = status
    knowledge = Mock()
    knowledge.load.return_value = SimpleNamespace(snapshot=_knowledge())
    inspector = AgentGenerationInspectionService(repository, tmp_path)
    view = inspector.prepare(
        context.generation_run_id,
        knowledge_store=knowledge,
        task_id=context.task_id,
        target_id=context.target_id,
    )
    assert view.read_model.human_review_status == "pending"
    assert view.generation_state == status.run.state
    assert view.read_model.attempts[0].status == outcome
    assert all(call[0] == "generation_run_status" for call in repository.mock_calls)
    monkeypatch.setenv("HCUOPT_AGENT_INSPECTION_ROOT", str(tmp_path))
    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "false")
    url = f"/v1/operator/agent-generations/{context.generation_run_id}/inspection"
    authorizer = Mock(side_effect=lambda _request, run_id: run_id == context.generation_run_id)
    with TestClient(
        create_app(repository=repository, agent_inspection_read_authorizer=authorizer)
    ) as client:
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == view.model_dump(mode="json")
        file_uri_to_path(status.attempts[0].runner_receipt_uri).write_bytes(b"{}")
        assert client.get(url).status_code == 422
        # Revocation must take effect on the next read, before corrupted evidence
        # is touched; neither the previous success nor D result grants access.
        authorizer.side_effect = None
        authorizer.return_value = False
        assert all(call[0] == "generation_run_status" for call in repository.mock_calls)
        repository.reset_mock()
        assert client.get(url).status_code == 403
        assert not repository.mock_calls
    assert authorizer.call_count == 3
    assert all(call[0] == "generation_run_status" for call in repository.mock_calls)


@pytest.mark.skipif(os.name != "posix", reason="requires native secure evidence reader")
def test_inspection_rejects_live_status_drift_and_does_not_complete_review(tmp_path):
    context, _ = _build(tmp_path)
    status = GenerationRunStatusView.model_validate_json((tmp_path / "status.json").read_bytes())
    repository = Mock()
    repository.generation_run_status.return_value = status
    inspector = AgentGenerationInspectionService(repository, tmp_path)
    knowledge = Mock()
    knowledge.load.return_value = SimpleNamespace(snapshot=_knowledge())
    inspector.prepare(
        context.generation_run_id,
        knowledge_store=knowledge,
        task_id=context.task_id,
        target_id=context.target_id,
    )
    repository.generation_run_status.return_value = status.model_copy(
        update={"run": status.run.model_copy(update={"version": status.run.version + 1})}
    )
    with pytest.raises(AgentGenerationReadModelError, match="changed"):
        inspector.get(context.generation_run_id)
    repository.complete_generation_review.assert_not_called()
    repository.publish_agent_generation_evidence_publication.assert_not_called()


def test_inspection_api_is_disabled_without_server_owned_configuration(monkeypatch):
    monkeypatch.delenv("HCUOPT_AGENT_INSPECTION_ROOT", raising=False)
    context_id = "10000000-0000-0000-0000-000000000001"
    client = TestClient(
        create_app(repository=Mock(), agent_inspection_read_authorizer=lambda _request, _run: True)
    )
    response = client.get(f"/v1/operator/agent-generations/{context_id}/inspection")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "agent_inspection_unconfigured"


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (None, 503),
        (False, 403),
        (1, 403),
        ("true", 403),
        ({"allowed": True}, 403),
        (RuntimeError("private-auth-detail"), 503),
    ],
)
def test_inspection_auth_fails_closed_before_reading_state_or_files(
    tmp_path, monkeypatch, decision, expected
):
    monkeypatch.setenv("HCUOPT_AGENT_INSPECTION_ROOT", str(tmp_path))
    repository = Mock()
    factory = Mock(side_effect=AssertionError("must not construct evidence service"))
    monkeypatch.setattr(
        importlib.import_module("hcuopt.api.app"), "AgentGenerationInspectionService", factory
    )
    authorizer = (
        None
        if decision is None
        else Mock(
            side_effect=decision if isinstance(decision, Exception) else None,
            return_value=decision,
        )
    )
    client = TestClient(
        create_app(repository=repository, agent_inspection_read_authorizer=authorizer)
    )
    response = client.get(
        "/v1/operator/agent-generations/10000000-0000-0000-0000-000000000001/inspection"
    )
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"
    assert "private-auth-detail" not in response.text
    factory.assert_not_called()
    assert not repository.mock_calls


def test_inspection_authorizer_receives_current_request_and_exact_run(monkeypatch):
    monkeypatch.delenv("HCUOPT_AGENT_INSPECTION_ROOT", raising=False)
    permitted_run = UUID(int=100)
    calls = []

    def authorize(request, run_id):
        calls.append((request.headers.get("x-test-identity"), run_id))
        # Test-only stand-in for a verified session identity, not a deployment
        # recipe that trusts caller-controlled identity headers.
        return request.headers.get("x-test-identity") == "reader" and run_id == permitted_run

    repository = Mock()
    client = TestClient(
        create_app(repository=repository, agent_inspection_read_authorizer=authorize)
    )
    path = f"/v1/operator/agent-generations/{permitted_run}/inspection"
    assert client.get(path).status_code == 403
    allowed = client.get(path, headers={"x-test-identity": "reader"})
    assert allowed.status_code == 503  # auth passes; server root is still mandatory
    assert allowed.json()["code"] == "agent_inspection_unconfigured"
    other_run = UUID(int=101)
    assert (
        client.get(
            f"/v1/operator/agent-generations/{other_run}/inspection",
            headers={"x-test-identity": "reader"},
        ).status_code
        == 403
    )
    assert calls == [(None, permitted_run), ("reader", permitted_run), ("reader", other_run)]
    assert not repository.mock_calls
