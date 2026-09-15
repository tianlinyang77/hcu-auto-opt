from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.adapters.execution import CommandRunner, ContainerExecutionAdapter
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.m1_verification import (
    M1CandidateAdjudicatorWorkerAdapter,
    M1CorrectnessEvidenceProducer,
    M1KernelCorrectnessWorkerAdapter,
)
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    ManualOverlayCandidateBuilder,
)
from hcuopt.adapters.noop_builder import NoopBuilder
from hcuopt.adapters.profiles import (
    REAL_FRAMEWORK_SMOKE_PROFILE,
    REAL_MANUAL_CANDIDATE_PROFILE,
    REAL_STAGE0_MEASUREMENT_PROFILE,
    REAL_STAGE0_PROFILE,
    real_manual_candidate_profile,
)
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.adapters.sglang_evaluator import SGLangSmokeEvaluator
from hcuopt.adapters.stage0_router import RoutedStage0ProbeAdapter
from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.evaluation.m1_protocol import LoadedM1Protocol
from hcuopt.evaluation.stage0_verifier import Stage0EvidenceReader
from hcuopt.measurement.harness import (
    CleanupController,
    EvidenceMeasurementHarness,
    FormalStage0Workload,
    ProcessLifecycleRecorder,
    TelemetryCollector,
)
from hcuopt.measurement.m1_harness import (
    M1DeviceTimerFactory,
    M1TrustedMeasurementHarness,
    M1WorkloadFactory,
)
from hcuopt.measurement.nmz36_runtime import (
    HySmiTelemetryCollector,
    ManagedProcessRegistry,
    Nmz36Stage0MeasurementProbeAdapter,
)
from hcuopt.measurement.sampling import SampledWorkload
from hcuopt.measurement.stage0 import Stage0MeasurementProbeAdapter
from hcuopt.measurement.timers import DeviceTimer, HostClock
from hcuopt.runtime_probes import (
    DeploymentContentAddressedEvidencePublisher,
    EvidencePublisher,
    ManualCandidateOverlayRuntime,
    ManualOverlayRuntimeProfile,
    OverlayCapabilityProbe,
    ProfilerCapabilityProbe,
    RuntimeProbeAdapter,
    RuntimeProbeProfile,
)


def build_m1_source_artifact_registry(
    target: TargetSpec,
    output_dir: Path,
    *,
    trusted_source_root: Path,
    runtime_configuration: ManualOverlayRuntimeProfile,
    evidence_root: Path,
    allowed_overlay_roots: tuple[str, ...],
    runner: CommandRunner | None = None,
) -> AdapterRegistry:
    """Compose M1-C build and runtime boundaries from deployment-owned inputs."""

    profile = runtime_configuration.profile
    output_root = output_dir.resolve()
    source_manager = GitSourceManager(profile)
    artifact_store = LocalArtifactStore(output_root / "artifacts", profile)
    source_packages = CandidateSourcePackageStore(
        trusted_source_root,
        profile=profile,
        allowed_overlay_roots=allowed_overlay_roots,
        approved_mount_targets=runtime_configuration.replacement_points,
    )
    candidate_builder = ManualOverlayCandidateBuilder(
        source_manager,
        source_packages,
        artifact_store,
        LocalBuildCache(output_root / "build-cache"),
        profile=profile,
    )
    executor = ContainerExecutionAdapter(runner, profile=profile)
    cleaner = ContainerResourceCleaner(target, runner, profile=profile)
    candidate_runtime = ManualCandidateOverlayRuntime(
        source_manager,
        OverlayCapabilityProbe(executor, cleaner),
        cleaner,
        target,
        runtime_configuration,
        evidence_root=evidence_root,
    )
    return AdapterRegistry(
        profile=profile,
        candidate_builder=candidate_builder,
        candidate_runtime=candidate_runtime,
        executor=executor,
        resource_cleaner=cleaner,
        source_manager=source_manager,
        artifact_store=artifact_store,
    )


def build_m1_correctness_registry(
    *,
    profile: str,
    protocol: LoadedM1Protocol,
    reader: HashedEvidenceReader,
    producer: M1CorrectnessEvidenceProducer,
    evidence_root: Path,
) -> AdapterRegistry:
    """Build the D-owned shared-lease correctness Worker boundary."""

    return AdapterRegistry(
        profile=profile,
        kernel_correctness=M1KernelCorrectnessWorkerAdapter(
            profile=profile,
            protocol=protocol,
            reader=reader,
            producer=producer,
            evidence_root=evidence_root,
        ),
    )


