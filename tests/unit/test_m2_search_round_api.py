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


def _usage(**updates) -> dict:
    payload = {
        "candidates": 0,
        "build_attempts": 0,
        "correctness_attempts": 0,
        "search_samples": 0,
        "holdout_samples": 0,
        "wall_seconds": 0.0,
        "exclusive_lease_seconds": 0.0,
    }
    payload.update(updates)
    return payload


def _budget_reservation(round_id: str) -> dict:
    return {
        "reservation_id": str(uuid4()),
        "round_id": round_id,
        "job_id": str(uuid4()),
        "attempt": 1,
        "candidate_id": str(uuid4()),
        "phase": "search",
        "planned": _usage(search_samples=20, exclusive_lease_seconds=10.0),
        "state": "reserved",
        "idempotency_key": "m2-budget-reservation-api",
    }


def _budget_entry(
    reservation: dict, entry_type: str, *, terminal: bool = False
) -> dict:
    actual = (
        _usage(search_samples=20, exclusive_lease_seconds=10.0)
        if terminal
        else _usage()
    )
    return {
        "ledger_entry_id": str(uuid4()),
        "reservation_id": reservation["reservation_id"],
        "round_id": reservation["round_id"],
        "entry_type": entry_type,
        "reserved": reservation["planned"],
        "actual": actual,
        "lease_held_seconds": 10.0 if terminal else 0.0,
        "harness_active_seconds": 8.0 if terminal else 0.0,
        "raw_usage_evidence_hash": _hash("f"),
        "idempotency_key": f"m2-budget-{entry_type}-api",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


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
    assert (
        "/v1/search-rounds/{round_id}/candidates/"
        "{round_candidate_id}/build-terminal"
    ) in paths
    assert "/v1/search-rounds/{round_id}/artifact-family:freeze" in paths
    assert "/v1/search-rounds/{round_id}/budget-reservations" in paths
    assert (
        "/v1/search-rounds/{round_id}/budget-reservations/"
        "{reservation_id}/finalize"
    ) in paths
    assert "/v1/search-rounds/{round_id}/barriers/search:close" in paths
    assert "/v1/search-rounds/{round_id}/holdout-plan:reveal" in paths
    assert "/v1/search-rounds/{round_id}/barriers/holdout:close" in paths
    assert "/v1/search-rounds/{round_id}/multiple-comparison" in paths
    assert "/v1/search-rounds/{round_id}/scripted:finalize" in paths
    assert "/v1/search-rounds/{round_id}/cancel" in paths
    assert "/v1/search-rounds/{round_id}/reconcile" in paths


def test_search_round_budget_api_preserves_atomic_pairs() -> None:
    repository = _repository()
    round_id = str(uuid4())
    reservation = _budget_reservation(round_id)
    reserve_entry = _budget_entry(reservation, "reserve")
    settle_entry = _budget_entry(reservation, "settle", terminal=True)
    persisted_reservation = _persisted(reservation)
    repository.reserve_round_budget.return_value = {
        "reservation": persisted_reservation,
        "ledger_entry": reserve_entry,
    }
    repository.finalize_round_budget.return_value = {
        "reservation": {**persisted_reservation, "state": "settled"},
        "ledger_entry": settle_entry,
    }

    with TestClient(create_app(repository=repository)) as client:
        reserved = client.post(
            f"/v1/search-rounds/{round_id}/budget-reservations",
            json={"reservation": reservation, "ledger_entry": reserve_entry},
        )
        settled = client.post(
            f"/v1/search-rounds/{round_id}/budget-reservations/"
            f"{reservation['reservation_id']}/finalize",
            json={"ledger_entry": settle_entry},
        )

    assert reserved.status_code == 201
    assert reserved.json()["reservation"]["state"] == "reserved"
    assert settled.status_code == 200
    assert settled.json()["reservation"]["state"] == "settled"
    repository.reserve_round_budget.assert_called_once()
    repository.finalize_round_budget.assert_called_once()


def test_search_round_budget_api_rejects_path_identity_mismatch() -> None:
    repository = _repository()
    reservation = _budget_reservation(str(uuid4()))
    reserve_entry = _budget_entry(reservation, "reserve")

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/search-rounds/{uuid4()}/budget-reservations",
            json={"reservation": reservation, "ledger_entry": reserve_entry},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    repository.reserve_round_budget.assert_not_called()


def test_search_round_budget_api_rejects_mismatched_reserve_pair() -> None:
    repository = _repository()
    reservation = _budget_reservation(str(uuid4()))
    reserve_entry = _budget_entry(reservation, "reserve")
    reserve_entry["reservation_id"] = str(uuid4())

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/search-rounds/{reservation['round_id']}/budget-reservations",
            json={"reservation": reservation, "ledger_entry": reserve_entry},
        )

    assert response.status_code == 422
    repository.reserve_round_budget.assert_not_called()


