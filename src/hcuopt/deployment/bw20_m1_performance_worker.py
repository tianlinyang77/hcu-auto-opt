# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Run one exclusive-lease BW20 M1 ``manual_performance`` Job."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.contracts.platform_v1 import SourceSnapshot, TargetSpec
from hcuopt.deployment.bw20_local_runner import BW20LocalCommandRunner
from hcuopt.deployment.bw20_m1_correctness_worker import BW20M1IdleGuard
from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY
from hcuopt.deployment.bw20_m1_profile import compose_bw20_m1_measurement
from hcuopt.deployment.bw20_stage0_staging import freeze_controller
from hcuopt.deployment.nmz36_m1_allocator import ALLOCATOR_RELATIVE_PATH
from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target
from hcuopt.workers.sdk import Worker


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _regular_root(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ExecutionSafetyError(f"{label} must be an existing regular directory")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise ExecutionSafetyError(f"{label} must not be redirected")
    return resolved


def baseline_module_hash(snapshot_file: Path) -> str:
    snapshot = SourceSnapshot.model_validate_json(
        snapshot_file.resolve(strict=True).read_bytes()
    )
    root = file_uri_to_path(snapshot.worktree_uri)
    if snapshot.kind != "baseline" or not snapshot.clean or root.is_symlink():
        raise ExecutionSafetyError("BW20 M1 performance Baseline is not immutable")
    root = root.resolve(strict=True)
    GitSourceManager(BW20_MANUAL_CANDIDATE_PROFILE)._assert_snapshot_unchanged(  # noqa: SLF001
        snapshot,
        root,
    )
    source = root / ALLOCATOR_RELATIVE_PATH
    if source.is_symlink() or not source.is_file():
        raise ExecutionSafetyError("BW20 M1 Baseline allocator source is unavailable")
    return _sha256(source.resolve(strict=True))


def build_worker(
    *,
    worker_id: str,
    api_url: str,
    target: TargetSpec,
    source_root: Path,
    baseline_snapshot_file: Path,
    trusted_evidence_root: Path,
    output_dir: Path,
) -> Worker:
    """Compose B's unique Harness with BW20 runtime and cleanup authorities."""

    BW20_M1_POLICY.validate_target(target)
    source = _regular_root(source_root, "BW20 M1 controller source")
    trusted = _regular_root(trusted_evidence_root, "BW20 M1 trusted evidence root")
    output = output_dir.absolute()
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise ExecutionSafetyError("BW20 M1 performance output must be a regular directory")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve(strict=True)
    try:
        source.relative_to(trusted)
        output.relative_to(trusted)
    except ValueError as exc:
        raise ExecutionSafetyError(
            "BW20 M1 performance source and output must stay in the trusted root"
        ) from exc

    runner = BW20LocalCommandRunner()
    guard = BW20M1IdleGuard(runner)
    guard(BW20_M1_POLICY.resource_id)
    initial = guard.observations[-1]["snapshot"]["device"]
    controller_archive = output / "controller.tar"
    if controller_archive.exists() or controller_archive.is_symlink():
        raise ExecutionSafetyError("BW20 M1 performance requires a fresh controller archive")
    bundle = freeze_controller(source, controller_archive)
    composition = compose_bw20_m1_measurement(
        target=target,
        runner=runner,
        controller_bundle=bundle,
        controller_manifest_sha256=bundle.manifest_sha256,
        trusted_evidence_root=trusted,
        baseline_module_hash=baseline_module_hash(baseline_snapshot_file),
        initial_clock_state={
            "mode": initial["performance_level"],
            "sclk_mhz": initial["sclk_mhz"],
            "mclk_mhz": initial["mclk_mhz"],
        },
    )
    return Worker(
        worker_id,
        WorkerType.GPU,
        api_url,
        capabilities={
            "adapter_profile": BW20_MANUAL_CANDIDATE_PROFILE,
            "adapters": ["measurement_harness"],
            "resource_id": BW20_M1_POLICY.resource_id,
            "controller_archive_hash": bundle.archive_sha256,
            "controller_manifest_hash": bundle.manifest_sha256,
            "clock_mutation_performed": False,
            "automatic_release_allowed": False,
        },
        heartbeat_seconds=10,
        adapters=composition.registry,
        output_dir=output,
        resource_guard=guard,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--target-lock", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--baseline-snapshot", type=Path, required=True)
    parser.add_argument("--trusted-evidence-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    worker = build_worker(
        worker_id=args.worker_id,
        api_url=args.api_url,
        target=load_target(args.target_lock),
        source_root=args.source_root,
        baseline_snapshot_file=args.baseline_snapshot,
        trusted_evidence_root=args.trusted_evidence_root,
        output_dir=args.output_dir,
    )
    worked = worker.run_once()
    print(
        json.dumps(
            {
                "worked": worked,
                "profile": BW20_MANUAL_CANDIDATE_PROFILE,
                "resource_id": BW20_M1_POLICY.resource_id,
                "clock_mutation_performed": False,
                "automatic_release_allowed": False,
            },
            sort_keys=True,
        )
    )
    return 0 if worked else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["baseline_module_hash", "build_worker", "main"]
