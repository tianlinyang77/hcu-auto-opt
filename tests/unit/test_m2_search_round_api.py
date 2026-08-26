# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

from hcuopt.api.app import create_app
from hcuopt.storage.repository import PostgresRepository


def _hash(value: str) -> str:
    return "sha256:" + value * 64


def _round_payload() -> dict:
    return {
        "round_id": str(uuid4()),
        "task_id": str(uuid4()),
        "idempotency_key": "m2-search-round-api",
        "state": "intake_open",
        "run_mode": "scripted",
        "project_mode": None,
        "target_snapshot_id": str(uuid4()),
        "stage0_run_id": str(uuid4()),
        "stage0_protocol_hash": _hash("1"),
        "baseline_epoch_id": str(uuid4()),
        "hotspot_id": str(uuid4()),
        "replacement_point": "sglang.fixture.layer_norm",
        "workload_id": "m2-scripted-workload-v1",
        "workload_hash": _hash("2"),
        "configuration_hash": _hash("3"),
        "image_digest": _hash("4"),
        "adapter_profile": "m2-scripted-v1",
        "declared_candidate_count": 2,
        "max_promoted": 2,
        "family_alpha": 0.05,
        "search_plan_hash": _hash("5"),
        "holdout_plan_commitment": _hash("6"),
        "holdout_plan_authority_id": "synthetic-holdout-v1",
        "holdout_plan_authority_hash": _hash("7"),
        "selection_rule_hash": _hash("8"),
        "budget": {
            "max_candidates": 2,
            "max_build_attempts": 4,
            "max_correctness_attempts": 4,
            "max_search_samples": 200,
            "max_holdout_samples": 200,
            "max_wall_seconds": 600,
            "max_exclusive_lease_seconds": 300,
        },
        "automatic_release_allowed": False,
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _candidate_payload(round_id: str) -> dict:
    return {
        "round_candidate_id": str(uuid4()),
        "round_id": round_id,
        "candidate_id": str(uuid4()),
        "ordinal": 0,
        "source_package_store_id": "synthetic-store-0",
        "source_package_store_hash": _hash("9"),
        "source_package_hash": _hash("a"),
        "source_manifest_hash": _hash("b"),
        "baseline_source_hash": _hash("c"),
        "candidate_source_hash": _hash("d"),
        "optimization_intent": "known scripted signal",
        "replacement_point": "sglang.fixture.layer_norm",
        "candidate_kind": "fixture",
        "state": "intake_accepted",
        "idempotency_key": "m2-round-candidate-api-0",
    }


def _persisted(payload: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {**payload, "created_at": payload.get("created_at", now), "updated_at": now}


def _repository() -> Mock:
    repository = Mock(spec=PostgresRepository)
    repository.create_search_round.side_effect = lambda request: _persisted(
        request.model_dump(mode="json")
    )
    repository.add_round_candidate.side_effect = lambda request: _persisted(
        request.model_dump(mode="json")
    )
    return repository


def test_search_round_api_exposes_scripted_control_plane() -> None:
    repository = _repository()
    round_payload = _round_payload()
    candidate_payload = _candidate_payload(round_payload["round_id"])
    persisted_round = _persisted(round_payload)
    persisted_candidate = _persisted(candidate_payload)
    repository.get_search_round.return_value = persisted_round
    repository.close_search_round_intake.return_value = {
        **persisted_round,
        "state": "intake_closed",
        "candidate_family_hash": _hash("e"),
        "version": 2,
        "intake_closed_at": datetime.now(timezone.utc).isoformat(),
    }
    repository.search_round_summary.return_value = {
        "round": persisted_round,
        "candidates": [persisted_candidate],
        "budget_reservations": [],
        "budget_ledger": [],
        "automatic_release_allowed": False,
    }

    with TestClient(create_app(repository=repository)) as client:
        created = client.post("/v1/search-rounds", json=round_payload)
        fetched = client.get(f"/v1/search-rounds/{round_payload['round_id']}")
        summary = client.get(
            f"/v1/search-rounds/{round_payload['round_id']}/summary"
        )
        candidate = client.post(
            f"/v1/search-rounds/{round_payload['round_id']}/candidates",
            json=candidate_payload,
        )
        closed = client.post(
            f"/v1/search-rounds/{round_payload['round_id']}/intake-close"
        )

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json()["round_id"] == round_payload["round_id"]
    assert summary.status_code == 200
    assert summary.json()["automatic_release_allowed"] is False
    assert candidate.status_code == 201
    assert closed.status_code == 200
    assert closed.json()["state"] == "intake_closed"
    repository.create_search_round.assert_called_once()
    repository.add_round_candidate.assert_called_once()
    repository.close_search_round_intake.assert_called_once()


def test_candidate_path_round_identity_must_match_payload() -> None:
    repository = _repository()
    payload = _candidate_payload(str(uuid4()))

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/search-rounds/{uuid4()}/candidates",
            json=payload,
        )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    repository.add_round_candidate.assert_not_called()


def test_search_round_api_is_declared_in_openapi() -> None:
    paths = create_app().openapi()["paths"]

    assert "/v1/search-rounds" in paths
    assert "/v1/search-rounds/{round_id}" in paths
    assert "/v1/search-rounds/{round_id}/summary" in paths
    assert "/v1/search-rounds/{round_id}/candidates" in paths
    assert "/v1/search-rounds/{round_id}/intake-close" in paths
