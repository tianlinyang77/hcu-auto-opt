# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hcuopt.deployment.framework_signoff_identity import (
    FrameworkSigningSession,
    FrameworkSignoffIdentity,
)
from hcuopt.deployment.framework_smoke_viewer import create_viewer
from hcuopt.deployment.framework_viewer_signing import ViewerSigning
from hcuopt.domain.errors import Conflict
from tests.unit.test_framework_smoke_viewer import projection_fixture

TOKEN = "s" * 43
ORIGIN = "http://127.0.0.1:4194"


def fixture_app(*, write=None, read=None, token=TOKEN, wrong_task=False):
    summary = projection_fixture()
    task = summary.task.task_id
    session = FrameworkSigningSession(
        FrameworkSignoffIdentity(
            task_id=uuid4() if wrong_task else task,
            actor="fixture-owner",
            token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    signing = ViewerSigning(session, write or (lambda *_: None), read or (lambda *_: None))
    instance = uuid4()
    app = create_viewer(
        task_id=task,
        credential="r" * 43,
        read_summary=lambda: summary,
        signing=signing,
        control=(instance, "c" * 43, session.revoke),
        revoke_signing=session.revoke,
    )
    payload = {
        "actor": "fixture-owner",
        "decision": "approved",
        "reason": "fixture only",
        "evidence_bundle_id": str(summary.evidence_bundles[0]["evidence_id"]),
        "idempotency_key": str(uuid4()),
    }
    return app, task, instance, session, payload


@pytest.mark.parametrize("key", ["", "r" * 43, "c" * 43, "different" * 8])
def test_read_and_control_credentials_never_reach_writer(key):
    def forbidden(*_):
        pytest.fail("unauthorized DB access")

    app, task, _, _, payload = fixture_app(write=forbidden, read=forbidden)
    with TestClient(app, base_url=ORIGIN) as client:
        url = f"/v1/framework-smoke/tasks/{task}/signoff"
        headers = {"Authorization": "Bearer " + key}
        assert client.get(url, headers=headers).status_code == 403
        assert client.post(url, headers=headers, json=payload).status_code == 403


@pytest.mark.parametrize("token,wrong_task", [("r" * 43, False), ("c" * 43, False), (TOKEN, True)])
def test_reused_credentials_or_wrong_task_fail_at_construction(token, wrong_task):
    with pytest.raises(ValueError, match="independent"):
        fixture_app(token=token, wrong_task=wrong_task)


def test_expiry_revocation_and_original_writer_receipt():
    calls = []
    row = None

    def write(task, payload):
        nonlocal row
        calls.append(payload)
        row = {
            "signoff_id": uuid4(),
            "task_id": task,
            **payload.model_dump(),
            "task_state": "completed",
            "created_at": datetime.now(timezone.utc),
        }
        return row

    app, task, instance, session, payload = fixture_app(write=write, read=lambda *_: row)
    headers = {"Authorization": "Bearer " + TOKEN, "Origin": ORIGIN}
    with TestClient(app, base_url=ORIGIN) as client:
        url = f"/v1/framework-smoke/tasks/{task}/signoff"
        assert client.get(url, headers=headers).json()["signoff"] is None
        assert not calls
        reply = client.post(url, headers=headers, json=payload)
        assert reply.status_code == 200 and len(calls) == 1
        assert client.get(url, headers=headers).json()["signoff"] == reply.json()
        revoked = client.post(
            f"/v1/viewer-control/{instance}/revoke-signing",
            headers={"Authorization": "Bearer " + "c" * 43},
        )
        assert revoked.status_code == 200 and not session.active
        assert client.post(url, headers=headers, json=payload).status_code == 403
        assert len(calls) == 1
        view = client.get(
            f"/v1/framework-smoke-inspection/{task}",
            headers={"Authorization": "Bearer " + "r" * 43},
        ).json()
        assert view["write_actions_available"] is False
        assert view["automatic_release_allowed"] is False


@pytest.mark.parametrize("change", ["task", "actor", "origin", "expired", "cross-site"])
def test_signoff_scope_cannot_be_forged(change):
    def forbidden(*_):
        pytest.fail("unauthorized DB access")

    app, task, _, session, payload = fixture_app(write=forbidden, read=forbidden)
    headers = {"Authorization": "Bearer " + TOKEN, "Origin": ORIGIN}
    if change == "task":
        task = uuid4()
    elif change == "actor":
        payload["actor"] = "forged"
    elif change == "origin":
        headers["Origin"] = "http://untrusted.invalid"
    elif change == "cross-site":
        headers["Sec-Fetch-Site"] = "cross-site"
    else:
        session.identity = FrameworkSignoffIdentity(
            task_id=task,
            actor="fixture-owner",
            token_sha256=session.identity.token_sha256,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    with TestClient(app, base_url=ORIGIN) as client:
        assert (
            client.post(
                f"/v1/framework-smoke/tasks/{task}/signoff", headers=headers, json=payload
            ).status_code
            == 403
        )


@pytest.mark.parametrize("failure,code", [(Conflict("secret"), 409), (RuntimeError("secret"), 503)])
def test_writer_failure_is_redacted(failure, code):
    def write(*_):
        raise failure

    app, task, _, _, payload = fixture_app(write=write)
    with TestClient(app, base_url=ORIGIN) as client:
        reply = client.post(
            f"/v1/framework-smoke/tasks/{task}/signoff",
            json=payload,
            headers={"Authorization": "Bearer " + TOKEN},
        )
        assert reply.status_code == code
        assert "secret" not in reply.text
        assert reply.headers["Cache-Control"] == "no-store"
