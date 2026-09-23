# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.api.formal_start_management import FormalIntentSubmission
from hcuopt.contracts.formal_dispatch_v1 import FormalDispatchStatus
from hcuopt.contracts.m2_formal_start_v1 import derive_formal_start_ids
from tests.unit.test_formal_operator_plans import NOW
from tests.unit.test_formal_start_management_api import TOKEN, setup_management


def status_client(tmp_path, state="queued", drift=False):  # type: ignore[no-untyped-def]
    management, repository, payload = setup_management(tmp_path)
    intent_id, _, round_id = derive_formal_start_ids(payload["idempotency_key"])
    calls = []

    class Reader:
        def read_status(self, requested_id):  # type: ignore[no-untyped-def]
            calls.append(requested_id)
            assert requested_id == intent_id
            return FormalDispatchStatus(
                intent_id=uuid4() if drift else intent_id,
                round_id=round_id,
                resolved_plan_hash=payload["resolved_plan_hash"],
                state=state,
                service_identity=management.coordinator.service_identity,
            )

    management = replace(
        management,
        capabilities=(
            replace(
                management.capabilities[0],
                submission=FormalIntentSubmission.model_validate(payload),
            ),
        ),
        dispatch_reader=Reader(),
    )
    app = create_app(repository=repository, formal_start_management=management)
    return TestClient(app), management, repository, calls


@pytest.mark.parametrize("state", ["not_created", "queued", "cancelled"])
def test_read_bound_status_without_creating_intent(tmp_path, state):  # type: ignore[no-untyped-def]
    client, _, repository, calls = status_client(tmp_path, state)
    with client:
        response = client.get(
            "/v1/operator/formal-round-dispatch",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200
    assert response.json()["state"] == state
    assert response.json()["execution_consumer_enabled"] is False
    assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 1
    assert not repository.intents


def test_status_auth_rejects_before_reader(tmp_path):  # type: ignore[no-untyped-def]
    client, management, _, calls = status_client(tmp_path)
    with client:
        assert client.get("/v1/operator/formal-round-dispatch").status_code == 403
        management.coordinator.clock = lambda: NOW + timedelta(hours=1)
        assert (
            client.get(
                "/v1/operator/formal-round-dispatch",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).status_code
            == 409
        )
    assert calls == []


def test_status_reader_binding_drift_rejected(tmp_path):  # type: ignore[no-untyped-def]
    client, _, _, _ = status_client(tmp_path, drift=True)
    with client:
        assert (
            client.get(
                "/v1/operator/formal-round-dispatch",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).status_code
            == 409
        )
