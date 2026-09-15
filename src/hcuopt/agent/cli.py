# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import Error as DatabaseError
from pydantic import ValidationError

from hcuopt.agent.authority import AgentAuthorityError, ApexGenerationCoordinator
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
        if args.command.startswith("agent-messages-"):
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
        detail = type(exc).__name__ if args.command.startswith("agent-messages-") else str(exc)
        print(f"Agent Generation command failed: {detail}", file=sys.stderr)
        return 2
    print(result.model_dump_json(indent=2))
    return 0


__all__ = [
    "configure_agent_generation_parsers",
    "run_agent_generation_command",
]