def test_search_round_artifact_api_records_and_freezes_build_family() -> None:
    repository = _repository()
    round_payload = _round_payload()
    candidate = _candidate_payload(round_payload["round_id"])
    artifact_id = str(uuid4())
    artifact_hash = _hash("1")
    built = {
        "round_id": round_payload["round_id"],
        "round_candidate_id": candidate["round_candidate_id"],
        "candidate_id": candidate["candidate_id"],
        "state": "built",
        "artifact_id": artifact_id,
        "artifact_hash": artifact_hash,
        "terminal_failure_code": None,
        "failure_evidence_hash": None,
    }
    persisted_candidate = _persisted(
        {
            **candidate,
            "state": "built",
            "artifact_id": artifact_id,
            "artifact_hash": artifact_hash,
        }
    )
    candidate_family_hash = _hash("2")
    artifact_family_hash = _hash("3")
    freeze = {
        "round_id": round_payload["round_id"],
        "candidate_family_hash": candidate_family_hash,
        "expected_artifact_family_hash": artifact_family_hash,
    }
    repository.record_round_candidate_build.return_value = persisted_candidate
    repository.freeze_search_round_artifact_family.return_value = {
        **_persisted(round_payload),
        "state": "correctness",
        "candidate_family_hash": candidate_family_hash,
        "artifact_family_hash": artifact_family_hash,
        "version": 5,
    }

    with TestClient(create_app(repository=repository)) as client:
        recorded = client.post(
            f"/v1/search-rounds/{round_payload['round_id']}/candidates/"
            f"{candidate['round_candidate_id']}/build-terminal",
            json=built,
        )
        frozen = client.post(
            f"/v1/search-rounds/{round_payload['round_id']}/artifact-family:freeze",
            json=freeze,
        )

    assert recorded.status_code == 200
    assert recorded.json()["artifact_hash"] == artifact_hash
    assert frozen.status_code == 200
    assert frozen.json()["state"] == "correctness"
    assert frozen.json()["artifact_family_hash"] == artifact_family_hash
    repository.record_round_candidate_build.assert_called_once()
    repository.freeze_search_round_artifact_family.assert_called_once()


def test_search_round_artifact_api_rejects_path_identity_mismatch() -> None:
    repository = _repository()
    payload = {
        "round_id": str(uuid4()),
        "round_candidate_id": str(uuid4()),
        "candidate_id": str(uuid4()),
        "state": "build_failed",
        "terminal_failure_code": "scripted_build_failure",
        "failure_evidence_hash": _hash("4"),
    }

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/search-rounds/{uuid4()}/candidates/"
            f"{payload['round_candidate_id']}/build-terminal",
            json=payload,
        )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    repository.record_round_candidate_build.assert_not_called()


def test_search_round_cancel_and_reconcile_expose_fail_closed_control_actions() -> None:
    repository = _repository()
    payload = _round_payload()
    cancelled = {
        **_persisted(payload),
        "state": "cancelled",
        "version": 2,
    }
    repository.cancel_scripted_search_round.return_value = cancelled
    repository.reconcile_scripted_search_round.return_value = {
        "round": cancelled,
        "consistent": True,
        "next_action": "none",
        "reason": "cancelled Round is terminal",
        "automatic_release_allowed": False,
    }

    with TestClient(create_app(repository=repository)) as client:
        cancel = client.post(
            f"/v1/search-rounds/{payload['round_id']}/cancel",
            json={"reason": "operator requested stop"},
        )
        reconcile = client.post(
            f"/v1/search-rounds/{payload['round_id']}/reconcile"
        )

    assert cancel.status_code == 200
    assert cancel.json()["state"] == "cancelled"
    assert reconcile.status_code == 200
    assert reconcile.json()["next_action"] == "none"
    assert reconcile.json()["automatic_release_allowed"] is False
    repository.cancel_scripted_search_round.assert_called_once()
    repository.reconcile_scripted_search_round.assert_called_once()
