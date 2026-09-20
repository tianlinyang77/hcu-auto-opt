# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app


class SignoffRepository:
    def __init__(self) -> None:
        self.calls = []

    def migrate(self) -> None:
        raise AssertionError("signoff identity test must not access a database")

    def signoff_endpoint_validation_campaign(self, campaign_id, payload):
        self.calls.append((campaign_id, payload))
        return {
            "signoff_id": uuid4(),
            "campaign_id": campaign_id,
            **payload.model_dump(mode="json"),
            "campaign_state": (
                "completed" if payload.decision == "accepted" else "rejected"
            ),
            "created_at": datetime.now(timezone.utc),
        }


@pytest.fixture
def signoff_request(monkeypatch):
    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "false")
    campaign_id = uuid4()
    payload = {
        "decision": "accepted",
        "actor": "campaign-reviewer",
        "reason": "formal D evidence reviewed",
        "adjudication_result_sha256": "sha256:" + "a" * 64,
        "idempotency_key": f"endpoint-signoff:{uuid4()}",
        "automatic_release_allowed": False,
    }
    return campaign_id, payload


@pytest.mark.parametrize("mode", ("missing", "denied", "mismatch", "allowed"))
def test_endpoint_campaign_signoff_requires_scoped_identity(signoff_request, mode):
    campaign_id, payload = signoff_request
    repository = SignoffRepository()
    authorizer = None
    expected = 503
    if mode == "denied":
        authorizer, expected = lambda _request, _campaign_id: None, 403
    elif mode == "mismatch":
        authorizer, expected = lambda _request, _campaign_id: "another-reviewer", 403
    elif mode == "allowed":
        authorizer, expected = (
            lambda _request, authorized_campaign_id: (
                "campaign-reviewer"
                if authorized_campaign_id == campaign_id
                else None
            ),
            200,
        )
    app = create_app(
        repository=repository,
        endpoint_campaign_signoff_authorizer=authorizer,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/v1/endpoint-validation-campaigns/{campaign_id}/signoff",
            json=payload,
        )

    assert response.status_code == expected
    if mode == "allowed":
        assert len(repository.calls) == 1
        recorded_campaign, recorded = repository.calls[0]
        assert recorded_campaign == campaign_id
        assert recorded.actor == "campaign-reviewer"
        assert response.json()["campaign_state"] == "completed"
        assert response.json()["automatic_release_allowed"] is False
    else:
        assert repository.calls == []


def test_endpoint_campaign_authorizer_failure_is_redacted(signoff_request):
    campaign_id, payload = signoff_request

    def broken(_request, _campaign_id: UUID):
        raise RuntimeError("private-provider-detail")

    app = create_app(
        repository=SignoffRepository(),
        endpoint_campaign_signoff_authorizer=broken,
    )
    with TestClient(app) as client:
        response = client.post(
            f"/v1/endpoint-validation-campaigns/{campaign_id}/signoff",
            json=payload,
        )
    assert response.status_code == 503
    assert "private-provider-detail" not in response.text
