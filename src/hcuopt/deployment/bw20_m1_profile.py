# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in BW20 M1 measurement composition; never mutates the public catalog."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.adapters.real_profile import build_m1_measurement_registry
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.deployment.bw20_m1_factory import (
    BW20M1Cleaner,
    BW20M1DeviceTimerFactory,
    BW20M1WorkloadFactory,
)
from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY
from hcuopt.deployment.bw20_stage0_staging import ControllerBundle
from hcuopt.deployment.bw20_stage0_telemetry import (
    BW20TelemetryCollector,
    require_endpoint,
)
from hcuopt.evaluation.stage0_verifier import Stage0EvidenceReader
from hcuopt.measurement.nmz36_runtime import DockerProcessLifecycleRecorder


@dataclass(frozen=True, slots=True)
class BW20M1MeasurementComposition:
    registry: AdapterRegistry
    workload_factory: BW20M1WorkloadFactory
    device_timer_factory: BW20M1DeviceTimerFactory
    telemetry: BW20TelemetryCollector
    cleaner: BW20M1Cleaner


def compose_bw20_m1_measurement(
    *,
    target: TargetSpec,
    runner,
    controller_bundle: ControllerBundle,
    controller_manifest_sha256: str,
    trusted_evidence_root: Path,
    baseline_module_hash: str,
    initial_clock_state: dict[str, object],
) -> BW20M1MeasurementComposition:
    """Compose B's existing Harness around BW20 target-specific process adapters."""

    BW20_M1_POLICY.validate_target(target)
    require_endpoint(runner)
    if (
        re.fullmatch(r"sha256:[0-9a-f]{64}", controller_manifest_sha256) is None
        or controller_bundle.manifest_sha256 != controller_manifest_sha256
    ):
        raise ValueError("BW20 M1 controller bundle differs from its deployment pin")
    root = trusted_evidence_root.resolve(strict=True)
    provenance = AdapterProvenance(
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
        capability="measurement_harness",
        adapter_name="M1TrustedMeasurementHarness",
        adapter_version="1",
        implementation_kind="real",
    )
    workload = BW20M1WorkloadFactory(
        target=target,
        runner=runner,
        bundle=controller_bundle,
        local_evidence_root=root / "bw20-m1-worker-raw",
        harness_provenance=provenance,
        baseline_module_hash=baseline_module_hash,
    )
    telemetry = BW20TelemetryCollector(runner=runner, live_bindings=workload.live_bindings)
    cleaner = BW20M1Cleaner(
        factory=workload,
        telemetry=telemetry,
        initial_clock_state=initial_clock_state,
    )
    device_timer = BW20M1DeviceTimerFactory(
        target=target,
        runner=runner,
        bundle=controller_bundle,
        session_registry=workload,
    )
    registry = build_m1_measurement_registry(
        profile=BW20_MANUAL_CANDIDATE_PROFILE,
        evidence_root=root,
        evidence_reader=Stage0EvidenceReader(root),
        workload_factory=workload,
        telemetry=telemetry,
        lifecycle_recorder=DockerProcessLifecycleRecorder(),
        cleaner=cleaner,
        device_timer_factory=device_timer,
    )
    return BW20M1MeasurementComposition(
        registry=registry,
        workload_factory=workload,
        device_timer_factory=device_timer,
        telemetry=telemetry,
        cleaner=cleaner,
    )


__all__ = ["BW20M1MeasurementComposition", "compose_bw20_m1_measurement"]
