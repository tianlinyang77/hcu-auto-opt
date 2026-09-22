# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Explicit CPU-only build command consuming an existing private claim binding."""

import argparse
import json
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hcuopt.deployment.formal_build_driver import FormalBuildDriver
from hcuopt.deployment.formal_service import _directory, load_formal_service_runtime
from hcuopt.measurement.m2_formal_receipt import _read_regular


class FormalBuildInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["formal-build-invocation-v1"]
    intent_id: UUID
    worker_id: str = Field(min_length=1, max_length=128)
    claim_token: UUID = Field(repr=False)
    wall_seconds_per_candidate: float = Field(gt=0, allow_inf_nan=False, strict=True)
    artifact_root: str
    cache_root: str
    output_root: str


def execute_build_command(
    *, deployment_root: Path, configuration_path: Path, invocation_path: Path,
    database_url: str, expected_source_commit: str, clock=None,
):
    invocation = FormalBuildInvocation.model_validate_json(
        _read_regular(deployment_root, invocation_path, 16 * 1024),
    )
    # Private claim files must be provisioned with owner-only access. Windows ACLs
    # are deployment-owned; POSIX mode bits can additionally be checked here.
    if os.name != "nt" and invocation_path.stat().st_mode & 0o077:
        raise ValueError("Formal build invocation requires owner-only permissions")
    paths = {name: _directory(deployment_root, getattr(invocation, name))
             for name in ("artifact_root", "cache_root", "output_root")}
    runtime, _, _ = load_formal_service_runtime(
        deployment_root=deployment_root, configuration_path=configuration_path,
        database_url=database_url, expected_source_commit=expected_source_commit,
        clock=clock, enabled=True,
    )
    results = FormalBuildDriver(runtime, **paths).build_family(
        intent_id=invocation.intent_id, worker_id=invocation.worker_id,
        claim_token=invocation.claim_token,
        wall_seconds_per_candidate=invocation.wall_seconds_per_candidate,
    )
    return {
        "intent_id": str(invocation.intent_id), "state": "build_family_published",
        "candidates": [{"candidate_id": str(result.build.candidate_id),
                        "artifact_hash": result.build.artifact.content_hash} for result in results],
        "hcu_accessed": False, "automatic_release_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-root", required=True, type=Path)
    parser.add_argument("--configuration", required=True, type=Path)
    parser.add_argument("--invocation", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        dsn = os.environ.get("HCUOPT_DATABASE_URL")
        if not dsn:
            raise ValueError("database configuration missing")
        report = execute_build_command(
            deployment_root=args.deployment_root,
            configuration_path=args.deployment_root / args.configuration,
            invocation_path=args.deployment_root / args.invocation,
            database_url=dsn, expected_source_commit=args.source_commit,
        )
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"error": "formal_build_stopped", "automatic_retry_allowed": False}))
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
