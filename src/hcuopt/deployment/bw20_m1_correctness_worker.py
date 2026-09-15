# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Run one lease-guarded BW20 M1 ``manual_correctness`` Job."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hcuopt.adapters.execution import LocalCommandRunner
from hcuopt.adapters.m1_verification import M1CorrectnessEvidenceProducer
from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.adapters.real_profile import build_m1_correctness_registry
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY
from hcuopt.deployment.bw20_stage0_telemetry import HOST_TELEMETRY, parse_snapshot
from hcuopt.deployment.nmz36_m1_allocator import (
    Nmz36M1AllocatorCorrectnessEvidenceProducer,
)
from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.evaluation.m1_protocol import load_registered_m1_protocol
from hcuopt.targets import load_target
from hcuopt.workers.handlers import JobHandlers
from hcuopt.workers.sdk import Worker


class BW20M1AllocatorCorrectnessEvidenceProducer(Nmz36M1AllocatorCorrectnessEvidenceProducer):
    """Reuse the generic allocator oracle under the explicit BW20 policy."""


class BW20M1IdleGuard:
    """Read-only pre/post guard for the accepted HCU 7 validation window."""

    def __init__(self, runner: LocalCommandRunner | None = None) -> None:
        self.runner = runner or LocalCommandRunner()
        self.observations: list[dict[str, Any]] = []

    def observe(self) -> tuple[dict[str, Any], dict[str, Any]]:
        completed = self.runner.run(("python3", "-c", HOST_TELEMETRY), timeout=40)
        if completed.returncode != 0:
            raise ExecutionSafetyError("BW20 read-only host telemetry failed")
        try:
            raw = json.loads(completed.stdout)
            snapshot = parse_snapshot(raw, {})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExecutionSafetyError("BW20 host telemetry is invalid") from exc
        record = {"raw": raw, "snapshot": snapshot.model_dump(mode="json")}
        self.observations.append(record)
        return raw, record["snapshot"]

    def __call__(self, resource_id: str | None) -> None:
        if resource_id != BW20_M1_POLICY.resource_id:
            raise ExecutionSafetyError("BW20 M1 guard received a different resource")
        raw, snapshot = self.observe()
        device = snapshot["device"]
        files = raw["files"]
        if (
            snapshot["background_processes"]
            or device["performance_level"] != "auto"
            or int(files["gpu_busy_percent"]) != 0
            or not 0 <= int(files["mem_info_vram_used"]) <= 4 * 1024 * 1024
        ):
            raise ExecutionSafetyError("BW20 HCU 7 is not idle in the accepted auto window")
        return None


class BW20M1CorrectnessCleaner:
    """Fence only Job-owned containers and require idle auto-mode readback."""

    def __init__(self, target: TargetSpec, guard: BW20M1IdleGuard) -> None:
        BW20_M1_POLICY.validate_target(target)
        self.containers = ContainerResourceCleaner(
            target,
            profile=BW20_MANUAL_CANDIDATE_PROFILE,
        )
        self.guard = guard
        self.provenance = AdapterProvenance(
            profile=BW20_MANUAL_CANDIDATE_PROFILE,
            capability="resource_cleaner",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )

    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, object]:
        result = dict(self.containers.fence(resource_id, fencing_token))
        result["scope"] = "job_owned_labeled_containers_only"
        result["clock_mutation_performed"] = False
        return result

    def health_check(self, resource_id: str) -> Mapping[str, object]:
        try:
            self.guard(resource_id)
            observation = self.guard.observations[-1]
            return {
                "resource_id": resource_id,
                "healthy": True,
                "quarantined": False,
                "reason": "observed_idle_auto_policy",
                "host_observation": observation,
                "clock_mutation_performed": False,
            }
        except Exception as exc:
            return {
                "resource_id": resource_id,
                "healthy": False,
                "quarantined": True,
                "reason": "host_state_not_restored",
                "error_type": type(exc).__name__,
                "clock_mutation_performed": False,
            }


