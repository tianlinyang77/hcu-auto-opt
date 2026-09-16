# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.deployment import bw20_agent_m1_promotion as deployment


def _hash(value: str) -> str:
    return "sha256:" + value * 64


def test_candidate_id_uses_existing_m1_identity_rule() -> None:
    assert deployment.candidate_id_for("candidate-key") == UUID(
        "8854122e-e43a-5a40-a043-5e6663827914"
    )


def test_promotion_requires_timezone_before_repository_access(tmp_path: Path) -> None:
    class Repository:
        def generation_run_status(self, _run_id):  # type: ignore[no-untyped-def]
            raise AssertionError("repository must not be read")

    with pytest.raises(ValueError, match="timezone-aware"):
        deployment.promote(
            Repository(),
            generation_run_id=uuid4(),
            proposal_id=uuid4(),
            review_id=uuid4(),
            task_id=uuid4(),
            baseline_snapshot_file=tmp_path / "missing.json",
            store_root=tmp_path,
            source_package_root=tmp_path / "packages",
            candidate_output_dir=tmp_path / "work",
            candidate_key="candidate-key",
            completed_at=datetime(2026, 9, 15),
        )


def test_promotion_registers_one_package_and_preserves_safety_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, proposal_id, review_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    baseline_epoch_id, hotspot_id = uuid4(), uuid4()
    baseline_hash = _hash("a")
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    snapshot = SourceSnapshot(
        kind="baseline",
        repository="https://example.invalid/sglang.git",
        commit="1" * 40,
        tree_hash="2" * 40,
        source_hash=baseline_hash,
        worktree_uri=baseline_root.as_uri(),
        clean=True,
    )
    snapshot_file = tmp_path / "baseline.json"
    snapshot_file.write_text(snapshot.model_dump_json(), encoding="utf-8")
    store_root = tmp_path / "store"
    store_root.mkdir()
    request = SimpleNamespace(
        baseline_epoch_id=baseline_epoch_id,
        baseline_source_hash=baseline_hash,
        hotspot_id=hotspot_id,
        replacement_point=deployment.ALLOCATOR_REPLACEMENT_POINT,
    )
    status = SimpleNamespace(run=SimpleNamespace(generation_run_id=run_id, request=request))
    package_ref = CandidateSourcePackageRef(
        candidate_source_hash=_hash("b"),
        source_package_hash=_hash("c"),
        manifest_hash=_hash("d"),
        manifest_schema_version="m1-candidate-source-v1",
    )
    prepared = SimpleNamespace(
        resolved=SimpleNamespace(
            proposal=SimpleNamespace(optimization_intent="use adjacent page runs")
        ),
        source_package_ref=package_ref,
    )
    published: list[bytes] = []

    class Repository:
        def generation_run_status(self, value):  # type: ignore[no-untyped-def]
            assert value == run_id
            return status

        def manual_candidate_summary(self, value):  # type: ignore[no-untyped-def]
            assert value == task_id
            return {"baseline": {"baseline_epoch_id": baseline_epoch_id}}

        def create_manual_candidate(self, value, candidate):  # type: ignore[no-untyped-def]
            assert value == task_id
            self.candidate = candidate
            return {
                "candidate_id": deployment.candidate_id_for("candidate-key"),
                "state": "manual_building",
            }

        def complete_generation_review(self, value, **kwargs):  # type: ignore[no-untyped-def]
            assert value == run_id
            assert kwargs["review_evidence_hash"] == _hash("e")
            assert kwargs["now"] == datetime(2026, 9, 15, tzinfo=timezone.utc)
            return SimpleNamespace(state="completed")

    repository = Repository()

    class SourceManager:
        provenance = SimpleNamespace(implementation_kind="real")

        def __init__(self, _profile):
            pass

        def _assert_snapshot_unchanged(self, value, root):  # type: ignore[no-untyped-def]
            assert value == snapshot
            assert root == baseline_root

    class Worker:
        def __init__(self, root):
            self.patches = ("patches", root)
            self.batches = ("batches", root)

    class Decisions:
        def __init__(self, root):
            self.root = root

        def publish_evidence(self, kind, payload):  # type: ignore[no-untyped-def]
            assert kind == "generation-review"
            published.append(payload)
            return SimpleNamespace(uri="file:///review.json", content_hash=_hash("e"))

    class Service:
        def __init__(self, **_kwargs):
            pass

        def prepare(self, run, proposal, review, **kwargs):  # type: ignore[no-untyped-def]
            assert (run, proposal, review) == (status, proposal_id, review_id)
            assert kwargs["candidate_id"] == deployment.candidate_id_for("candidate-key")
            return prepared

    monkeypatch.setattr(deployment, "GitSourceManager", SourceManager)
    monkeypatch.setattr(deployment, "MessagesGenerationWorker", Worker)
    monkeypatch.setattr(deployment, "ProposalDecisionStore", Decisions)
    monkeypatch.setattr(deployment, "ProposalReviewAuthority", lambda **kwargs: kwargs)
    monkeypatch.setattr(deployment, "CandidateSourcePackagePublisher", lambda *a, **k: k)
    monkeypatch.setattr(deployment, "ProposalPromotionService", Service)

    result = deployment.promote(
        repository,
        generation_run_id=run_id,
        proposal_id=proposal_id,
        review_id=review_id,
        task_id=task_id,
        baseline_snapshot_file=snapshot_file,
        store_root=store_root,
        source_package_root=tmp_path / "packages",
        candidate_output_dir=tmp_path / "work",
        candidate_key="candidate-key",
        completed_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )

    assert repository.candidate.source_hash == package_ref.candidate_source_hash
    assert repository.candidate.hotspot_id == hotspot_id
    assert result["candidate_state"] == "manual_building"
    assert result["hcu_accessed"] is False
    assert result["performance_conclusion"] == "not_measured"
    assert result["automatic_release_allowed"] is False
    summary = json.loads(published[0])
    assert summary["decision"] == "approved_for_m1_candidate_build"
    assert summary["candidate_created"] is True
    assert summary["hcu_accessed"] is False
    assert summary["automatic_release_allowed"] is False
