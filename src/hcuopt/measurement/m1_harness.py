from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    MeasurementSeries,
    TargetSpec,
)
from hcuopt.contracts.v1 import ManualPerformanceEvidenceResult
from hcuopt.domain.enums import LeaseScope
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_verifier import Stage0EvidenceReader
from hcuopt.measurement.evidence import write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.harness import (
    CleanupController,
    MeasurementSafetyError,
    ProcessLifecycleRecorder,
    TelemetryCollector,
)
from hcuopt.measurement.m1_models import (
    M1AcquisitionEvidence,
    M1ActivationEvidence,
    M1Arm,
    M1MeasurementBinding,
    M1MeasurementEvidence,
    M1MeasurementPlan,
    M1RawSample,
    M1Stage0ReportReference,
    M1WorkloadTiming,
    m1_plan_hash,
)
from hcuopt.measurement.m1_stage0 import load_m1_stage0_authority
from hcuopt.measurement.models import (
    ProcessIdentity,
    ProcessLifecycleRecordV2,
    RawEvidenceFileV2,
    Stage0AdapterProvenance,
    Stage0LeaseBinding,
    TelemetrySnapshotV2,
)
from hcuopt.measurement.timers import (
    DeviceTimer,
    HostClock,
    HostMonotonicClock,
    calibrate_device_timer_v2,
)
from hcuopt.targets import target_fingerprint


class M1PairedWorkload(Protocol):
    """One fresh baseline or startup-overlay process supplied by the C integration."""

    def process_identity(self) -> ProcessIdentity: ...

    def activation_evidence(self) -> Mapping[str, Any] | M1ActivationEvidence: ...

    def synchronize(self) -> None: ...

    def warmup(self) -> None: ...

    def measure_batch(self, iterations: int) -> Mapping[str, Any] | M1WorkloadTiming: ...

    def close(self) -> None: ...

    def is_alive(self) -> bool: ...


M1WorkloadFactory = Callable[[M1Arm, int, Mapping[str, Any], Path], M1PairedWorkload]
M1PlanFactory = Callable[[Mapping[str, Any], Any], M1MeasurementPlan]


