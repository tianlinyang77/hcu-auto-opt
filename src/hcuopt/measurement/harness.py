from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hcuopt.contracts.platform_v1 import AdapterProvenance, MeasurementSeries
from hcuopt.domain.enums import LeaseScope, Stage0ProbeType, Stage0RunMode
from hcuopt.measurement.evidence import EvidenceArtifact, write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.models import (
    DynamicObservation,
    DynamicObservationV2,
    FormalWorkloadTimingV2,
    MeasurementEvidence,
    MeasurementEvidenceV2,
    MeasurementPlan,
    MeasurementPlanV2,
    ProcessIdentity,
    ProcessLifecycleRecordV2,
    RawEvidenceFileV2,
    RawSampleV2,
    Stage0AdapterProvenance,
    Stage0EvidenceBinding,
    Stage0Segment,
    TelemetrySnapshotV2,
)
from hcuopt.measurement.sampling import SampledWorkload, collect_samples
from hcuopt.measurement.timers import (
    DeviceTimer,
    HostClock,
    HostMonotonicClock,
    calibrate_device_timer,
    calibrate_device_timer_v2,
)


class MeasurementSafetyError(RuntimeError):
    pass


class TelemetryCollector(Protocol):
    def collect(self) -> Mapping[str, Any]: ...


class CleanupController(Protocol):
    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, Any]: ...

    def health_check(self, resource_id: str) -> Mapping[str, Any]: ...


class FormalStage0Workload(Protocol):
    """One independently restarted process that can execute registered segments."""

    def process_identity(self) -> ProcessIdentity: ...

    def synchronize(self) -> None: ...

    def warmup_segment(self, segment: Stage0Segment) -> None: ...

    def measure_segment_batch(
        self,
        segment: Stage0Segment,
        iterations: int,
    ) -> Mapping[str, Any] | FormalWorkloadTimingV2: ...

    def close(self) -> None: ...

    def is_alive(self) -> bool: ...


class ProcessLifecycleRecorder(Protocol):
    """Capture raw /proc identity and waitpid evidence outside the measured process."""

    def record_started(
        self,
        workload: FormalStage0Workload,
        *,
        restart_ordinal: int,
        captured_monotonic_ns: int,
    ) -> Mapping[str, Any] | ProcessLifecycleRecordV2: ...

    def record_reaped(
        self,
        workload: FormalStage0Workload,
        *,
        restart_ordinal: int,
        captured_monotonic_ns: int,
    ) -> Mapping[str, Any] | ProcessLifecycleRecordV2: ...


