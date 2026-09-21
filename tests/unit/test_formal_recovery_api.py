# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from hcuopt.api.formal_start_management import FormalIntentSubmission
from hcuopt.contracts.formal_recovery_v1 import FormalRecoveryObservation
from hcuopt.contracts.m2_formal_start_v1 import derive_formal_start_ids
from tests.unit.test_formal_start_management_api import NOW, TOKEN, client_for, setup_management


def setup(tmp_path):
    management, repo, payload = setup_management(tmp_path)
    intent_id, _, round_id = derive_formal_start_ids(payload["idempotency_key"])
    hash_ = "sha256:" + "a" * 64
    observation = FormalRecoveryObservation(
        intent_id=intent_id, round_id=round_id, resolved_plan_hash=payload["resolved_plan_hash"],
        service_identity=management.coordinator.service_identity,
        report=dict(schema_version="formal-correctness-recovery-v1", job_id=uuid4(),
                    input_hash=hash_, snapshot_hash=hash_, job_state="running",
                    status="unknown_requires_manual_recovery", resource_id="fixture",
                    resource_state="active", resource_owned_by_attempt=True,
                    budget_state="reserved", reservation_id=uuid4(), recorded_result=False,
                    release_recorded=False, reconciliation_allowed=False,
                    execution_retry_allowed=False, automatic_release_allowed=False,
                    required_manual_checks=["inspect_exact_job_owned_containers_and_processes"]),
    )
    reader = Mock()
    reader.read_status.return_value = observation
    management = replace(management, recovery_reader=reader, capabilities=(replace(
        management.capabilities[0], submission=FormalIntentSubmission.model_validate(payload),
    ),))
    return management, repo, reader, observation


def test_scoped_read_has_no_mutation_route(tmp_path):
    management, repo, reader, observed = setup(tmp_path)
    with client_for(management, repo) as client:
        response = client.get("/v1/operator/formal-correctness-recovery",
                              headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["web_reconciliation_allowed"] is False
        assert TOKEN not in response.text
        assert client.post("/v1/operator/formal-correctness-recovery").status_code == 405
    reader.read_status.assert_called_once_with(observed.intent_id)
    assert not repo.intents


@pytest.mark.parametrize("case", ["missing", "wrong", "expired", "signature", "drift"])
def test_recovery_authority_rejected(tmp_path, case):
    management, repo, reader, observed = setup(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if case == "missing":
        headers = {}
    if case == "wrong":
        headers = {"Authorization": "Bearer wrong"}
    if case == "expired":
        management.coordinator.clock = lambda: NOW + timedelta(hours=1)
    if case == "signature":
        management.coordinator.actor_verifier.accepted = False
    if case == "drift":
        reader.read_status.return_value = observed.model_copy(update={"round_id": uuid4()})
    with client_for(management, repo) as client:
        response = client.get("/v1/operator/formal-correctness-recovery", headers=headers)
    assert response.status_code in {403, 409}
    if case != "drift":
        reader.read_status.assert_not_called()
