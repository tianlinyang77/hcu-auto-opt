# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import Error as DatabaseError
from pydantic import ValidationError

from hcuopt.agent.authority import AgentAuthorityError, ApexGenerationCoordinator
from hcuopt.agent.identity import verify_candidate_proposal_review_record
from hcuopt.contracts.agent_v1 import GenerationRunStartRequest
from hcuopt.domain.errors import ContractError
from hcuopt.evaluation.agent_generation_read_model import (
    AgentGenerationEvidenceReadService,
)
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.storage.repository import PostgresRepository

AGENT_GENERATION_COMMANDS = {
    "agent-generation-start",
    "agent-generation-status",
    "agent-generation-reconcile",
    "agent-generation-evidence",
    "agent-messages-run-once",
    "agent-messages-recover",
    "agent-messages-prepare-inspection",
    "agent-proposal-review",
    "agent-generation-close-rejected",
}


def configure_agent_generation_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    start = subparsers.add_parser(
        "agent-generation-start",
        help="create or replay one dev-only Agent/Apex Generation Run",
    )
    start.add_argument("request", type=Path)
    start.add_argument("--database-url")

    status = subparsers.add_parser(
        "agent-generation-status",
        help="read one Agent/Apex Generation Run and its Attempt/Proposal refs",
    )
    status.add_argument("generation_run_id", type=UUID)
    status.add_argument("--database-url")

    reconcile = subparsers.add_parser(
        "agent-generation-reconcile",
        help="recover stale Attempts and converge one Agent/Apex Generation Run",
    )
    reconcile.add_argument("generation_run_id", type=UUID)
    reconcile.add_argument("--database-url")

    evidence = subparsers.add_parser(
        "agent-generation-evidence",
        help="rebuild and verify one durable Agent Generation evidence read model",
    )
    evidence.add_argument("generation_run_id", type=UUID)
    evidence.add_argument("--database-url")
    evidence.add_argument(
        "--evidence-root",
        type=Path,
        default=os.getenv("HCUOPT_AGENT_EVIDENCE_ROOT"),
    )
    for name in (
        "agent-messages-run-once",
        "agent-messages-recover",
        "agent-messages-prepare-inspection",
    ):
        command = subparsers.add_parser(name, help="bounded dev-only Messages dispatch/recovery")
        command.add_argument("generation_run_id", type=UUID)
        command.add_argument("--database-url")
        command.add_argument("--store-root", type=Path, required=True)
        if name == "agent-messages-run-once":
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--worker-id", required=True)
            command.add_argument("--lease-seconds", type=int)
        elif name == "agent-messages-recover":
            command.add_argument("--attempt-id", type=UUID, required=True)
        else:
            command.add_argument("--knowledge-root", type=Path, required=True)
            command.add_argument("--task-id", type=UUID, required=True)
            command.add_argument("--target-id", required=True)

    review = subparsers.add_parser(
        "agent-proposal-review",
        help="reread one retained Proposal and publish a deployment-owned review",
    )
    review.add_argument("generation_run_id", type=UUID)
    review.add_argument("proposal_id", type=UUID)
    review.add_argument("--database-url")
    review.add_argument("--store-root", type=Path, required=True)
    review.add_argument("--decision", choices=("approved", "rejected"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason-file", type=Path, required=True)
    review.add_argument("--evidence-file", type=Path, required=True)
    review.add_argument("--idempotency-key", required=True)
    review.add_argument(
        "--reviewed-at",
        required=True,
        help="fixed timezone-aware ISO-8601 timestamp used for idempotent replay",
    )

    close_rejected = subparsers.add_parser(
        "agent-generation-close-rejected",
        help="terminally close a Run after every retained Proposal was rejected",
    )
    close_rejected.add_argument("generation_run_id", type=UUID)
    close_rejected.add_argument("--database-url")
    close_rejected.add_argument("--store-root", type=Path, required=True)
    close_rejected.add_argument("--review-id", type=UUID, action="append", required=True)


def run_agent_generation_command(
    args: argparse.Namespace,
    *,
    repository_factory: Callable[[str], Any] = PostgresRepository,
    evidence_service_factory: Callable[[Any, Path], Any] | None = None,
) -> int | None:
    if args.command not in AGENT_GENERATION_COMMANDS:
        return None
    database_url = args.database_url or os.getenv(
        "HCUOPT_DATABASE_URL",
        "postgresql://hcuopt:hcuopt@localhost:5432/hcuopt",
    )
    coordinator = ApexGenerationCoordinator()
    try:
        repository = repository_factory(database_url)
        if args.command in {"agent-proposal-review", "agent-generation-close-rejected"}:
            from hcuopt.adapters.agent_promotion import (
                ProposalDecisionStore,
                ProposalReviewAuthority,
            )
            from hcuopt.agent.messages_worker import MessagesGenerationWorker
            from hcuopt.measurement.evidence import canonical_json_bytes

            worker = MessagesGenerationWorker(args.store_root)
            decisions = ProposalDecisionStore(args.store_root / "decisions")
            authority = ProposalReviewAuthority(
                patch_store=worker.patches,
                batch_store=worker.batches,
                decision_store=decisions,
            )
            status = repository.generation_run_status(args.generation_run_id)
            if args.command == "agent-proposal-review":
                reason = args.reason_file.read_text(encoding="utf-8").strip()
                evidence = args.evidence_file.read_bytes()
                reviewed_at = datetime.fromisoformat(args.reviewed_at.replace("Z", "+00:00"))
                result = authority.review(
                    status,
                    args.proposal_id,
                    decision=args.decision,
                    reviewer=args.reviewer,
                    reason=reason,
                    review_evidence=evidence,
                    idempotency_key=args.idempotency_key,
                    reviewed_at=reviewed_at,
                )
            else:
                retained = {
                    item.proposal_id
                    for item in status.proposals
                    if item.disposition == "retained"
                }
                reviews = tuple(decisions.load_review(review_id) for review_id in args.review_id)
                if len(reviews) != len(args.review_id) or {
                    item.proposal_id for item in reviews
                } != retained:
                    raise ValueError("rejected closure requires one review per retained Proposal")
                for review in reviews:
                    if review.decision != "rejected":
                        raise ValueError("rejected closure cannot contain an approved review")
                    resolved = authority.resolve(status, review.proposal_id)
                    verify_candidate_proposal_review_record(
                        status.run.request,
                        resolved.proposal,
                        resolved.raw_patch,
                        review,
                    )
                aggregate = decisions.publish_evidence(
                    "generation-review",
                    canonical_json_bytes(
                        {
                            "schema_version": "m2b-rejected-generation-review-v1",
                            "generation_run_id": str(args.generation_run_id),
                            "review_ids": [str(item.review_id) for item in reviews],
                            "decisions": [item.decision for item in reviews],
                            "promotion_ids": [],
                            "candidate_created": False,
                            "hcu_accessed": False,
                            "performance_conclusion": "not_measured",
                            "automatic_release_allowed": False,
                        }
                    ),
                )
                result = repository.complete_generation_review(
                    args.generation_run_id,
                    review_evidence_uri=aggregate.uri,
                    review_evidence_hash=aggregate.content_hash,
                )
        elif args.command.startswith("agent-messages-"):
            from hcuopt.adapters.agent_runner import AgentInputFile
            from hcuopt.agent.messages_dispatch import MessagesDispatchService
            from hcuopt.agent.messages_worker import MessagesGenerationWorker
            from hcuopt.generators.anthropic_messages import (
                CREDENTIAL_ENVIRONMENT_NAME,
                INPUT_NAME,
                MAX_INPUT_BYTES,
            )

            service = MessagesDispatchService(repository, MessagesGenerationWorker(args.store_root))
            if args.command == "agent-messages-prepare-inspection":
                from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
                from hcuopt.evaluation.agent_generation_inspection import (
                    AgentGenerationInspectionService,
                )

                result = AgentGenerationInspectionService(repository, args.store_root).prepare(
                    args.generation_run_id,
                    knowledge_store=KnowledgeSnapshotStore(
                        args.knowledge_root, profile="messages-inspection-reader-v1"
                    ),
                    task_id=args.task_id,
                    target_id=args.target_id,
                )
            elif args.command == "agent-messages-recover":
                result = service.recover(args.generation_run_id, args.attempt_id)
            else:
                with args.input.open("rb") as stream:
                    payload = stream.read(MAX_INPUT_BYTES + 1)
                credential_path = os.getenv(CREDENTIAL_ENVIRONMENT_NAME)
                if not credential_path:
                    raise ValueError("deployment credential file is required")
                with Path(credential_path).open("rb") as stream:
                    deployment_credential = stream.read(64 * 1024 + 1)
                result = service.run_once(
                    args.generation_run_id,
                    AgentInputFile(path=INPUT_NAME, content=payload),
                    worker_id=args.worker_id,
                    deployment_credential=deployment_credential,
                    lease_seconds=args.lease_seconds,
                )
        elif args.command == "agent-generation-start":
            request = GenerationRunStartRequest.model_validate_json(
                args.request.read_text(encoding="utf-8")
            )
            result = coordinator.start(request, repository)
        elif args.command == "agent-generation-status":
            result = coordinator.status(args.generation_run_id, repository)
        elif args.command == "agent-generation-reconcile":
            result = coordinator.reconcile(args.generation_run_id, repository)
        else:
            if args.evidence_root is None:
                raise ValueError("--evidence-root or HCUOPT_AGENT_EVIDENCE_ROOT is required")
            service = (
                evidence_service_factory(repository, args.evidence_root)
                if evidence_service_factory is not None
                else AgentGenerationEvidenceReadService(
                    repository,
                    HashedEvidenceReader(args.evidence_root),
                )
            )
            result = service.get(args.generation_run_id)
    except (
        AgentAuthorityError,
        ContractError,
        OSError,
        ValidationError,
        ValueError,
        DatabaseError,
        KeyError,
        TypeError,
    ) as exc:
        # Provider/deployment input validation may carry source or credentials.
        redact = args.command.startswith("agent-messages-") or args.command in {
            "agent-proposal-review",
            "agent-generation-close-rejected",
        }
        detail = type(exc).__name__ if redact else str(exc)
        print(f"Agent Generation command failed: {detail}", file=sys.stderr)
        return 2
    print(result.model_dump_json(indent=2))
    return 0


__all__ = [
    "configure_agent_generation_parsers",
    "run_agent_generation_command",
]
