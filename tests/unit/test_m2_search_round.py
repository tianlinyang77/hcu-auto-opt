# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from datetime import datetime, timezone
from uuid import uuid4

from hcuopt.orchestrator.search_round import (
    candidate_family_hash,
    round_budget_ledger_document,
    round_budget_ledger_hash,
)


def _hash(value: str) -> str:
    return "sha256:" + value * 64


def _member(ordinal: int) -> dict:
    return {
        "ordinal": ordinal,
        "candidate_id": uuid4(),
        "round_candidate_id": uuid4(),
        "source_package_store_id": f"store-{ordinal}",
        "source_package_store_hash": _hash(str(ordinal + 1)),
        "source_package_hash": _hash(str(ordinal + 2)),
        "source_manifest_version": "m1-candidate-source-v1",
        "source_manifest_hash": _hash(str(ordinal + 3)),
        "baseline_source_hash": _hash("a"),
        "candidate_source_hash": _hash(str(ordinal + 4)),
        "replacement_point": "sglang.fixture.layer_norm",
        "candidate_kind": "fixture",
        "optimization_intent": f"known signal {ordinal}",
    }


def test_candidate_family_hash_is_order_independent_but_identity_sensitive() -> None:
    authority = {"hotspot_id": uuid4()}
    first = _member(0)
    second = _member(1)

    expected = candidate_family_hash(authority, [first, second])
    assert candidate_family_hash(authority, [second, first]) == expected
    assert (
        candidate_family_hash(
            authority,
            [first, {**second, "optimization_intent": "different intent"}],
        )
        != expected
    )


def test_budget_ledger_hash_is_order_independent_but_usage_sensitive() -> None:
    round_id = uuid4()
    first_reservation = {
        "reservation_id": uuid4(),
        "round_id": round_id,
        "job_id": uuid4(),
        "attempt": 1,
        "candidate_id": uuid4(),
        "phase": "search",
        "planned": {"search_samples": 10},
        "state": "settled",
        "idempotency_key": "m2-budget-reservation-first",
    }
    second_reservation = {
        **first_reservation,
        "reservation_id": uuid4(),
        "job_id": uuid4(),
        "idempotency_key": "m2-budget-reservation-second",
    }
    first_entry = {
        "ledger_entry_id": uuid4(),
        "reservation_id": first_reservation["reservation_id"],
        "round_id": round_id,
        "entry_type": "settle",
        "reserved": {"search_samples": 10},
        "actual": {"search_samples": 9},
        "lease_held_seconds": 2.0,
        "harness_active_seconds": 1.5,
        "raw_usage_evidence_hash": _hash("b"),
        "idempotency_key": "m2-budget-ledger-first",
        "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
    }
    second_entry = {
        **first_entry,
        "ledger_entry_id": uuid4(),
        "reservation_id": second_reservation["reservation_id"],
        "idempotency_key": "m2-budget-ledger-second",
    }

    expected = round_budget_ledger_hash(
        round_id,
        [first_reservation, second_reservation],
        [first_entry, second_entry],
    )
    assert round_budget_ledger_hash(
        round_id,
        [second_reservation, first_reservation],
        [second_entry, first_entry],
    ) == expected
    assert round_budget_ledger_document(round_id, [], [])["schema_version"] == (
        "m2a-round-budget-ledger-v1"
    )
    changed = {**second_entry, "actual": {"search_samples": 10}}
    assert (
        round_budget_ledger_hash(
            round_id,
            [first_reservation, second_reservation],
            [first_entry, changed],
        )
        != expected
    )
