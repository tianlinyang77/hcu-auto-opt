# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

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
    M1CacheNamespaceRecord,
    M1CleanupEvidence,
    M1DeviceEventRecord,
    M1MeasurementBinding,
    M1MeasurementEvidence,
    M1MeasurementPlan,
    M1OverlayImportRecord,
    M1RawSample,
    M1Stage0ReportReference,
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

    def measure_batch(self, iterations: int) -> Mapping[str, Any] | RawEvidenceFileV2: ...

    def close(self) -> None: ...

    def is_alive(self) -> bool: ...


@runtime_checkable
class M1WorkloadFactory(Protocol):
    """C-owned constructor consumed by B's only formal Measurement Harness."""

    def __call__(
        self,
        arm: M1Arm,
        acquisition_ordinal: int,
        payload: Mapping[str, Any],
        output_dir: Path,
    ) -> M1PairedWorkload: ...


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
        if provenance.implementation_kind != "real" or provenance.capability != (
            "measurement_harness"
        ):
            raise ValueError("M1 trusted measurement requires real Harness provenance")
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
        if payload.get("adapter_profile") != self.provenance.profile:
            raise MeasurementSafetyError(
                "M1 Task Adapter Profile does not match the Measurement Harness"
            )
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
        deadline_ns = _wall_deadline_ns(payload, self.clock)
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
            adapter_profile=self.provenance.profile,
            lease=Stage0LeaseBinding(
                lease_id=UUID(str(context["lease_id"])),
                lease_scope=LeaseScope(str(context["lease_scope"])),
                resource_id=str(context["resource_id"]),
                fencing_token=int(context["fencing_token"]),
            ),
        )
        measurement_root = output_dir / "m1" / measurement_id.hex
        cleanup: dict[str, Any]
        acquisitions: list[M1AcquisitionEvidence] = []
        calibration = None
        collection_error: BaseException | None = None
        try:
            _require_within_wall_budget(deadline_ns, self.clock)
            calibration = calibrate_device_timer_v2(
                self.clock,
                self.device_timer,
                device_index=target.execution_host.accelerator.device_index,
                synchronize=self.synchronize,
                device_name="hcu-device-event",
            )
            for acquisition_ordinal, arm in enumerate(plan.acquisition_order):
                _require_live_lease(context)
                _require_within_wall_budget(deadline_ns, self.clock)
                acquisitions.append(
                    self._acquire(
                        arm,
                        acquisition_ordinal,
                        plan,
                        binding,
                        payload,
                        measurement_root,
                        context,
                        deadline_ns,
                    )
                )
        except BaseException as exc:
            collection_error = exc
        finally:
            cleanup = _cleanup_resource(self.cleaner, context)

        failure = collection_error
        cleanup_model: M1CleanupEvidence | None = None
        if failure is None:
            try:
                _require_within_wall_budget(deadline_ns, self.clock)
                cleanup_model = M1CleanupEvidence.model_validate(cleanup)
                _require_cleanup_binding(cleanup_model, binding)
            except BaseException as exc:
                failure = exc
        if failure is not None:
            _write_failure_evidence(
                measurement_root,
                binding=binding,
                plan=plan,
                acquisitions=acquisitions,
                calibration=calibration,
                cleanup=cleanup,
                error=failure,
            )
            raise failure

        assert calibration is not None
        assert cleanup_model is not None
        try:
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
                cleanup_evidence=cleanup_model,
            )
            artifact_file = write_evidence(
                measurement_root / "performance.json",
                evidence,
            )
        except BaseException as exc:
            _write_failure_evidence(
                measurement_root,
                binding=binding,
                plan=plan,
                acquisitions=acquisitions,
                calibration=calibration,
                cleanup=cleanup,
                error=exc,
            )
            raise
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
            cleanup_evidence=cleanup_model.model_dump(mode="json"),
        )

    def _acquire(
        self,
        arm: M1Arm,
        acquisition_ordinal: int,
        plan: M1MeasurementPlan,
        binding: M1MeasurementBinding,
        payload: Mapping[str, Any],
        measurement_root: Path,
        context: Mapping[str, Any],
        deadline_ns: int | None,
    ) -> M1AcquisitionEvidence:
        workload = self.workload_factory(arm, acquisition_ordinal, payload, measurement_root)
        identity = ProcessIdentity.model_validate(workload.process_identity())
        lifecycle_root = measurement_root / "lifecycle"
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
            lifecycle_root / f"a{acquisition_ordinal}-start.json", started
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
            if arm == "candidate":
                assert activation.import_attestation is not None
                imported = M1OverlayImportRecord.model_validate_json(
                    self.reader.read_bytes(
                        activation.import_attestation.uri,
                        activation.import_attestation.sha256,
                    )
                )
                if (
                    imported.acquisition_ordinal != acquisition_ordinal
                    or imported.process_id != identity.pid
                    or imported.process_start_token != identity.start_token
                    or imported.image_digest != binding.image_digest
                    or imported.artifact_content_hash != binding.artifact_content_hash
                ):
                    raise MeasurementSafetyError(
                        "M1 Overlay import attestation belongs to another process or Artifact"
                    )
            cache_record = M1CacheNamespaceRecord.model_validate_json(
                self.reader.read_bytes(
                    activation.cache_namespace_evidence.uri,
                    activation.cache_namespace_evidence.sha256,
                )
            )
            if (
                cache_record.acquisition_ordinal != acquisition_ordinal
                or cache_record.arm != arm
                or cache_record.process_id != identity.pid
                or cache_record.process_start_token != identity.start_token
                or cache_record.namespace_hash != activation.cache_namespace_hash
                or not cache_record.empty_before_execution
            ):
                raise MeasurementSafetyError(
                    "M1 cache evidence does not prove a fresh empty acquisition namespace"
                )
            for _ in range(plan.warmup_count):
                _require_live_lease(context)
                _require_within_wall_budget(deadline_ns, self.clock)
                workload.synchronize()
                workload.warmup()
            for sample_ordinal in range(plan.samples_per_acquisition):
                _require_live_lease(context)
                _require_within_wall_budget(deadline_ns, self.clock)
                event_reference = RawEvidenceFileV2.model_validate(
                    workload.measure_batch(plan.batch_iterations)
                )
                event = M1DeviceEventRecord.model_validate_json(
                    self.reader.read_bytes(event_reference.uri, event_reference.sha256)
                )
                if (event.process_id, event.process_start_token) != (
                    identity.pid,
                    identity.start_token,
                ):
                    raise MeasurementSafetyError("M1 timing came from another process")
                if (
                    event.arm != arm
                    or event.acquisition_ordinal != acquisition_ordinal
                    or event.sample_ordinal != sample_ordinal
                    or event.batch_iterations != plan.batch_iterations
                ):
                    raise MeasurementSafetyError(
                        "M1 device Event record does not match its planned acquisition"
                    )
                if event.timer_provenance.model_dump(mode="python") != Stage0AdapterProvenance(
                    **self.provenance.model_dump(mode="python")
                ).model_dump(mode="python"):
                    raise MeasurementSafetyError(
                        "M1 device Event record was not produced by the registered B Harness"
                    )
                samples.append(
                    M1RawSample(
                        **event.model_dump(mode="python"),
                        device_event_record=event_reference,
                    )
                )
                _require_within_wall_budget(deadline_ns, self.clock)
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
            if started.captured_monotonic_ns >= reaped.captured_monotonic_ns:
                raise MeasurementSafetyError("M1 lifecycle timestamps are not ordered")
            reaped_file = write_evidence(
                lifecycle_root / f"a{acquisition_ordinal}-exit.json",
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
    load_registered_stage0_protocol(authority.report.protocol_version)
    budget = authority.sample_budget
    return M1MeasurementPlan(
        acquisition_order=budget.acquisition_order,
        warmup_count=budget.warmup_count,
        samples_per_acquisition=budget.samples_per_acquisition,
        batch_iterations=budget.batch_iterations,
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
    if maximum is not None:
        maximum = _positive_budget_int(maximum, "max_samples")
        if plan.expected_sample_count > maximum:
            raise MeasurementSafetyError("M1 trusted plan exceeds the Task sample budget")
    maximum_wall = budget.get("max_wall_seconds")
    if maximum_wall is not None:
        _positive_budget_int(maximum_wall, "max_wall_seconds")


def _wall_deadline_ns(payload: Mapping[str, Any], clock: HostClock) -> int | None:
    budget = payload.get("budget", {})
    if not isinstance(budget, Mapping):
        raise MeasurementSafetyError("M1 budget must be a mapping")
    maximum_wall = budget.get("max_wall_seconds")
    if maximum_wall is None:
        return None
    seconds = _positive_budget_int(maximum_wall, "max_wall_seconds")
    return clock.now_ns() + seconds * 1_000_000_000


def _positive_budget_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MeasurementSafetyError(f"M1 {field} must be a positive integer")
    return value


def _require_within_wall_budget(deadline_ns: int | None, clock: HostClock) -> None:
    if deadline_ns is not None and clock.now_ns() > deadline_ns:
        raise MeasurementSafetyError("M1 trusted measurement exceeded max_wall_seconds")


def _cleanup_resource(
    cleaner: CleanupController,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    resource_id = str(context["resource_id"])
    fencing_token = int(context["fencing_token"])
    try:
        fence = dict(cleaner.fence(resource_id, fencing_token))
    except BaseException as exc:
        fence = {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "fenced": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    try:
        health = dict(cleaner.health_check(resource_id))
    except BaseException as exc:
        health = {
            "resource_id": resource_id,
            "healthy": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    return {"fence": fence, "health": health}


def _require_cleanup_binding(
    cleanup: M1CleanupEvidence,
    binding: M1MeasurementBinding,
) -> None:
    if (
        cleanup.fence.get("resource_id") != binding.lease.resource_id
        or cleanup.fence.get("fencing_token") != binding.lease.fencing_token
        or cleanup.health.get("resource_id") != binding.lease.resource_id
    ):
        raise MeasurementSafetyError("M1 cleanup evidence belongs to another lease resource")


def _write_failure_evidence(
    measurement_root: Path,
    *,
    binding: M1MeasurementBinding,
    plan: M1MeasurementPlan,
    acquisitions: list[M1AcquisitionEvidence],
    calibration: Any,
    cleanup: Mapping[str, Any],
    error: BaseException,
) -> None:
    write_evidence(
        measurement_root / "failure.json",
        {
            "schema_version": "m1-measurement-failure-v1",
            "binding": binding.model_dump(mode="json"),
            "plan_hash": m1_plan_hash(plan),
            "completed_acquisitions": [
                item.model_dump(mode="json") for item in acquisitions
            ],
            "calibration": (
                calibration.model_dump(mode="json") if calibration is not None else None
            ),
            "cleanup_evidence": dict(cleanup),
            "error_type": type(error).__name__,
            "error": str(error),
        },
    )


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
    if _proc_start_token(record.proc_stat_line, record.process_id) != identity.start_token:
        raise MeasurementSafetyError("M1 lifecycle start token does not match the process")
    if event == "reaped" and (
        record.waitpid_result_pid != identity.pid or record.wait_status != 0
    ):
        raise MeasurementSafetyError("M1 measured process did not exit successfully")


def _proc_start_token(proc_stat_line: str, process_id: int) -> str:
    prefix = f"{process_id} ("
    if not proc_stat_line.startswith(prefix):
        raise MeasurementSafetyError("M1 procfs record identifies another process")
    close = proc_stat_line.rfind(") ")
    if close < len(prefix):
        raise MeasurementSafetyError("M1 procfs record has no parseable command field")
    remaining = proc_stat_line[close + 2 :].split()
    if len(remaining) < 20:
        raise MeasurementSafetyError("M1 procfs record has no starttime field")
    try:
        start_ticks = int(remaining[19])
    except ValueError as exc:
        raise MeasurementSafetyError("M1 procfs starttime is not an integer") from exc
    if start_ticks < 1:
        raise MeasurementSafetyError("M1 procfs starttime must be positive")
    return f"linux-proc-startticks:{start_ticks}"