@dataclass(frozen=True, slots=True)
class HarnessRun:
    series: MeasurementSeries
    evidence: MeasurementEvidence
    artifact: EvidenceArtifact
    cleanup_evidence: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class FormalHarnessRun:
    evidence: MeasurementEvidenceV2
    artifact: EvidenceArtifact
    cleanup_evidence: dict[str, Any]


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
        formal_workload_factory: (
            Callable[[Stage0ProbeType, int], FormalStage0Workload] | None
        ) = None,
        lifecycle_recorder: ProcessLifecycleRecorder | None = None,
    ) -> None:
        if provenance.implementation_kind != "real":
            raise ValueError("EvidenceMeasurementHarness requires real provenance")
        self.provenance = provenance
        self.stable_identity = dict(stable_identity)
        self.environment_fingerprint = stable_fingerprint(self.stable_identity)
        self.workload_factory = workload_factory
        self.telemetry = telemetry
        self.device_timer = device_timer
        self.synchronize = synchronize
        self.clock = clock or HostMonotonicClock()
        self.cleaner = cleaner
        self.formal_workload_factory = formal_workload_factory
        self.lifecycle_recorder = lifecycle_recorder

    def run(self, plan: Mapping[str, Any], output_dir: Path) -> MeasurementSeries:
        return self.run_with_evidence(plan, output_dir).series

    def cleanup_formal_context(self, context: Mapping[str, Any]) -> dict[str, Any]:
        self._require_formal_context(context)
        return self._cleanup(context)

    def require_formal_context(self, context: Mapping[str, Any]) -> None:
        self._require_formal_context(context)

    def collect_formal_telemetry(self) -> TelemetrySnapshotV2:
        try:
            return TelemetrySnapshotV2.model_validate(dict(self.telemetry.collect()))
        except (TypeError, ValueError) as exc:
            raise MeasurementSafetyError(
                f"Formal Stage 0 telemetry is not measurement-evidence-v2 compatible: {exc}"
            ) from exc

    def run_with_evidence(self, plan: Mapping[str, Any], output_dir: Path) -> HarnessRun:
        measurement_plan = MeasurementPlan.model_validate(plan["measurement_plan"])
        if measurement_plan.synthetic:
            raise MeasurementSafetyError("real harness refuses synthetic measurement plans")
        if measurement_plan.environment_fingerprint != self.environment_fingerprint:
            raise MeasurementSafetyError(
                "measurement plan environment fingerprint does not match the bound target"
            )
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
                resolution_sample_count=int(plan.get("resolution_samples", 64)),
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
                require_fresh_processes=formal,
            )
            observations.append(self._observe(context))
            evidence = MeasurementEvidence(
                protocol_version=measurement_plan.protocol_version,
                stable_fingerprint=self.environment_fingerprint,
                environment_fingerprint=self.environment_fingerprint,
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

    def run_stage0_v2(
        self,
        *,
        binding: Stage0EvidenceBinding,
        plan: MeasurementPlanV2,
        adapter_provenance: tuple[Stage0AdapterProvenance, ...],
        output_dir: Path,
        job_context: Mapping[str, Any],
        calibration_samples: int = 5,
        resolution_samples: int = 64,
        device_timer_name: str = "hcu-device-timer",
    ) -> FormalHarnessRun:
        """Collect one Formal timing envelope without trusting producer summaries."""

        if binding.run_mode is not Stage0RunMode.FORMAL:
            raise MeasurementSafetyError("run_stage0_v2 requires a Formal evidence binding")
        if binding.environment_fingerprint != self.environment_fingerprint:
            raise MeasurementSafetyError(
                "Formal evidence binding does not match the harness environment"
            )
        self._require_formal_context(job_context)
        if self.formal_workload_factory is None:
            raise MeasurementSafetyError(
                "Formal Stage 0 requires a segmented external-process workload factory"
            )
        if self.lifecycle_recorder is None:
            raise MeasurementSafetyError("Formal Stage 0 requires a raw process lifecycle recorder")

        cleanup_evidence: dict[str, Any] | None = None
        try:
            calibration = calibrate_device_timer_v2(
                self.clock,
                self.device_timer,
                device_index=self._formal_device_index(binding),
                sample_count=calibration_samples,
                resolution_sample_count=resolution_samples,
                synchronize=self.synchronize,
                device_name=device_timer_name,
            )
            observations: list[DynamicObservationV2] = [self._observe_v2("before_run")]
            samples: list[RawSampleV2] = []
            process_identities: set[tuple[int, str]] = set()
            lifecycle_root = (
                output_dir
                / "stage0"
                / str(binding.stage0_run_id)
                / binding.probe_type.value
                / str(binding.measurement_id)
                / "process-lifecycle"
            )
            for restart_ordinal in range(plan.restart_count):
                workload = self.formal_workload_factory(
                    binding.probe_type,
                    restart_ordinal,
                )
                identity = ProcessIdentity.model_validate(workload.process_identity())
                identity_key = (identity.pid, identity.start_token)
                if identity.pid == os.getpid():
                    raise MeasurementSafetyError(
                        "Formal measurement workloads must run outside the harness process"
                    )
                if identity_key in process_identities:
                    raise MeasurementSafetyError(
                        "Formal process restart groups must use distinct process identities"
                    )
                process_identities.add(identity_key)
                try:
                    started_at = self._next_observation_time(observations)
                    started_record = ProcessLifecycleRecordV2.model_validate(
                        self.lifecycle_recorder.record_started(
                            workload,
                            restart_ordinal=restart_ordinal,
                            captured_monotonic_ns=started_at,
                        )
                    )
                    self._bind_lifecycle_record(
                        started_record,
                        event="started",
                        restart_ordinal=restart_ordinal,
                        identity=identity,
                        captured_monotonic_ns=started_at,
                    )
                    started_artifact = write_evidence(
                        lifecycle_root / f"restart-{restart_ordinal}-started.json",
                        started_record,
                    )
                    observations.append(
                        self._observe_v2(
                            "before_restart",
                            captured_monotonic_ns=started_at,
                            restart_ordinal=restart_ordinal,
                            identity=identity,
                            lifecycle=RawEvidenceFileV2(
                                uri=started_artifact.uri,
                                sha256=started_artifact.sha256,
                            ),
                        )
                    )
                    for segment in plan.segment_order:
                        for _ in range(plan.warmup_count):
                            workload.synchronize()
                            workload.warmup_segment(segment)
                        for segment_sample_ordinal in range(plan.samples_per_segment):
                            self._require_live_lease(job_context)
                            timing = FormalWorkloadTimingV2.model_validate(
                                workload.measure_segment_batch(
                                    segment,
                                    plan.batch_iterations,
                                )
                            )
                            if (
                                timing.process_id,
                                timing.process_start_token,
                            ) != identity_key:
                                raise MeasurementSafetyError(
                                    "Formal workload timing does not match its procfs identity"
                                )
                            if (
                                timing.segment != segment
                                or timing.batch_iterations != plan.batch_iterations
                            ):
                                raise MeasurementSafetyError(
                                    "Formal workload timing does not match its requested segment"
                                )
                            samples.append(
                                RawSampleV2(
                                    process_id=identity.pid,
                                    process_start_token=identity.start_token,
                                    restart_ordinal=restart_ordinal,
                                    arm=_segment_arm(segment),
                                    segment=segment,
                                    acquisition_ordinal=len(samples),
                                    segment_sample_ordinal=segment_sample_ordinal,
                                    started_monotonic_ns=timing.started_monotonic_ns,
                                    finished_monotonic_ns=timing.finished_monotonic_ns,
                                    started_device_ticks=timing.started_device_ticks,
                                    finished_device_ticks=timing.finished_device_ticks,
                                    batch_iterations=plan.batch_iterations,
                                )
                            )
                finally:
                    workload.close()
                if workload.is_alive():
                    raise MeasurementSafetyError(
                        "Formal measurement workload remained alive after close"
                    )
                reaped_at = self._next_observation_time(observations, samples=samples)
                reaped_record = ProcessLifecycleRecordV2.model_validate(
                    self.lifecycle_recorder.record_reaped(
                        workload,
                        restart_ordinal=restart_ordinal,
                        captured_monotonic_ns=reaped_at,
                    )
                )
                self._bind_lifecycle_record(
                    reaped_record,
                    event="reaped",
                    restart_ordinal=restart_ordinal,
                    identity=identity,
                    captured_monotonic_ns=reaped_at,
                )
                reaped_artifact = write_evidence(
                    lifecycle_root / f"restart-{restart_ordinal}-reaped.json",
                    reaped_record,
                )
                observations.append(
                    self._observe_v2(
                        "after_restart",
                        captured_monotonic_ns=reaped_at,
                        restart_ordinal=restart_ordinal,
                        identity=identity,
                        lifecycle=RawEvidenceFileV2(
                            uri=reaped_artifact.uri,
                            sha256=reaped_artifact.sha256,
                        ),
                    )
                )
            observations.append(
                self._observe_v2(
                    "after_run",
                    captured_monotonic_ns=self._next_observation_time(
                        observations,
                        samples=samples,
                    ),
                )
            )
            evidence = MeasurementEvidenceV2(
                binding=binding,
                plan=plan,
                calibration=calibration,
                samples=tuple(samples),
                observations=tuple(observations),
                adapter_provenance=adapter_provenance,
            )
            artifact = write_evidence(
                output_dir
                / "stage0"
                / str(binding.stage0_run_id)
                / binding.probe_type.value
                / str(binding.measurement_id)
                / "measurement-evidence-v2.json",
                evidence,
            )
        finally:
            cleanup_evidence = self._cleanup(job_context)
        if not _cleanup_is_healthy(cleanup_evidence):
            raise MeasurementSafetyError("formal measurement cleanup is unhealthy")
        return FormalHarnessRun(
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

    def _observe_v2(
        self,
        phase: str,
        *,
        captured_monotonic_ns: int | None = None,
        restart_ordinal: int | None = None,
        identity: ProcessIdentity | None = None,
        lifecycle: RawEvidenceFileV2 | None = None,
    ) -> DynamicObservationV2:
        return DynamicObservationV2(
            phase=phase,
            captured_monotonic_ns=(
                self.clock.now_ns() if captured_monotonic_ns is None else captured_monotonic_ns
            ),
            restart_ordinal=restart_ordinal,
            process_id=identity.pid if identity is not None else None,
            process_start_token=identity.start_token if identity is not None else None,
            process_lifecycle_record=lifecycle,
            telemetry=self.collect_formal_telemetry(),
        )

    def _next_observation_time(
        self,
        observations: list[DynamicObservationV2],
        *,
        samples: list[RawSampleV2] | None = None,
    ) -> int:
        captured = self.clock.now_ns()
        lower_bound = observations[-1].captured_monotonic_ns if observations else -1
        if samples:
            lower_bound = max(lower_bound, samples[-1].finished_monotonic_ns)
        if captured <= lower_bound:
            raise MeasurementSafetyError(
                "Formal telemetry clock did not advance beyond prior evidence"
            )
        return captured

    @staticmethod
    def _bind_lifecycle_record(
        record: ProcessLifecycleRecordV2,
        *,
        event: str,
        restart_ordinal: int,
        identity: ProcessIdentity,
        captured_monotonic_ns: int,
    ) -> None:
        expected = (
            event,
            restart_ordinal,
            identity.pid,
            captured_monotonic_ns,
        )
        actual = (
            record.event,
            record.restart_ordinal,
            record.process_id,
            record.captured_monotonic_ns,
        )
        if actual != expected:
            raise MeasurementSafetyError(
                "Formal process lifecycle record does not match its restart identity"
            )

    @staticmethod
    def _formal_device_index(binding: Stage0EvidenceBinding) -> int:
        prefix = "hcu-"
        if not binding.lease.resource_id.startswith(prefix):
            raise MeasurementSafetyError("Formal measurement resource_id must be hcu-<index>")
        try:
            return int(binding.lease.resource_id.removeprefix(prefix))
        except ValueError as exc:
            raise MeasurementSafetyError(
                "Formal measurement resource_id has no numeric device index"
            ) from exc

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


def _segment_arm(segment: Stage0Segment) -> str:
    if segment in {"A1", "A2"}:
        return "baseline"
    if segment in {"B1", "B2"}:
        return "comparison"
    return "single"