def build_m1_measurement_registry(
    *,
    profile: str,
    evidence_root: Path,
    workload_factory: M1WorkloadFactory,
    telemetry: TelemetryCollector,
    lifecycle_recorder: ProcessLifecycleRecorder,
    cleaner: CleanupController,
    device_timer: DeviceTimer | None = None,
    device_timer_factory: M1DeviceTimerFactory | None = None,
    evidence_reader: Stage0EvidenceReader | None = None,
    synchronize: Callable[[], None] | None = None,
    clock: HostClock | None = None,
) -> AdapterRegistry:
    """Build B's unique raw-evidence producer for one real M1 process factory."""

    provenance = AdapterProvenance(
        profile=profile,
        capability="measurement_harness",
        adapter_name="M1TrustedMeasurementHarness",
        adapter_version="1",
        implementation_kind="real",
    )
    cleaner_provenance = getattr(cleaner, "provenance", None)
    if cleaner_provenance is not None and cleaner_provenance.profile != profile:
        raise ValueError("M1 Measurement Harness and Resource Cleaner use another profile")
    harness = M1TrustedMeasurementHarness(
        provenance=provenance,
        evidence_root=evidence_root,
        evidence_reader=evidence_reader,
        workload_factory=workload_factory,
        telemetry=telemetry,
        lifecycle_recorder=lifecycle_recorder,
        cleaner=cleaner,
        device_timer=device_timer,
        device_timer_factory=device_timer_factory,
        synchronize=synchronize,
        clock=clock,
    )
    return AdapterRegistry(
        profile=profile,
        measurement_harness=harness,
        resource_cleaner=cleaner,
    )


def build_m1_adjudication_registry(
    *,
    profile: str,
    protocol: LoadedM1Protocol,
    reader: HashedEvidenceReader,
    evidence_root: Path,
) -> AdapterRegistry:
    """Build the HCU-free D adjudication Worker boundary."""

    return AdapterRegistry(
        profile=profile,
        candidate_adjudicator=M1CandidateAdjudicatorWorkerAdapter(
            profile=profile,
            protocol=protocol,
            reader=reader,
            evidence_root=evidence_root,
        ),
    )


def compose_nmz36_m1_registry(
    source_registry: AdapterRegistry,
    correctness_registry: AdapterRegistry,
    measurement_registry: AdapterRegistry,
    adjudication_registry: AdapterRegistry,
) -> AdapterRegistry:
    """Compose the five formal M1 boundaries under one opt-in profile.

    Each worker may still run only its own partial registry. This composition
    gate proves that the deployment has one compatible real implementation for
    every capability before the API catalog is allowed to expose M1 task
    creation.
    """

    registries = (
        source_registry,
        correctness_registry,
        measurement_registry,
        adjudication_registry,
    )
    if any(item.profile != REAL_MANUAL_CANDIDATE_PROFILE for item in registries):
        raise ValueError(f"all M1 registries must use profile {REAL_MANUAL_CANDIDATE_PROFILE}")
    components = {
        "candidate_builder": source_registry.require("candidate_builder"),
        "kernel_correctness": correctness_registry.require("kernel_correctness"),
        "measurement_harness": measurement_registry.require("measurement_harness"),
        "candidate_adjudicator": adjudication_registry.require("candidate_adjudicator"),
        "resource_cleaner": measurement_registry.require("resource_cleaner"),
    }
    for capability, adapter in components.items():
        provenance = adapter.provenance
        if provenance.implementation_kind != "real":
            raise ValueError(f"M1 capability {capability} must use a real Adapter")
        if provenance.capability != capability:
            raise ValueError(
                f"M1 boundary {capability} has provenance capability {provenance.capability}"
            )

    registry = AdapterRegistry(
        profile=REAL_MANUAL_CANDIDATE_PROFILE,
        candidate_builder=components["candidate_builder"],
        candidate_runtime=source_registry.candidate_runtime,
        kernel_correctness=components["kernel_correctness"],
        measurement_harness=components["measurement_harness"],
        candidate_adjudicator=components["candidate_adjudicator"],
        resource_cleaner=components["resource_cleaner"],
        executor=source_registry.executor,
        source_manager=source_registry.source_manager,
        artifact_store=source_registry.artifact_store,
    )
    profile = real_manual_candidate_profile()
    profile.require_manual_candidate()
    missing = sorted(profile.capabilities - set(registry.available()))
    if missing:
        raise ValueError(f"composed M1 registry is incomplete: {', '.join(missing)}")
    return registry


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
    formal_workload_factory: (Callable[[Stage0ProbeType, int], FormalStage0Workload] | None) = None,
    lifecycle_recorder: ProcessLifecycleRecorder | None = None,
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
        formal_workload_factory=formal_workload_factory,
        lifecycle_recorder=lifecycle_recorder,
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
    configuration: RuntimeProbeProfile,
    evidence_publisher: EvidencePublisher | None = None,
    telemetry: TelemetryCollector | None = None,
    clock: HostClock | None = None,
    runner: CommandRunner | None = None,
) -> AdapterRegistry:
    """Compose the real S0-C probes under the shared Stage 0 profile."""

    del output_dir
    profile = REAL_STAGE0_PROFILE
    if configuration.profile != profile:
        raise ValueError(
            f"runtime probe configuration profile must be {profile}, got {configuration.profile}"
        )
    executor = ContainerExecutionAdapter(runner, profile=profile)
    cleaner = ContainerResourceCleaner(target, runner, profile=profile)
    runtime_probe = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(executor=executor),
        OverlayCapabilityProbe(executor, cleaner),
        cleaner,
        target,
        configuration,
        evidence_publisher,
        telemetry,
        clock,
    )
    return AdapterRegistry(
        profile=profile,
        executor=executor,
        resource_cleaner=cleaner,
        stage0_probe=runtime_probe,
    )


