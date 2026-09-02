# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from uuid import UUID

from hcuopt.agent.cli import run_agent_generation_command
from hcuopt.cli import build_parser


class _JsonResult:
    def model_dump_json(self, *, indent: int) -> str:
        assert indent == 2
        return '{"automatic_release_allowed":false}'


class _StatusRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.reconciled: UUID | None = None

    def generation_run_status(self, generation_run_id: UUID) -> _JsonResult:
        assert generation_run_id == UUID("61000000-0000-0000-0000-000000000001")
        return _JsonResult()

    def reconcile_generation_run(self, generation_run_id: UUID, *, now):  # type: ignore[no-untyped-def]
        self.reconciled = generation_run_id
        return _JsonResult()


def test_agent_generation_cli_exposes_start_status_and_reconcile() -> None:
    parser = build_parser()

    assert parser.parse_args(["agent-generation-start", "request.json"]).command == (
        "agent-generation-start"
    )
    assert parser.parse_args(
        ["agent-generation-status", "61000000-0000-0000-0000-000000000001"]
    ).command == "agent-generation-status"
    assert parser.parse_args(
        ["agent-generation-reconcile", "61000000-0000-0000-0000-000000000001"]
    ).command == "agent-generation-reconcile"


def test_agent_generation_status_cli_is_read_only_and_reports_safety(
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    args = build_parser().parse_args(
        [
            "agent-generation-status",
            "61000000-0000-0000-0000-000000000001",
            "--database-url",
            "postgresql://fixture",
        ]
    )
    repositories: list[_StatusRepository] = []

    def factory(database_url: str) -> _StatusRepository:
        repository = _StatusRepository(database_url)
        repositories.append(repository)
        return repository

    assert run_agent_generation_command(args, repository_factory=factory) == 0
    assert repositories[0].database_url == "postgresql://fixture"
    assert capsys.readouterr().out.strip() == '{"automatic_release_allowed":false}'
