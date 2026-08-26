# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from uuid import uuid4

from hcuopt.orchestrator.search_round import candidate_family_hash


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
