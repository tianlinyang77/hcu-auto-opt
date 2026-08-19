from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hcuopt.contracts.platform_v1 import AdapterProvenance, MeasurementSeries
from hcuopt.domain.enums import LeaseScope
from hcuopt.measurement.evidence import EvidenceArtifact, write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.models import DynamicObservation, MeasurementEvidence, MeasurementPlan
from hcuopt.measurement.sampling import SampledWorkload, collect_samples
from hcuopt.measurement.timers import (
    DeviceTimer,
    HostClock,
    HostMonotonicClock,
    calibrate_device_timer,
)


class MeasurementSafetyError(RuntimeError):
    pass


class TelemetryCollector(Protocol):
    def collect(self) -> Mapping[str, Any]: ...


class CleanupController(Protocol):
    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, Any]: ...

    def health_check(self, resource_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HarnessRun:
    series: MeasurementSeries
    evidence: MeasurementEvidence
    artifact: EvidenceArtifact
    cleanup_evidence: dict[str, Any] | None


class EvidenceMeasurementHarness:
    """One real measurement producer that keeps raw data before summaries."""

    def __init__(
        self,
        *,
        provenance: AdapterProvenance,
        stable_identity: Mapping[str, Any],
        workload_factory: Callable[[int], SampledWorkload],
        telemetry: TelemetryCollector,
        device_timer: DeviceTimer,
        synchronize: Callable[[], None] | None = None,
        clock: HostClock | None = None,
        cleaner: CleanupController | None = None,
    ) -> None:
        if provenance.implementation_kind != "real":
            raise ValueError("EvidenceMeasurementHarness requires real provenance")
        self.provenance = provenance
        self.stable_identity = dict(stable_identity)
        self.workload_factory = workload_factory
        self.telemetry = telemetry
        self.device_timer = device_timer
        self.synchronize = synchronize
        self.clock = clock or HostMonotonicClock()
        self.cleaner = cleaner

    def run(self, plan: Mapping[str, Any], output_dir: Path) -> MeasurementSeries:
        return self.run_with_evidence(plan, output_dir).series

    def cleanup_formal_context(self, context: Mapping[str, Any]) -> dict[str, Any]:
        self._require_formal_context(context)
        return self._cleanup(context)

    def run_with_evidence(self, plan: Mapping[str, Any], output_dir: Path) -> HarnessRun:
        measurement_plan = MeasurementPlan.model_validate(plan["measurement_plan"])
        if measurement_plan.synthetic:
            raise MeasurementSafetyError("real harness refuses synthetic measurement plans")
        formal = plan.get("mode") == "formal"
        context = dict(plan.get("_job_context", {}))
        cleanup_evidence: dict[str, Any] | None = None
        if formal:
            self._require_formal_context(context)
        try:
            calibration = calibrate_device_timer(
                self.clock,
                self.device_timer,
                sample_count=int(plan.get("calibration_samples", 3)),
                synchronize=self.synchronize,
                device_name=str(plan.get("device_timer_name", "hcu-device-timer")),
            )
            observations = [self._observe(context)]
            samples = collect_samples(
                measurement_plan,
                clock=self.clock,
                workload_factory=self.workload_factory,
                device_timer=self.device_timer,
                before_sample=lambda: self._require_live_lease(context) if formal else None,
            )
            observations.append(self._observe(context))
            evidence = MeasurementEvidence(
                protocol_version=measurement_plan.protocol_version,
                stable_fingerprint=stable_fingerprint(self.stable_identity),
                environment_fingerprint=measurement_plan.environment_fingerprint,
                observations=observations,
                calibration=calibration,
                raw_samples=samples,
            )
            measurement_id = MeasurementSeries(
                status="measured",
                metric_name=measurement_plan.metric_name,
                unit=measurement_plan.unit,
                protocol_version=measurement_plan.protocol_version,
                sample_count=len(samples),
                warmup_count=measurement_plan.warmup_count,
                process_restart_count=measurement_plan.process_restart_count,
                raw_samples_uri="file:///pending",
                raw_samples_hash="sha256:" + "0" * 64,
                environment_fingerprint=measurement_plan.environment_fingerprint,
                adapter_provenance=self.provenance,
            ).measurement_id
            artifact = write_evidence(
                output_dir / "measurements" / f"{measurement_id}.json",
                evidence,
            )
        finally:
            if formal:
                cleanup_evidence = self._cleanup(context)
        if formal and not _cleanup_is_healthy(cleanup_evidence):
            raise MeasurementSafetyError("formal measurement cleanup is unhealthy")
        return HarnessRun(
            series=MeasurementSeries(
                measurement_id=measurement_id,
                status="measured",
                metric_name=measurement_plan.metric_name,
                unit=measurement_plan.unit,
                protocol_version=measurement_plan.protocol_version,
                sample_count=len(samples),
                warmup_count=measurement_plan.warmup_count,
                process_restart_count=measurement_plan.process_restart_count,
                raw_samples_uri=artifact.uri,
                raw_samples_hash=artifact.sha256,
                environment_fingerprint=measurement_plan.environment_fingerprint,
                summary={
                    "stable_fingerprint": evidence.stable_fingerprint,
                    "device_timer_ns_per_tick": calibration.ns_per_tick,
                    "device_timer_max_residual_ns": calibration.max_residual_ns,
                    "raw_sample_count": len(samples),
                },
                adapter_provenance=self.provenance,
            ),
            evidence=evidence,
            artifact=artifact,
            cleanup_evidence=cleanup_evidence,
        )

    def _observe(self, context: Mapping[str, Any]) -> DynamicObservation:
        return DynamicObservation(
            captured_monotonic_ns=self.clock.now_ns(),
            telemetry=dict(self.telemetry.collect()),
            lease={
                key: context[key]
                for key in ("lease_id", "resource_id", "fencing_token")
                if context.get(key) is not None
            },
        )

    def _require_formal_context(self, context: Mapping[str, Any]) -> None:
        if context.get("lease_scope") != LeaseScope.EXCLUSIVE.value:
            raise MeasurementSafetyError("formal measurement requires an exclusive lease scope")
        if context.get("resource_id") is None or context.get("fencing_token") is None:
            raise MeasurementSafetyError("formal measurement requires resource and fencing token")
        if context.get("lease_id") is None:
            raise MeasurementSafetyError("formal measurement requires an exclusive lease")
        self._require_live_lease(context)
        if self.cleaner is None:
            raise MeasurementSafetyError("formal measurement requires a cleanup controller")

    @staticmethod
    def _require_live_lease(context: Mapping[str, Any]) -> None:
        event = context.get("lease_lost_event")
        if event is not None and callable(getattr(event, "is_set", None)) and event.is_set():
            raise MeasurementSafetyError("measurement lease was lost")

    def _cleanup(self, context: Mapping[str, Any]) -> dict[str, Any]:
        assert self.cleaner is not None
        resource_id = str(context["resource_id"])
        fencing_token = int(context["fencing_token"])
        return {
            "fence": dict(self.cleaner.fence(resource_id, fencing_token)),
            "health": dict(self.cleaner.health_check(resource_id)),
        }


def _cleanup_is_healthy(evidence: Mapping[str, Any] | None) -> bool:
    if evidence is None:
        return False
    fence = evidence.get("fence")
    health = evidence.get("health")
    return (
        isinstance(fence, Mapping)
        and isinstance(health, Mapping)
        and fence.get("fenced") is True
        and health.get("healthy") is True
    )
