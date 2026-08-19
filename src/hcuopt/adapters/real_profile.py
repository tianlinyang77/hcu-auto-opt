from __future__ import annotations

from pathlib import Path

from hcuopt.adapters.execution import CommandRunner, ContainerExecutionAdapter
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.noop_builder import NoopBuilder
from hcuopt.adapters.profiles import REAL_FRAMEWORK_SMOKE_PROFILE
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.adapters.sglang_evaluator import SGLangSmokeEvaluator
from hcuopt.contracts.platform_v1 import TargetSpec


def build_nmz36_framework_smoke_registry(
    target: TargetSpec,
    output_dir: Path,
    *,
    runner: CommandRunner | None = None,
    workload_path: Path | None = None,
    smoke_runner_path: Path | None = None,
) -> AdapterRegistry:
    """Compose the six real F1 adapters under one auditable profile name."""

    profile = REAL_FRAMEWORK_SMOKE_PROFILE
    output_root = output_dir.resolve()
    return AdapterRegistry(
        profile=profile,
        source_manager=GitSourceManager(profile),
        builder=NoopBuilder(profile),
        artifact_store=LocalArtifactStore(output_root / "artifacts", profile),
        executor=ContainerExecutionAdapter(runner, profile=profile),
        evaluator=SGLangSmokeEvaluator(
            profile=profile,
            workload_path=workload_path,
            runner_host_path=smoke_runner_path,
        ),
        resource_cleaner=ContainerResourceCleaner(target, runner, profile=profile),
    )
