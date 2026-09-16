# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Run one BW20 M1 ``manual_build`` through the original Worker lifecycle.

This entrypoint is deliberately CPU-only.  It packages a reviewed, immutable
Candidate source package into the normal M1 startup Overlay Artifact; it does
not execute Candidate code, acquire an HCU lease, or make a performance claim.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    ManualOverlayCandidateBuilder,
)
from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.deployment.nmz36_m1_allocator import (
    ALLOCATOR_MOUNT_TARGET,
    ALLOCATOR_REPLACEMENT_POINT,
)
from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.workers.handlers import JobHandlers
from hcuopt.workers.sdk import Worker


def _regular_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ExecutionSafetyError(f"{label} must be an existing regular directory")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise ExecutionSafetyError(f"{label} must not be redirected")
    return resolved


def build_bw20_m1_source_registry(
    *, source_package_root: Path, output_dir: Path
) -> AdapterRegistry:
    """Compose only the real C-line adapters needed by ``manual_build``."""

    packages = _regular_directory(source_package_root, "Candidate source package root")
    output = output_dir.absolute()
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise ExecutionSafetyError("M1 Build output must be a regular directory")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve(strict=True)
    if output == packages or output in packages.parents or packages in output.parents:
        raise ExecutionSafetyError(
            "M1 Build output and Candidate source package root must be separate"
        )

    source_manager = GitSourceManager(BW20_MANUAL_CANDIDATE_PROFILE)
    artifact_store = LocalArtifactStore(output / "artifacts", BW20_MANUAL_CANDIDATE_PROFILE)
    source_packages = CandidateSourcePackageStore(
        packages,
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={
            ALLOCATOR_REPLACEMENT_POINT: ALLOCATOR_MOUNT_TARGET,
        },
    )
    builder = ManualOverlayCandidateBuilder(
        source_manager,
        source_packages,
        artifact_store,
        LocalBuildCache(output / "build-cache"),
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
    )
    return AdapterRegistry(
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
        candidate_builder=builder,
        source_manager=source_manager,
        artifact_store=artifact_store,
    )


class BW20M1BuildJobHandler:
    """Fail closed if a same-type Worker is offered anything except M1 Build."""

    def __init__(self, registry: AdapterRegistry, output_dir: Path) -> None:
        if registry.profile != BW20_MANUAL_CANDIDATE_PROFILE:
            raise ValueError("BW20 M1 Build registry has the wrong profile")
        registry.require("candidate_builder")
        self.handlers = JobHandlers(registry, output_dir)

    def handle(self, job_type: str, payload: dict) -> dict:
        if job_type != "manual_build":
            raise ExecutionSafetyError("BW20 M1 Build Worker accepts only manual_build")
        return self.handlers.handle_manual_build(payload)

    def cleanup(self, job_type: str, payload: dict) -> dict:
        del payload
        if job_type != "manual_build":
            raise ExecutionSafetyError("BW20 M1 Build cleanup scope is invalid")
        # manual_build has no HCU lease. Candidate Worktree cleanup is owned by
        # ManualOverlayCandidateBuilder's finally block.
        return {}


def build_worker(
    *,
    worker_id: str,
    api_url: str,
    source_package_root: Path,
    output_dir: Path,
) -> Worker:
    registry = build_bw20_m1_source_registry(
        source_package_root=source_package_root,
        output_dir=output_dir,
    )
    return Worker(
        worker_id,
        WorkerType.BUILD,
        api_url,
        capabilities={
            "adapter_profile": BW20_MANUAL_CANDIDATE_PROFILE,
            "adapters": ["candidate_builder"],
            "hcu_accessed": False,
            "automatic_release_allowed": False,
        },
        handlers=BW20M1BuildJobHandler(registry, output_dir),
        output_dir=output_dir,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--source-package-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    worker = build_worker(
        worker_id=args.worker_id,
        api_url=args.api_url,
        source_package_root=args.source_package_root,
        output_dir=args.output_dir,
    )
    worked = worker.run_once()
    print(
        json.dumps(
            {
                "worked": worked,
                "profile": BW20_MANUAL_CANDIDATE_PROFILE,
                "hcu_accessed": False,
                "automatic_release_allowed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if worked else 3


if __name__ == "__main__":
    raise SystemExit(main())
