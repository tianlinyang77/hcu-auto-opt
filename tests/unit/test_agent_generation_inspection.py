# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Native D reads on Linux; fixture repository never claims PostgreSQL coverage."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

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
    with TestClient(create_app(repository=repository)) as client:
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.json() == view.model_dump(mode="json")
        file_uri_to_path(status.attempts[0].runner_receipt_uri).write_bytes(b"{}")
        assert client.get(url).status_code == 422
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
    client = TestClient(create_app(repository=Mock()))
    response = client.get(f"/v1/operator/agent-generations/{context_id}/inspection")
    assert response.status_code == 503
