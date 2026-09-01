# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from hcuopt.adapters.agent_generator import (
    CandidateProposalBatchStore,
    DeterministicCandidateGeneratorAdapter,
    ProposalPatchStore,
)
from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
from hcuopt.adapters.interfaces import CandidateGeneratorAdapter
from hcuopt.agent.identity import (
    candidate_generation_request_hash,
    candidate_proposal_batch_hash,
    knowledge_snapshot_hash,
)
from hcuopt.contracts.agent_v1 import CandidateGenerationRequest, KnowledgeSnapshot
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
PATCH = b"""diff --git a/sglang/runtime/operator.py b/sglang/runtime/operator.py
--- a/sglang/runtime/operator.py
+++ b/sglang/runtime/operator.py
@@ -1 +1 @@
-return value
+return value + 0
"""


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _knowledge(root: Path) -> tuple[KnowledgeSnapshotStore, KnowledgeSnapshot]:
    payload = b"approved operator guidance\n"
    snapshot = KnowledgeSnapshot(
        snapshot_id=UUID("62000000-0000-0000-0000-000000000001"),
        sources=(
            {
                "knowledge_id": "skill/operator-guidance",
                "source_kind": "skill",
                "version": "1.0.0",
                "source_uri": "skill:///operator-guidance/SKILL.md",
                "content_hash": _hash(payload),
                "license_id": "MulanPSL-2.0",
            },
        ),
        created_by="candidate-generator-test",
        created_at=NOW,
    )
    store = KnowledgeSnapshotStore(root, profile="m2b-generator-test-v1")
    store.publish(snapshot, {("skill/operator-guidance", "1.0.0"): payload})
    return store, snapshot


def _request(snapshot: KnowledgeSnapshot) -> CandidateGenerationRequest:
    return CandidateGenerationRequest(
        request_id=UUID("62000000-0000-0000-0000-000000000002"),
        generation_run_id=UUID("62000000-0000-0000-0000-000000000003"),
        target_snapshot_id=UUID("62000000-0000-0000-0000-000000000004"),
        stage0_run_id=UUID("62000000-0000-0000-0000-000000000005"),
        baseline_epoch_id=UUID("62000000-0000-0000-0000-000000000006"),
        baseline_source_hash=_hash(b"baseline"),
        hotspot_id=UUID("62000000-0000-0000-0000-000000000007"),
        replacement_point="sglang.runtime.operator.forward",
        workload_id="m2b-generator-test",
        workload_hash=_hash(b"workload"),
        configuration_hash=_hash(b"configuration"),
        image_digest=_hash(b"image"),
        profiler_evidence_uri="evidence:///profile.json",
        profiler_evidence_hash=_hash(b"profile"),
        knowledge_snapshot_id=snapshot.snapshot_id,
        knowledge_snapshot_hash=knowledge_snapshot_hash(snapshot),
        max_proposals=1,
    )


def _generator(tmp_path: Path):
    knowledge_store, snapshot = _knowledge(tmp_path / "knowledge")
    patch_store = ProposalPatchStore(tmp_path / "proposal-store", profile="m2b-c-v1")
    batch_store = CandidateProposalBatchStore(
        tmp_path / "proposal-store",
        profile="m2b-c-v1",
    )
    generator = DeterministicCandidateGeneratorAdapter(
        generator_id="deterministic-c",
        profile="m2b-deterministic-generator-v1",
        knowledge_store=knowledge_store,
        patch_store=patch_store,
        batch_store=batch_store,
        raw_patch=PATCH,
        touched_paths=("sglang/runtime/operator.py",),
        optimization_intent="remove a redundant materialization",
        rationale="Exercise the proposal-only control path.",
        risk_summary="Independent correctness review remains required.",
        clock=lambda: NOW,
    )
    return generator, patch_store, batch_store, _request(snapshot)


def test_deterministic_generator_implements_protocol_and_publishes_replayable_batch(
    tmp_path: Path,
) -> None:
    generator, patch_store, batch_store, request = _generator(tmp_path)

    first = generator.generate_proposals(request, tmp_path / "output")
    replay = generator.generate_proposals(request, tmp_path / "output")
    stored = batch_store.load(
        candidate_proposal_batch_hash(first),
        expected_batch_id=first.batch_id,
    )
    proposal = first.proposals[0]

    assert isinstance(generator, CandidateGeneratorAdapter)
    assert first == replay == stored.batch
    assert first.request_hash == candidate_generation_request_hash(request)
    assert patch_store.read(
        proposal.patch_uri,
        expected_patch_hash=proposal.patch_hash,
        expected_normalized_patch_hash=proposal.normalized_patch_hash,
    ) == PATCH
    assert first.synthetic
    assert first.performance_conclusion == "not_measured"
    assert not first.automatic_release_allowed


def test_generator_fails_before_output_when_knowledge_authority_drifted(
    tmp_path: Path,
) -> None:
    generator, _patch_store, _batch_store, request = _generator(tmp_path)
    digest = request.knowledge_snapshot_hash.removeprefix("sha256:")
    snapshot_path = (
        tmp_path
        / "knowledge"
        / "snapshots"
        / "sha256"
        / digest[:2]
        / digest[2:]
        / "snapshot.json"
    )
    snapshot = KnowledgeSnapshot.model_validate_json(snapshot_path.read_bytes())
    snapshot_path.write_bytes(
        canonical_json_bytes(snapshot.model_copy(update={"created_by": "changed"}))
    )

    with pytest.raises(SourceArtifactError, match="authority Hash"):
        generator.generate_proposals(request, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_patch_and_batch_stores_reject_post_publication_tampering(tmp_path: Path) -> None:
    generator, patch_store, batch_store, request = _generator(tmp_path)
    batch = generator.generate_proposals(request, tmp_path / "output")
    proposal = batch.proposals[0]
    patch_path = file_uri_to_path(proposal.patch_uri)
    patch_path.write_bytes(PATCH + b"# tampered\n")

    with pytest.raises(SourceArtifactError, match="raw Hash"):
        patch_store.read(
            proposal.patch_uri,
            expected_patch_hash=proposal.patch_hash,
            expected_normalized_patch_hash=proposal.normalized_patch_hash,
        )

    stored = batch_store.load(candidate_proposal_batch_hash(batch))
    changed = batch.model_copy(update={"raw_output_hash": _hash(b"changed")})
    file_uri_to_path(stored.uri).write_bytes(canonical_json_bytes(changed))
    with pytest.raises(SourceArtifactError, match="content changed"):
        batch_store.load(candidate_proposal_batch_hash(batch))
