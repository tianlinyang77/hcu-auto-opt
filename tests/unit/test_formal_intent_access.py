# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from datetime import timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.api.formal_start_management import FormalStartManagement
from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentRequest
from hcuopt.deployment.formal_intent_access import materialize_formal_intent_access
from tests.unit.test_formal_operator_plans import NOW
from tests.unit.test_formal_start_management_api import setup_management


def test_access_materialization_reload_and_http(tmp_path):  # type: ignore[no-untyped-def]
    management, repository, payload = setup_management(tmp_path)
    signed = FormalStartIntentRequest(
        **payload, actor_assertion=management.capabilities[0].assertion
    )
    result = materialize_formal_intent_access(
        signed,
        expected_actor_id=signed.actor_assertion.actor_id,
        verifier=management.coordinator.actor_verifier,
        now=NOW,
        private_parent=tmp_path / "private",
    )
    token = result.credential.read_text(encoding="utf-8")
    assert len(token) >= 43
    raw = result.configuration.read_text(encoding="utf-8")
    assert token not in raw and token not in repr(result)
    record = json.loads(raw)["capabilities"][0]
    assert record["token_sha256"] == sha256(token.encode()).hexdigest()
    assert record["assertion"] == signed.actor_assertion.model_dump(mode="json")
    for replayed in (False, True):
        loaded = FormalStartManagement.from_file(
            management.coordinator, deployment_root=result.directory, path=result.configuration
        )
        with TestClient(
            create_app(repository=repository, formal_start_management=loaded)
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            prepared = client.get("/v1/operator/formal-start-submission", headers=headers)
            assert prepared.status_code == 200
            assert prepared.json() == payload
            receipt = client.post(
                "/v1/operator/formal-start-intents", json=payload, headers=headers
            )
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["replayed"] is replayed
            assert receipt.json()["hcu_accessed"] is False
    assert len(repository.intents) == 1


@pytest.mark.parametrize(
    "failure",
    ["expired", "actor", "signature", "verifier_error", "naive", "subject", "signer", "truthy"],
)
def test_access_authority_failure_creates_no_files(tmp_path, failure):  # type: ignore[no-untyped-def]
    management, _, payload = setup_management(tmp_path)
    signed = FormalStartIntentRequest(
        **payload, actor_assertion=management.capabilities[0].assertion
    )
    verifier = management.coordinator.actor_verifier
    verifier.accepted = failure != "signature"
    verifier.raises = failure == "verifier_error"
    if failure == "subject":
        signed = signed.model_copy(update={"idempotency_key": "different-subject"})
    if failure == "signer":
        verifier.signer_ref = verifier.signer_ref.model_copy(update={"key_id": "wrong-key"})
    if failure == "truthy":
        verifier.accepted = 1
    now = NOW + timedelta(hours=1) if failure == "expired" else NOW
    if failure == "naive":
        now = now.replace(tzinfo=None)
    parent = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="authority was rejected"):
        materialize_formal_intent_access(
            signed,
            expected_actor_id="wrong" if failure == "actor" else signed.actor_assertion.actor_id,
            verifier=verifier,
            now=now,
            private_parent=parent,
        )
    assert not parent.exists()
