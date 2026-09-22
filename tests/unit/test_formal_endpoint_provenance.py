# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from copy import deepcopy
from uuid import uuid4

import pytest

from hcuopt.contracts.endpoint_source_v1 import FormalEndpointSource
from hcuopt.domain.errors import Conflict
from hcuopt.storage.endpoint_validation import EndpointValidationRepositoryMixin


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, args):
        assert query.startswith("SELECT") and query.endswith("FOR SHARE")
        self.calls.append((query, args))
        self.current = self.rows.get(query.split()[3])
        return self

    def fetchone(self):
        return self.current


def fixture():
    ids = {
        key: uuid4()
        for key in (
            "task_id",
            "round_id",
            "candidate_id",
            "baseline_epoch_id",
            "target_snapshot_id",
            "artifact_id",
            "round_evidence_bundle_id",
            "round_signoff_id",
        )
    }
    hashes = {
        key: "sha256:" + "a" * 64
        for key in (
            "candidate_source_hash",
            "artifact_hash",
            "evidence_bundle_hash",
            "decision_artifact_hash",
        )
    }
    source = FormalEndpointSource(kind="signed_formal_round", **ids, **hashes)
    authority = dict(run_mode="formal", synthetic=False, automatic_release_allowed=False)
    common = dict(authority, task_id=source.task_id, round_id=source.round_id)
    comparison_id = uuid4()
    rows = {
        "search_rounds": dict(
            common,
            state="completed",
            baseline_epoch_id=source.baseline_epoch_id,
            target_snapshot_id=source.target_snapshot_id,
        ),
        "tasks": dict(
            state="completed",
            automatic_release_allowed=False,
            target_snapshot_id=source.target_snapshot_id,
        ),
        "formal_round_signoffs": dict(
            common,
            decision="approved",
            round_evidence_bundle_id=source.round_evidence_bundle_id,
            evidence_bundle_hash=source.evidence_bundle_hash,
            decision_artifact_hash=source.decision_artifact_hash,
        ),
        "formal_round_evidence_bundles": dict(
            common,
            payload_hash=source.evidence_bundle_hash,
            payload={
                "multiple_comparison_id": str(comparison_id),
                "summary": {
                    "endpoint_selection": {
                        "rule": "frozen_search_rank_after_batch_d_v1",
                        "selected_candidate_id": str(source.candidate_id),
                        "status": "selected",
                    },
                },
            },
        ),
        "artifacts": dict(
            task_id=source.task_id,
            candidate_id=source.candidate_id,
            content_hash=source.artifact_hash,
            synthetic=False,
        ),
        "baseline_epochs": {"task_id": source.task_id},
        "round_candidates": dict(
            candidate_source_hash=source.candidate_source_hash,
            artifact_id=source.artifact_id,
            artifact_hash=source.artifact_hash,
        ),
        "formal_multiple_comparison_results": dict(
            authority,
            multiple_comparison_id=comparison_id,
            payload={
                "candidate_results": [
                    {
                        "candidate_id": str(source.candidate_id),
                        "verdict": "faster",
                        "synthetic": False,
                    },
                ]
            },
        ),
    }
    return source, rows


def test_signed_formal_provenance_is_reread_without_creating_jobs():
    source, rows = fixture()
    connection = Connection(rows)
    EndpointValidationRepositoryMixin._verify_formal_endpoint_source(connection, source)
    assert len(connection.calls) == 8
    assert connection.calls[-2][1] == (source.round_id, source.candidate_id)


@pytest.mark.parametrize(
    "table,key,value",
    [
        ("search_rounds", "state", "awaiting_signoff"),
        ("search_rounds", "run_mode", "scripted"),
        ("search_rounds", "baseline_epoch_id", uuid4()),
        ("formal_round_signoffs", "decision", "rejected"),
        ("formal_round_signoffs", "evidence_bundle_hash", "sha256:" + "b" * 64),
        ("formal_round_evidence_bundles", "synthetic", True),
        ("artifacts", "candidate_id", uuid4()),
        ("round_candidates", "artifact_id", uuid4()),
        ("round_candidates", "candidate_source_hash", "sha256:" + "b" * 64),
        ("formal_multiple_comparison_results", "multiple_comparison_id", uuid4()),
    ],
)
def test_rejects_cross_bound_or_unsigned_source(table, key, value):
    source, rows = fixture()
    rows[table][key] = value
    with pytest.raises(Conflict):
        EndpointValidationRepositoryMixin._verify_formal_endpoint_source(Connection(rows), source)


@pytest.mark.parametrize("verdict", ["inconclusive", "slower", "invalid"])
def test_rejected_candidate_cannot_enter_endpoint(verdict):
    source, rows = fixture()
    rows["formal_multiple_comparison_results"]["payload"]["candidate_results"][0]["verdict"] = (
        verdict
    )
    with pytest.raises(Conflict):
        EndpointValidationRepositoryMixin._verify_formal_endpoint_source(Connection(rows), source)


def test_missing_durable_records_fail_closed():
    source, rows = fixture()
    for table in rows:
        missing = deepcopy(rows)
        del missing[table]
        with pytest.raises(Conflict):
            EndpointValidationRepositoryMixin._verify_formal_endpoint_source(
                Connection(missing), source
            )


@pytest.mark.parametrize(
    "selection",
    [
        None,
        {},
        {
            "rule": "frozen_search_rank_after_batch_d_v1",
            "status": "selected",
            "selected_candidate_id": str(uuid4()),
        },
        {
            "rule": "holdout_best",
            "status": "selected",
        },
    ],
)
def test_source_requires_signed_search_selection(selection):
    source, rows = fixture()
    rows["formal_round_evidence_bundles"]["payload"]["summary"] = {
        "endpoint_selection": selection,
    }
    with pytest.raises(Conflict, match="Search-selected"):
        EndpointValidationRepositoryMixin._verify_formal_endpoint_source(Connection(rows), source)
