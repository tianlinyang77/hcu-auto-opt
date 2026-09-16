# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Promote one approved BW20 Agent Proposal into the normal M1 build queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.adapters.agent_promotion import (
    BaselineOverlaySource,
    CandidateSourcePackagePublisher,
    ProposalDecisionStore,
    ProposalPromotionService,
    ProposalReviewAuthority,
)
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.agent.messages_worker import MessagesGenerationWorker
from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.contracts.v1 import ManualCandidateCreate
from hcuopt.deployment.bw20_agent_m1_intake import PROFILE
from hcuopt.deployment.nmz36_m1_allocator import (
    ALLOCATOR_MOUNT_TARGET,
    ALLOCATOR_RELATIVE_PATH,
    ALLOCATOR_REPLACEMENT_POINT,
)
from hcuopt.domain.enums import ManualCandidateKind
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path
from hcuopt.storage.repository import PostgresRepository


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def candidate_id_for(candidate_key: str) -> UUID:
    """Return the existing M1 Candidate authority identity for one key."""

    return uuid5(NAMESPACE_URL, f"hcuopt:m1-candidate:{candidate_key}")


def promote(
    repository: Any,
    *,
    generation_run_id: UUID,
    proposal_id: UUID,
    review_id: UUID,
    task_id: UUID,
    baseline_snapshot_file: Path,
    store_root: Path,
    source_package_root: Path,
    candidate_output_dir: Path,
    candidate_key: str,
    completed_at: datetime,
) -> dict[str, Any]:
    """Reread approved authority, publish one package, and enqueue M1 Build."""

    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise ValueError("BW20 Agent M1 completion time must be timezone-aware")
    status = repository.generation_run_status(generation_run_id)
    if status.run.generation_run_id != generation_run_id:
        raise RuntimeError("BW20 Agent Generation Run identity drifted")
    request = status.run.request
    task = repository.manual_candidate_summary(task_id)
    baseline_epoch_id = UUID(str(task["baseline"]["baseline_epoch_id"]))
    if baseline_epoch_id != request.baseline_epoch_id:
        raise RuntimeError("BW20 Agent Proposal does not belong to the selected M1 Task")

    baseline = SourceSnapshot.model_validate_json(
        baseline_snapshot_file.resolve(strict=True).read_bytes()
    )
    manager = GitSourceManager(PROFILE)
    manager._assert_snapshot_unchanged(  # noqa: SLF001 - deployment revalidation boundary
        baseline,
        file_uri_to_path(baseline.worktree_uri),
    )
    if (
        baseline.kind != "baseline"
        or not baseline.clean
        or baseline.source_hash != request.baseline_source_hash
    ):
        raise RuntimeError("BW20 Agent Proposal Baseline authority drifted")

    worker = MessagesGenerationWorker(store_root.resolve(strict=True))
    decisions = ProposalDecisionStore(store_root / "decisions")
    review_authority = ProposalReviewAuthority(
        patch_store=worker.patches,
        batch_store=worker.batches,
        decision_store=decisions,
    )
    publisher = CandidateSourcePackagePublisher(
        source_package_root,
        profile=PROFILE,
        source_manager=manager,
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={
            ALLOCATOR_REPLACEMENT_POINT: ALLOCATOR_MOUNT_TARGET,
        },
    )
    service = ProposalPromotionService(
        review_authority=review_authority,
        package_publisher=publisher,
        decision_store=decisions,
    )
    candidate_id = candidate_id_for(candidate_key)
    prepared = service.prepare(
        status,
        proposal_id,
        review_id,
        baseline=BaselineOverlaySource(
            snapshot=baseline,
            path=ALLOCATOR_RELATIVE_PATH,
        ),
        candidate_id=candidate_id,
        candidate_output_dir=candidate_output_dir,
    )
    proposal = prepared.resolved.proposal
    row = repository.create_manual_candidate(
        task_id,
        ManualCandidateCreate(
            hotspot_id=request.hotspot_id,
            baseline_epoch_id=baseline_epoch_id,
            source_hash=prepared.source_package_ref.candidate_source_hash,
            optimization_intent=proposal.optimization_intent,
            replacement_point=request.replacement_point,
            track="triton",
            release_mode="overlay",
            candidate_kind=ManualCandidateKind.BUSINESS,
            idempotency_key=candidate_key,
        ),
    )
    if UUID(str(row["candidate_id"])) != candidate_id:
        raise RuntimeError("registered BW20 Agent Candidate identity drifted")

    summary = {
        "schema_version": "bw20-agent-m1-generation-review-v1",
        "generation_run_id": str(generation_run_id),
        "proposal_id": str(proposal_id),
        "review_id": str(review_id),
        "decision": "approved_for_m1_candidate_build",
        "candidate_id": str(candidate_id),
        "source_package_ref": prepared.source_package_ref.model_dump(mode="json"),
        "candidate_created": True,
        "hcu_accessed": False,
        "performance_conclusion": "not_measured",
        "automatic_release_allowed": False,
    }
    review_evidence = decisions.publish_evidence(
        "generation-review",
        canonical_json_bytes(summary),
    )
    completed = repository.complete_generation_review(
        generation_run_id,
        review_evidence_uri=review_evidence.uri,
        review_evidence_hash=review_evidence.content_hash,
        now=completed_at,
    )
    worktrees = candidate_output_dir / "worktrees"
    if worktrees.exists() and any(worktrees.iterdir()):
        raise RuntimeError("BW20 Agent Candidate Worktree cleanup is incomplete")
    return {
        "schema_version": "bw20-agent-m1-promotion-result-v1",
        "generation_run_id": str(generation_run_id),
        "generation_state": completed.state,
        "proposal_id": str(proposal_id),
        "review_id": str(review_id),
        "candidate_id": str(candidate_id),
        "candidate_state": row["state"],
        "candidate_source_hash": prepared.source_package_ref.candidate_source_hash,
        "source_package_hash": prepared.source_package_ref.source_package_hash,
        "manifest_hash": prepared.source_package_ref.manifest_hash,
        "review_evidence_uri": review_evidence.uri,
        "review_evidence_hash": review_evidence.content_hash,
        "worktree_cleanup": "verified",
        "hcu_accessed": False,
        "performance_conclusion": "not_measured",
        "automatic_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("generation_run_id", type=UUID)
    parser.add_argument("proposal_id", type=UUID)
    parser.add_argument("review_id", type=UUID)
    parser.add_argument("--task-id", type=UUID, required=True)
    parser.add_argument("--baseline-snapshot", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--source-package-root", type=Path, required=True)
    parser.add_argument("--candidate-output-dir", type=Path, required=True)
    parser.add_argument("--candidate-key", required=True)
    parser.add_argument("--completed-at", required=True)
    args = parser.parse_args(argv)
    database_url = os.getenv("HCUOPT_DATABASE_URL")
    if not database_url:
        parser.error("HCUOPT_DATABASE_URL is required")
    completed_at = datetime.fromisoformat(args.completed_at.replace("Z", "+00:00"))
    result = promote(
        PostgresRepository(database_url),
        generation_run_id=args.generation_run_id,
        proposal_id=args.proposal_id,
        review_id=args.review_id,
        task_id=args.task_id,
        baseline_snapshot_file=args.baseline_snapshot,
        store_root=args.store_root,
        source_package_root=args.source_package_root,
        candidate_output_dir=args.candidate_output_dir,
        candidate_key=args.candidate_key,
        completed_at=completed_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["candidate_id_for", "main", "promote"]
