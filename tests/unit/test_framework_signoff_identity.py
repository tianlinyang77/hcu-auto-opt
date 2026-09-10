# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.deployment.framework_signoff_identity import FrameworkSignoffIdentity
from hcuopt.domain.errors import Conflict

TOKEN = "fixture-signing-token-" + "x" * 32
ORIGIN = "http://127.0.0.1:4194"


class Repository:
    def __init__(self, conflict=False):
        self.calls = []
        self.conflict = conflict

    def migrate(self):
        raise AssertionError("identity tests must not migrate or access a database")

    def signoff_framework_task(self, task, payload):
        self.calls.append((task, payload))
        if self.conflict:
            raise Conflict("signoff must bind the latest Framework Smoke evidence")
        return {
            "signoff_id": uuid4(),
            "task_id": task,
            **payload.model_dump(),
            "task_state": "completed",
            "created_at": datetime.now(timezone.utc),
        }


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("HCUOPT_AUTO_MIGRATE", "false")
    task = uuid4()
    identity = FrameworkSignoffIdentity(
        task_id=task,
        actor="fixture-owner",
        token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    payload = {
        "decision": "approved",
        "actor": "fixture-owner",
        "reason": "fixture only",
        "evidence_bundle_id": str(uuid4()),
        "idempotency_key": "fixture-signoff-v1",
    }
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
    }
    return task, identity, payload, headers


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "missing",
        "read-key",
        "other-task",
        "expired",
        "actor",
        "origin",
        "cross-site",
        "broken-provider",
    ],
)
def test_signing_requires_independent_task_identity(setup, change):
    task, identity, payload, headers = setup
    authorizer = identity
    expected = 403
    if change == "none":
        authorizer, expected = None, 503
    elif change == "missing":
        headers.pop("Authorization")
    elif change == "read-key":
        headers["Authorization"] = "Bearer " + "read-only-" * 8
    elif change == "other-task":
        task = uuid4()
    elif change == "expired":
        authorizer = FrameworkSignoffIdentity(
            task_id=task,
            actor=identity.actor,
            token_sha256=identity.token_sha256,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    elif change == "actor":
        payload["actor"] = "forged-owner"
    elif change == "origin":
        headers["Origin"] = "http://untrusted.invalid"
    elif change == "cross-site":
        headers["Sec-Fetch-Site"] = "cross-site"
    else:

        def broken(*args):
            raise RuntimeError("secret-in-provider")

        authorizer, expected = broken, 503
    repository = Repository()
    app = create_app(repository=repository, framework_signoff_authorizer=authorizer)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/framework-smoke/tasks/{task}/signoff", headers=headers, json=payload
        )
    assert response.status_code == expected
    assert repository.calls == []
    assert TOKEN not in response.text and "secret-in-provider" not in response.text


@pytest.mark.parametrize("cli", [False, True])
def test_verified_actor_and_exact_evidence_reach_original_state_machine(setup, cli):
    task, identity, payload, headers = setup
    if cli:
        headers = {"Authorization": headers["Authorization"]}
    repository = Repository()
    app = create_app(repository=repository, framework_signoff_authorizer=identity)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/framework-smoke/tasks/{task}/signoff", headers=headers, json=payload
        )
    assert response.status_code == 200
    assert len(repository.calls) == 1
    recorded_task, request = repository.calls[0]
    assert recorded_task == task
    assert request.actor == identity.actor
    assert request.model_dump(mode="json") == payload


def test_identity_does_not_override_repository_evidence_conflict(setup):
    task, identity, payload, headers = setup
    app = create_app(repository=Repository(conflict=True), framework_signoff_authorizer=identity)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/framework-smoke/tasks/{task}/signoff", headers=headers, json=payload
        )
    assert response.status_code == 409


def test_identity_repr_hides_digest_and_rejects_naive_expiry(setup):
    _, identity, _, _ = setup
    assert identity.token_sha256 not in repr(identity)
    with pytest.raises(ValueError, match="timezone-aware"):
        FrameworkSignoffIdentity(
            task_id=identity.task_id,
            actor=identity.actor,
            token_sha256=identity.token_sha256,
            expires_at=datetime.now(),
        )
