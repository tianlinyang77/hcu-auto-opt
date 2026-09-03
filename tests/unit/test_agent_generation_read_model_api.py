# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.contracts.agent_verification_v1 import AgentGenerationReadModel
from hcuopt.evaluation.agent_generation_read_model import AgentGenerationReadModelError
from hcuopt.storage.repository import PostgresRepository

RUN_ID = UUID("71000000-0000-0000-0000-000000000001")


def _read_model() -> AgentGenerationReadModel:
    fixture = (
        Path(__file__).parents[2]
        / "web"
        / "public"
        / "fixtures"
        / "demo-agent-proposals.json"
    )
    return AgentGenerationReadModel.model_validate(
        json.loads(fixture.read_text(encoding="utf-8"))
    )


class _ReadService:
    def __init__(self, result: AgentGenerationReadModel | Exception) -> None:
        self.result = result
        self.calls: list[UUID] = []

    def get(self, generation_run_id: UUID) -> AgentGenerationReadModel:
        self.calls.append(generation_run_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _repository() -> Mock:
    return Mock(spec=PostgresRepository)


def test_agent_generation_evidence_api_returns_the_shared_d_read_model() -> None:
    service = _ReadService(_read_model())
    application = create_app(
        repository=_repository(),
        agent_evidence_read_models=service,  # type: ignore[arg-type]
    )

    with TestClient(application) as client:
        response = client.get(f"/v1/operator/agent-generations/{RUN_ID}/evidence")

    assert response.status_code == 200
    assert response.json() == _read_model().model_dump(mode="json")
    assert service.calls == [RUN_ID]
    assert response.json()["formal_readiness"] == "hold"
    assert response.json()["performance_conclusion"] == "not_measured"
    assert response.json()["formal_intake_allowed"] is False
    assert response.json()["automatic_release_allowed"] is False


def test_agent_generation_evidence_api_fails_closed_on_invalid_evidence() -> None:
    service = _ReadService(
        AgentGenerationReadModelError(
            "agent_verification_digest_drift",
            "recomputed digest changed",
        )
    )
    application = create_app(
        repository=_repository(),
        agent_evidence_read_models=service,  # type: ignore[arg-type]
    )

    with TestClient(application) as client:
        response = client.get(f"/v1/operator/agent-generations/{RUN_ID}/evidence")

    assert response.status_code == 422
    assert response.json() == {
        "code": "agent_verification_digest_drift",
        "message": "recomputed digest changed",
        "retryable": False,
    }


def test_agent_generation_evidence_api_requires_a_server_owned_root(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("HCUOPT_AGENT_EVIDENCE_ROOT", raising=False)
    application = create_app(repository=_repository())

    with TestClient(application) as client:
        response = client.get(f"/v1/operator/agent-generations/{RUN_ID}/evidence")

    assert response.status_code == 503
    assert response.json()["code"] == "agent_evidence_root_unavailable"
    assert response.json()["retryable"] is True


def test_agent_generation_evidence_api_is_declared_in_openapi() -> None:
    paths = create_app().openapi()["paths"]

    assert "/v1/operator/agent-generations/{generation_run_id}/evidence" in paths