class BW20M1OutputAccess:
    """Grant the fixed non-root container user access to exact Job directories."""

    def __init__(self, root: Path, runner: LocalCommandRunner | None = None) -> None:
        self.root = root.resolve(strict=True)
        self.runner = runner or LocalCommandRunner()

    def __call__(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ExecutionSafetyError("BW20 M1 output ACL escaped its Job root") from exc
        if resolved.is_symlink() or not resolved.is_dir():
            raise ExecutionSafetyError("BW20 M1 output ACL target is not a directory")
        applied = self.runner.run(("setfacl", "-m", "u:65534:rwx", "--", str(resolved)), timeout=10)
        if applied.returncode != 0:
            raise ExecutionSafetyError("BW20 M1 output ACL could not be applied")
        verified = self.runner.run(("getfacl", "-cpn", str(resolved)), timeout=10)
        if verified.returncode != 0 or b"user:65534:rwx" not in verified.stdout.splitlines():
            raise ExecutionSafetyError("BW20 M1 output ACL could not be verified")


def build_bw20_m1_correctness_registry(
    *,
    target: TargetSpec,
    source_root: Path,
    trusted_evidence_root: Path,
    output_dir: Path,
    guard: BW20M1IdleGuard,
) -> AdapterRegistry:
    BW20_M1_POLICY.validate_target(target)
    source = source_root.resolve(strict=True)
    trusted = trusted_evidence_root.resolve(strict=True)
    output = output_dir.resolve(strict=True)
    if source.is_symlink() or trusted.is_symlink() or output.is_symlink():
        raise ExecutionSafetyError("BW20 M1 correctness roots must not be redirected")
    try:
        output.relative_to(trusted)
        source.relative_to(trusted)
    except ValueError as exc:
        raise ExecutionSafetyError(
            "BW20 M1 correctness source and output must stay in the trusted root"
        ) from exc

    protocol = load_registered_m1_protocol()
    cleaner = BW20M1CorrectnessCleaner(target, guard)
    output_access = BW20M1OutputAccess(output)
    producer: M1CorrectnessEvidenceProducer = BW20M1AllocatorCorrectnessEvidenceProducer(
        target=target,
        source_root=source,
        protocol=protocol,
        cleaner=cleaner,
        deployment_policy=BW20_M1_POLICY,
        prepare_output_directory=output_access,
    )
    correctness = build_m1_correctness_registry(
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
        protocol=protocol,
        reader=HashedEvidenceReader(trusted),
        producer=producer,
        evidence_root=output,
    )
    return AdapterRegistry(
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
        kernel_correctness=correctness.require("kernel_correctness"),
        resource_cleaner=cleaner,
    )


class BW20M1CorrectnessJobHandler:
    def __init__(self, registry: AdapterRegistry, output_dir: Path) -> None:
        if registry.profile != BW20_MANUAL_CANDIDATE_PROFILE:
            raise ValueError("BW20 M1 correctness registry has the wrong profile")
        registry.require("kernel_correctness")
        self.handlers = JobHandlers(registry, output_dir)

    def handle(self, job_type: str, payload: dict) -> dict:
        if job_type != "manual_correctness":
            raise ExecutionSafetyError("BW20 M1 correctness Worker accepts only manual_correctness")
        return self.handlers.handle_manual_correctness(payload)

    def cleanup(self, job_type: str, payload: dict) -> dict:
        if job_type != "manual_correctness":
            raise ExecutionSafetyError("BW20 M1 correctness cleanup scope is invalid")
        return self.handlers.cleanup(job_type, payload)


def build_worker(
    *,
    worker_id: str,
    api_url: str,
    target: TargetSpec,
    source_root: Path,
    trusted_evidence_root: Path,
    output_dir: Path,
) -> Worker:
    guard = BW20M1IdleGuard()
    registry = build_bw20_m1_correctness_registry(
        target=target,
        source_root=source_root,
        trusted_evidence_root=trusted_evidence_root,
        output_dir=output_dir,
        guard=guard,
    )
    return Worker(
        worker_id,
        WorkerType.GPU,
        api_url,
        capabilities={
            "adapter_profile": BW20_MANUAL_CANDIDATE_PROFILE,
            "adapters": ["kernel_correctness"],
            "resource_id": BW20_M1_POLICY.resource_id,
            "automatic_release_allowed": False,
        },
        heartbeat_seconds=10,
        handlers=BW20M1CorrectnessJobHandler(registry, output_dir),
        output_dir=output_dir,
        resource_guard=guard,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--target-lock", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--trusted-evidence-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    worker = build_worker(
        worker_id=args.worker_id,
        api_url=args.api_url,
        target=load_target(args.target_lock),
        source_root=args.source_root,
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
