# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Run one HCU-free formal Endpoint Campaign D Job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hcuopt.adapters.endpoint_adjudication import LocalEndpointCampaignAdjudicator
from hcuopt.adapters.profiles import ENDPOINT_FORMAL_ADJUDICATION_PROFILE
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.workers.sdk import Worker


def _trusted_root(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ExecutionSafetyError(
            "Endpoint D evidence root must be an existing regular directory"
        )
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise ExecutionSafetyError("Endpoint D evidence root must not be redirected")
    return resolved


def build_worker(
    *,
    worker_id: str,
    api_url: str,
    allowed_evidence_roots: tuple[Path, ...],
) -> Worker:
    roots = tuple(_trusted_root(path) for path in allowed_evidence_roots)
    adapter = LocalEndpointCampaignAdjudicator(
        profile=ENDPOINT_FORMAL_ADJUDICATION_PROFILE,
        allowed_evidence_roots=roots,
    )
    registry = AdapterRegistry(
        profile=ENDPOINT_FORMAL_ADJUDICATION_PROFILE,
        endpoint_campaign_adjudicator=adapter,
    )
    return Worker(
        worker_id,
        WorkerType.EVALUATION,
        api_url,
        capabilities={
            "adapter_profile": ENDPOINT_FORMAL_ADJUDICATION_PROFILE,
            "adapters": ["endpoint_campaign_adjudicator"],
            "allowed_evidence_roots": [str(path) for path in roots],
            "hcu_required": False,
            "automatic_release_allowed": False,
        },
        heartbeat_seconds=10,
        adapters=registry,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--allowed-evidence-root",
        action="append",
        type=Path,
        required=True,
    )
    args = parser.parse_args(argv)
    worker = build_worker(
        worker_id=args.worker_id,
        api_url=args.api_url,
        allowed_evidence_roots=tuple(args.allowed_evidence_root),
    )
    worked = worker.run_once()
    print(
        json.dumps(
            {
                "worked": worked,
                "profile": ENDPOINT_FORMAL_ADJUDICATION_PROFILE,
                "hcu_required": False,
                "automatic_release_allowed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if worked else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_worker", "main"]
