# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

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
    repository = repository_factory(database_url)
    coordinator = ApexGenerationCoordinator()
    try:
        if args.command == "agent-generation-start":
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
                raise ValueError(
                    "--evidence-root or HCUOPT_AGENT_EVIDENCE_ROOT is required"
                )
            service = (
                evidence_service_factory(repository, args.evidence_root)
                if evidence_service_factory is not None
                else AgentGenerationEvidenceReadService(
                    repository,
                    HashedEvidenceReader(args.evidence_root),
                )
            )
            result = service.get(args.generation_run_id)
    except (AgentAuthorityError, ContractError, OSError, ValidationError, ValueError) as exc:
        print(f"Agent Generation command failed: {exc}", file=sys.stderr)
        return 2
    print(result.model_dump_json(indent=2))
    return 0


__all__ = [
    "configure_agent_generation_parsers",
    "run_agent_generation_command",
]
