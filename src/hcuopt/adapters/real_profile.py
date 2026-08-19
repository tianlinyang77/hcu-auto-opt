from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hcuopt.adapters.execution import CommandRunner, ContainerExecutionAdapter
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.noop_builder import NoopBuilder
from hcuopt.adapters.profiles import (
    REAL_FRAMEWORK_SMOKE_PROFILE,
    REAL_STAGE0_MEASUREMENT_PROFILE,
    REAL_STAGE0_PROFILE,
)
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.adapters.sglang_evaluator import SGLangSmokeEvaluator
from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.harness import EvidenceMeasurementHarness, TelemetryCollector
from hcuopt.measurement.sampling import SampledWorkload
from hcuopt.measurement.stage0 import Stage0MeasurementProbeAdapter
from hcuopt.measurement.timers import DeviceTimer, HostClock
from hcuopt.runtime_probes import (
    OverlayCapabilityProbe,
    ProfilerCapabilityProbe,
    RuntimeProbeAdapter,
)


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


def build_nmz36_stage0_measurement_registry(
    target: TargetSpec,
    output_dir: Path,
    *,
    workload_factory: Callable[[int], SampledWorkload],
    telemetry: TelemetryCollector,
    device_timer: DeviceTimer,
    measurement_plan_factory: Callable[[Stage0ProbeType, Mapping[str, Any]], Mapping[str, Any]],
    known_signal_detector: Callable[[Any, Mapping[str, Any]], bool],
    null_signal_detector: Callable[[Any, Mapping[str, Any]], bool],
    synchronize: Callable[[], None] | None = None,
    clock: HostClock | None = None,
    runner: CommandRunner | None = None,
) -> AdapterRegistry:
    """Compose the real S0-B adapters without guessing a driver-specific timer API.

    Deployment supplies the HCU workload and device timer after the target's exclusive
    reservation is recorded. This constructor deliberately has no fallback timer.
    """

    profile = REAL_STAGE0_MEASUREMENT_PROFILE
    cleaner = ContainerResourceCleaner(target, runner, profile=profile)
    harness = EvidenceMeasurementHarness(
        provenance=cleaner.provenance.model_copy(
            update={
                "capability": "measurement_harness",
                "adapter_name": "EvidenceMeasurementHarness",
            }
        ),
        stable_identity=target.model_dump(mode="json"),
        workload_factory=workload_factory,
        telemetry=telemetry,
        device_timer=device_timer,
        synchronize=synchronize,
        clock=clock,
        cleaner=cleaner,
    )
    return AdapterRegistry(
        profile=profile,
        measurement_harness=harness,
        stage0_probe=Stage0MeasurementProbeAdapter(
            harness,
            target,
            measurement_plan_factory=measurement_plan_factory,
            known_signal_detector=known_signal_detector,
            null_signal_detector=null_signal_detector,
        ),
        resource_cleaner=cleaner,
    )


def build_nmz36_runtime_probe_registry(
    target: TargetSpec,
    output_dir: Path,
    *,
    runner: CommandRunner | None = None,
) -> AdapterRegistry:
    """Compose the real S0-C probes under the shared Stage 0 profile."""

    del output_dir
    profile = REAL_STAGE0_PROFILE
    executor = ContainerExecutionAdapter(runner, profile=profile)
    cleaner = ContainerResourceCleaner(target, runner, profile=profile)
    runtime_probe = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(executor=executor),
        OverlayCapabilityProbe(executor, cleaner),
        profile=profile,
    )
    return AdapterRegistry(
        profile=profile,
        executor=executor,
        resource_cleaner=cleaner,
        stage0_probe=runtime_probe,
    )
