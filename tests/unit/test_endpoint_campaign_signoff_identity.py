# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.deployment.endpoint_campaign_signoff_identity import (
    EndpointCampaignSignoffIdentity,
    endpoint_campaign_signoff_identity_from_env,
)

TOKEN = "endpoint-campaign-token-" + "x" * 32


def identity(**changes):
    values = {
        "campaign_id": uuid4(),
        "actor": "campaign-reviewer",
        "token_sha256": hashlib.sha256(TOKEN.encode("ascii")).hexdigest(),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "browser_origin": "http://127.0.0.1:4194",
    }
    values.update(changes)
    return EndpointCampaignSignoffIdentity(**values)


def request(*, token=TOKEN, origin="http://127.0.0.1:4194", site="same-origin"):
    headers = [(b"authorization", f"Bearer {token}".encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if site is not None:
        headers.append((b"sec-fetch-site", site.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def test_identity_authorizes_only_exact_campaign_actor_token_and_origin():
    configured = identity()
    assert configured(request(), configured.campaign_id) == configured.actor
    assert configured(request(token="y" * 43), configured.campaign_id) is None
    assert configured(request(), uuid4()) is None
    assert configured(request(origin="http://untrusted.invalid"), configured.campaign_id) is None
    assert configured(request(site="cross-site"), configured.campaign_id) is None
    assert configured.token_sha256 not in repr(configured)


def test_identity_rejects_expiry_and_non_loopback_http():
    expired = identity(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert expired(request(), expired.campaign_id) is None
    with pytest.raises(ValueError, match="loopback"):
        identity(browser_origin="http://example.invalid")
    with pytest.raises(ValueError, match="origin port"):
        identity(browser_origin="http://127.0.0.1:99999")


def test_environment_loader_is_opt_in_and_fail_closed(monkeypatch):
    names = [
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_CAMPAIGN_ID",
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ACTOR",
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_TOKEN_SHA256",
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_EXPIRES_AT",
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ORIGIN",
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert endpoint_campaign_signoff_identity_from_env() is None
    monkeypatch.setenv(names[0], str(uuid4()))
    with pytest.raises(RuntimeError, match="incomplete"):
        endpoint_campaign_signoff_identity_from_env()


def test_environment_loader_never_requires_plaintext_token(monkeypatch):
    configured = identity()
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_CAMPAIGN_ID", str(configured.campaign_id)
    )
    monkeypatch.setenv("HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ACTOR", configured.actor)
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_TOKEN_SHA256", configured.token_sha256
    )
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_EXPIRES_AT",
        configured.expires_at.isoformat(),
    )
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ORIGIN", configured.browser_origin
    )
    loaded = endpoint_campaign_signoff_identity_from_env()
    assert loaded == configured
    assert all(TOKEN not in value for value in map(str, loaded.__dict__.values()))


def test_standard_api_loads_scoped_identity_and_preserves_repository_actor(monkeypatch):
    configured = identity()
    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "false")
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_CAMPAIGN_ID", str(configured.campaign_id)
    )
    monkeypatch.setenv("HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ACTOR", configured.actor)
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_TOKEN_SHA256", configured.token_sha256
    )
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_EXPIRES_AT",
        configured.expires_at.isoformat(),
    )
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ORIGIN", configured.browser_origin
    )

    class Repository:
        calls = []

        def signoff_endpoint_validation_campaign(self, campaign_id, payload):
            self.calls.append((campaign_id, payload))
            return {
                "signoff_id": uuid4(),
                "campaign_id": campaign_id,
                **payload.model_dump(mode="json"),
                "campaign_state": "completed",
                "created_at": datetime.now(timezone.utc),
            }

    repository = Repository()
    app = create_app(repository=repository)
    payload = {
        "decision": "accepted",
        "actor": configured.actor,
        "reason": "accept the formal D evidence only",
        "adjudication_result_sha256": "sha256:" + "a" * 64,
        "idempotency_key": "endpoint-signoff-fixture",
        "automatic_release_allowed": False,
    }
    with TestClient(app, base_url=configured.browser_origin) as client:
        response = client.post(
            f"/v1/endpoint-validation-campaigns/{configured.campaign_id}/signoff",
            json=payload,
            headers={
                "Authorization": "Bearer " + TOKEN,
                "Origin": configured.browser_origin,
                "Sec-Fetch-Site": "same-origin",
            },
        )
    assert response.status_code == 200
    assert response.json()["automatic_release_allowed"] is False
    assert len(repository.calls) == 1
    assert repository.calls[0][0] == configured.campaign_id
    assert repository.calls[0][1].actor == configured.actor