class M1TrustedMeasurementHarness:
    """M1-B raw-evidence producer. It deliberately has no verdict method."""

    def __init__(
        self,
        *,
        provenance: AdapterProvenance,
        evidence_root: Path,
        evidence_reader: Stage0EvidenceReader | None = None,
        workload_factory: M1WorkloadFactory,
        telemetry: TelemetryCollector,
        device_timer: DeviceTimer,
        lifecycle_recorder: ProcessLifecycleRecorder,
        cleaner: CleanupController,
        synchronize: Callable[[], None] | None = None,
        clock: HostClock | None = None,
        plan_factory: M1PlanFactory | None = None,
    ) -> None:
        if provenance.implementation_kind != "real":
            raise ValueError("M1 trusted measurement requires real provenance")
        self.provenance = provenance
        self.reader = evidence_reader or Stage0EvidenceReader(evidence_root)
        self.workload_factory = workload_factory
        self.telemetry = telemetry
        self.device_timer = device_timer
        self.lifecycle_recorder = lifecycle_recorder
        self.cleaner = cleaner
        self.synchronize = synchronize
        self.clock = clock or HostMonotonicClock()
        self.plan_factory = plan_factory or _default_plan

    def run(self, plan: Mapping[str, Any], output_dir: Path) -> MeasurementSeries:
        return self.run_manual_performance(plan, output_dir).measurement

    def run_manual_performance(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> ManualPerformanceEvidenceResult:
        context = _exclusive_context(payload)
        target = TargetSpec.model_validate(payload["target"])
        if payload["target_fingerprint"] != target_fingerprint(target):
            raise MeasurementSafetyError("M1 Target fingerprint does not match TargetSpec")
        artifact = ArtifactManifest.model_validate(payload["artifact"])
        candidate_id = UUID(str(payload["candidate_id"]))
        if artifact.candidate_id != candidate_id or artifact.synthetic:
            raise MeasurementSafetyError("M1 performance Artifact is not the real Candidate")
        reference = M1Stage0ReportReference.model_validate(payload["stage0_report"])
        authority = load_m1_stage0_authority(self.reader, reference, task_payload=payload)
        plan = self.plan_factory(payload, authority)
        _enforce_budget(payload, plan)
        measurement_id = uuid4()
        binding = M1MeasurementBinding(
            task_id=UUID(str(payload["task_id"])),
            candidate_id=candidate_id,
            round_id=UUID(str(payload["round_id"])),
            baseline_epoch_id=UUID(str(payload["baseline_epoch_id"])),
            stage0_run_id=UUID(str(payload["stage0_run_id"])),
            target_snapshot_id=UUID(str(payload["target_snapshot_id"])),
            target_id=target.target_id,
            target_fingerprint=payload["target_fingerprint"],
            environment_fingerprint=stable_fingerprint(target.model_dump(mode="json")),
            workload_id=payload["workload_id"],
            workload_hash=payload["workload_hash"],
            configuration_hash=payload["configuration_hash"],
            image_digest=target.inference_image.registry_digest,
            baseline_source_hash=payload["baseline_source"]["source_hash"],
            candidate_source_hash=payload["candidate_source"]["source_hash"],
            artifact_id=artifact.artifact_id,
            artifact_content_hash=artifact.content_hash,
            measurement_id=measurement_id,
            lease=Stage0LeaseBinding(
                lease_id=UUID(str(context["lease_id"])),
                lease_scope=LeaseScope(str(context["lease_scope"])),
                resource_id=str(context["resource_id"]),
                fencing_token=int(context["fencing_token"]),
            ),
        )
        cleanup: dict[str, Any] | None = None
        acquisitions: list[M1AcquisitionEvidence] = []
        try:
            calibration = calibrate_device_timer_v2(
                self.clock,
                self.device_timer,
                device_index=target.execution_host.accelerator.device_index,
                synchronize=self.synchronize,
                device_name="hcu-device-event",
            )
            for acquisition_ordinal, arm in enumerate(plan.acquisition_order):
                _require_live_lease(context)
                acquisitions.append(
                    self._acquire(
                        arm,
                        acquisition_ordinal,
                        plan,
                        binding,
                        payload,
                        output_dir,
                        context,
                    )
                )
            evidence = M1MeasurementEvidence(
                binding=binding,
                plan=plan,
                plan_hash=m1_plan_hash(plan),
                calibration=calibration,
                acquisitions=tuple(acquisitions),
                adapter_provenance=(
                    Stage0AdapterProvenance.model_validate(
                        self.provenance.model_dump(mode="python")
                    ),
                ),
            )
            artifact_file = write_evidence(
                output_dir
                / "m1"
                / str(binding.task_id)
                / str(binding.candidate_id)
                / str(measurement_id)
                / "measurement-evidence.json",
                evidence,
            )
        except BaseException as exc:
            write_evidence(
                output_dir
                / "m1"
                / str(binding.task_id)
                / str(binding.candidate_id)
                / str(measurement_id)
                / "failure-evidence.json",
                {
                    "schema_version": "m1-measurement-failure-v1",
                    "binding": binding.model_dump(mode="json"),
                    "plan_hash": m1_plan_hash(plan),
                    "completed_acquisitions": [
                        item.model_dump(mode="json") for item in acquisitions
                    ],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
        finally:
            cleanup = {
                "fence": dict(
                    self.cleaner.fence(
                        str(context["resource_id"]), int(context["fencing_token"])
                    )
                ),
                "health": dict(self.cleaner.health_check(str(context["resource_id"]))),
            }
        if not cleanup_is_healthy(cleanup):
            raise MeasurementSafetyError("M1 performance cleanup is unhealthy")
        series = MeasurementSeries(
            measurement_id=measurement_id,
            status="measured",
            metric_name=plan.metric_name,
            unit=plan.unit,
            protocol_version=plan.protocol_version,
            sample_count=plan.expected_sample_count,
            warmup_count=plan.warmup_count,
            process_restart_count=plan.process_restart_count,
            raw_samples_uri=artifact_file.uri,
            raw_samples_hash=artifact_file.sha256,
            environment_fingerprint=binding.environment_fingerprint,
            summary={
                "plan_hash": evidence.plan_hash,
                "stage0_report_hash": authority.report.sha256,
                "stage0_input_digest": authority.report.input_digest,
                "raw_sample_count": plan.expected_sample_count,
                "verdict_owner": "candidate_adjudicator",
            },
            adapter_provenance=self.provenance,
        )
        return ManualPerformanceEvidenceResult(
            candidate_id=candidate_id,
            measurement=series,
            cleanup_evidence=cleanup,
        )

    def _acquire(
        self,
        arm: M1Arm,
        acquisition_ordinal: int,
        plan: M1MeasurementPlan,
        binding: M1MeasurementBinding,
        payload: Mapping[str, Any],
        output_dir: Path,
        context: Mapping[str, Any],
    ) -> M1AcquisitionEvidence:
        workload = self.workload_factory(arm, acquisition_ordinal, payload, output_dir)
        identity = ProcessIdentity.model_validate(workload.process_identity())
        lifecycle_root = (
            output_dir
            / "m1"
            / str(binding.task_id)
            / str(binding.candidate_id)
            / str(binding.measurement_id)
            / "process-lifecycle"
        )
        started_at = self.clock.now_ns()
        started = ProcessLifecycleRecordV2.model_validate(
            self.lifecycle_recorder.record_started(
                workload,
                restart_ordinal=acquisition_ordinal,
                captured_monotonic_ns=started_at,
            )
        )
        _validate_lifecycle(started, "started", acquisition_ordinal, identity)
        started_file = write_evidence(
            lifecycle_root / f"acquisition-{acquisition_ordinal}-started.json", started
        )
        before = _telemetry(self.telemetry)
        samples: list[M1RawSample] = []
        activation: M1ActivationEvidence | None = None
        reaped_file = None
        try:
            activation = M1ActivationEvidence.model_validate(workload.activation_evidence())
            if activation.arm != arm or activation.image_digest != binding.image_digest:
                raise MeasurementSafetyError("M1 process activation does not match its arm/image")
            if (
                arm == "candidate"
                and activation.loaded_artifact_hash != binding.artifact_content_hash
            ):
                raise MeasurementSafetyError("M1 Candidate process loaded another Artifact")
            for _ in range(plan.warmup_count):
                _require_live_lease(context)
                workload.synchronize()
                workload.warmup()
            for sample_ordinal in range(plan.samples_per_acquisition):
                _require_live_lease(context)
                timing = M1WorkloadTiming.model_validate(
                    workload.measure_batch(plan.batch_iterations)
                )
                if (timing.process_id, timing.process_start_token) != (
                    identity.pid,
                    identity.start_token,
                ):
                    raise MeasurementSafetyError("M1 timing came from another process")
                samples.append(
                    M1RawSample(
                        **timing.model_dump(mode="python"),
                        arm=arm,
                        acquisition_ordinal=acquisition_ordinal,
                        sample_ordinal=sample_ordinal,
                    )
                )
        finally:
            workload.close()
            if workload.is_alive():
                raise MeasurementSafetyError(
                    "M1 acquisition process remained alive after close"
                )
            reaped_at = self.clock.now_ns()
            reaped = ProcessLifecycleRecordV2.model_validate(
                self.lifecycle_recorder.record_reaped(
                    workload,
                    restart_ordinal=acquisition_ordinal,
                    captured_monotonic_ns=reaped_at,
                )
            )
            _validate_lifecycle(reaped, "reaped", acquisition_ordinal, identity)
            reaped_file = write_evidence(
                lifecycle_root / f"acquisition-{acquisition_ordinal}-reaped.json",
                reaped,
            )
        after = _telemetry(self.telemetry)
        assert activation is not None
        assert reaped_file is not None
        return M1AcquisitionEvidence(
            acquisition_ordinal=acquisition_ordinal,
            arm=arm,
            process_id=identity.pid,
            process_start_token=identity.start_token,
            activation=activation,
            started_lifecycle=RawEvidenceFileV2(uri=started_file.uri, sha256=started_file.sha256),
            reaped_lifecycle=RawEvidenceFileV2(uri=reaped_file.uri, sha256=reaped_file.sha256),
            before=before,
            after=after,
            samples=tuple(samples),
        )


def _default_plan(payload: Mapping[str, Any], authority: Any) -> M1MeasurementPlan:
    protocol = load_registered_stage0_protocol(authority.report.protocol_version).protocol
    sampling = protocol.sampling
    return M1MeasurementPlan(
        acquisition_order=("baseline", "candidate", "candidate", "baseline")
        * sampling.restart_count,
        warmup_count=sampling.warmup_count,
        samples_per_acquisition=sampling.signal_samples_per_segment,
        batch_iterations=sampling.batch_iterations,
        stage0_authority=authority,
    )


def _exclusive_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    context = payload.get("_job_context")
    if not isinstance(context, Mapping):
        raise MeasurementSafetyError("M1 performance requires a Job lease context")
    required = ("lease_id", "resource_id", "fencing_token")
    if context.get("lease_scope") != LeaseScope.EXCLUSIVE.value or any(
        context.get(field) is None for field in required
    ):
        raise MeasurementSafetyError("M1 performance requires a complete exclusive lease")
    return context


def _require_live_lease(context: Mapping[str, Any]) -> None:
    event = context.get("lease_lost_event")
    if event is not None and callable(getattr(event, "is_set", None)) and event.is_set():
        raise MeasurementSafetyError("M1 performance lease was lost")


def _enforce_budget(payload: Mapping[str, Any], plan: M1MeasurementPlan) -> None:
    budget = payload.get("budget", {})
    if not isinstance(budget, Mapping):
        raise MeasurementSafetyError("M1 budget must be a mapping")
    maximum = budget.get("max_samples")
    if maximum is not None and plan.expected_sample_count > int(maximum):
        raise MeasurementSafetyError("M1 trusted plan exceeds the Task sample budget")


def _telemetry(collector: TelemetryCollector) -> TelemetrySnapshotV2:
    return TelemetrySnapshotV2.model_validate(dict(collector.collect()))


def _validate_lifecycle(
    record: ProcessLifecycleRecordV2,
    event: str,
    ordinal: int,
    identity: ProcessIdentity,
) -> None:
    if (
        record.event != event
        or record.restart_ordinal != ordinal
        or record.process_id != identity.pid
    ):
        raise MeasurementSafetyError("M1 lifecycle record does not match the acquisition")