def compose_nmz36_stage0_registry(
    measurement_registry: AdapterRegistry,
    runtime_registry: AdapterRegistry,
) -> AdapterRegistry:
    """Route all seven Stage 0 probes through one public worker profile.

    S0-B and S0-C remain independently implemented and testable.  The composed
    registry is the deployment boundary consumed by the control plane.
    """

    measurement_probe = measurement_registry.require("stage0_probe")
    runtime_probe = runtime_registry.require("stage0_probe")
    resource_cleaner = runtime_registry.require("resource_cleaner")
    routes = {
        Stage0ProbeType.FINGERPRINT: measurement_probe,
        Stage0ProbeType.TIMER: measurement_probe,
        Stage0ProbeType.NOISE: measurement_probe,
        Stage0ProbeType.KNOWN_SIGNAL: measurement_probe,
        Stage0ProbeType.NULL_SIGNAL: measurement_probe,
        Stage0ProbeType.PROFILER: runtime_probe,
        Stage0ProbeType.HOTPATCH: runtime_probe,
    }
    return AdapterRegistry(
        profile=REAL_STAGE0_PROFILE,
        stage0_probe=RoutedStage0ProbeAdapter(routes, profile=REAL_STAGE0_PROFILE),
        executor=runtime_registry.executor,
        resource_cleaner=resource_cleaner,
    )


def build_nmz36_stage0_registry(
    target: TargetSpec,
    output_dir: Path,
    *,
    source_root: Path,
    runtime_configuration: RuntimeProbeProfile,
    evidence_root: Path,
    runner: CommandRunner | None = None,
) -> AdapterRegistry:
    """Build the deployment-ready seven-probe Worker registry.

    The measurement side derives its concrete fencing identity from every
    claimed Job.  Both producers publish below the same deployment-owned root,
    which is also the only root the independent finalizer is allowed to read.
    """

    output_root = output_dir.resolve()
    trusted_root = evidence_root.resolve()
    if output_root != trusted_root and trusted_root not in output_root.parents:
        raise ValueError("Stage 0 Worker output must be inside the trusted evidence root")
    registry = ManagedProcessRegistry()
    measurement = AdapterRegistry(
        profile=REAL_STAGE0_PROFILE,
        stage0_probe=Nmz36Stage0MeasurementProbeAdapter(
            target,
            source_root,
            registry,
        ),
        resource_cleaner=ContainerResourceCleaner(
            target,
            runner,
            profile=REAL_STAGE0_PROFILE,
        ),
    )
    runtime = build_nmz36_runtime_probe_registry(
        target,
        output_root,
        configuration=runtime_configuration,
        evidence_publisher=DeploymentContentAddressedEvidencePublisher(trusted_root),
        telemetry=HySmiTelemetryCollector(target, registry),
        runner=runner,
    )
    return compose_nmz36_stage0_registry(measurement, runtime)
