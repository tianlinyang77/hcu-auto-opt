# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from uuid import uuid4

import pytest

from hcuopt.adapters.m2_candidate import SCRIPTED_CANDIDATE_FIXTURES
from hcuopt.orchestrator.search_round import artifact_family_hash


def _hash(value: str) -> str:
    return "sha256:" + value * 64


def _round() -> dict:
    return {
        "candidate_family_hash": _hash("1"),
        "declared_candidate_count": 2,
    }


def _built(ordinal: int) -> dict:
    return {
        "ordinal": ordinal,
        "candidate_id": uuid4(),
        "round_candidate_id": uuid4(),
        "state": "built",
        "artifact_id": uuid4(),
        "artifact_hash": _hash(str(ordinal + 2)),
        "terminal_failure_code": None,
        "failure_evidence_hash": None,
    }


def _failed(ordinal: int) -> dict:
    return {
        "ordinal": ordinal,
        "candidate_id": uuid4(),
        "round_candidate_id": uuid4(),
        "state": "build_failed",
        "artifact_id": None,
        "artifact_hash": None,
        "terminal_failure_code": "scripted_build_failure",
        "failure_evidence_hash": _hash(str(ordinal + 4)),
    }


def test_scripted_fixture_registry_never_claims_performance() -> None:
    assert [item.fixture_id for item in SCRIPTED_CANDIDATE_FIXTURES] == [
        "noop",
        "known_faster",
        "known_slower",
        "build_failure",
    ]
    assert all(item.synthetic for item in SCRIPTED_CANDIDATE_FIXTURES)
    assert all(
        item.performance_conclusion == "not_measured"
        for item in SCRIPTED_CANDIDATE_FIXTURES
    )
    assert SCRIPTED_CANDIDATE_FIXTURES[-1].build_outcome == "build_failed"


def test_artifact_family_is_order_independent_and_keeps_failures() -> None:
    built = _built(0)
    failed = _failed(1)

    first = artifact_family_hash(_round(), [built, failed])
    replay = artifact_family_hash(_round(), [failed, built])

    assert first == replay
    assert first.startswith("sha256:")


def test_artifact_family_is_sensitive_to_artifact_and_failure_evidence() -> None:
    built = _built(0)
    failed = _failed(1)
    expected = artifact_family_hash(_round(), [built, failed])

    changed_artifact = {**built, "artifact_hash": _hash("a")}
    changed_failure = {**failed, "failure_evidence_hash": _hash("b")}

    assert artifact_family_hash(_round(), [changed_artifact, failed]) != expected
    assert artifact_family_hash(_round(), [built, changed_failure]) != expected


def test_artifact_family_rejects_partial_or_ambiguous_terminals() -> None:
    built = _built(0)
    failed = _failed(1)

    with pytest.raises(ValueError, match="complete identity pairs"):
        artifact_family_hash(
            _round(),
            [{**built, "artifact_hash": None}, failed],
        )
    with pytest.raises(ValueError, match="unambiguous Build terminal"):
        artifact_family_hash(
            _round(),
            [
                {
                    **built,
                    "terminal_failure_code": "also_failed",
                    "failure_evidence_hash": _hash("c"),
                },
                failed,
            ],
        )
    with pytest.raises(ValueError, match="declared Candidate count"):
        artifact_family_hash(_round(), [built])
