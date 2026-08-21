from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import LeaseScope, Stage0ProbeType, Stage0RunMode
from hcuopt.evaluation.stage0_protocol import (
    LoadedStage0Protocol,
    load_registered_stage0_protocol,
)
from hcuopt.evaluation.stage0_verifier import FingerprintEvidenceV2
from hcuopt.measurement.evidence import write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.harness import (
    EvidenceMeasurementHarness,
    HarnessRun,
    MeasurementSafetyError,
)
from hcuopt.measurement.models import (
    DynamicObservationV2,
    MeasurementPlanV2,
    Stage0AdapterProvenance,
    Stage0EvidenceBinding,
    Stage0LeaseBinding,
)
from hcuopt.targets import target_fingerprint


@dataclass(frozen=True, slots=True)
class Stage0ProbeOutput:
    summary: dict[str, Any]
    raw_evidence_uri: str
    raw_evidence_hash: str
    cleanup_evidence: dict[str, Any] | None
    synthetic: bool = False
    adapter_provenance: tuple[AdapterProvenance, ...] = ()


class Stage0MeasurementProbeAdapter:
    """B-owned probes; noise stays raw until D supplies the statistical verdict."""

    def __init__(
        self,
        harness: EvidenceMeasurementHarness,
        target: TargetSpec,
        *,
        measurement_plan_factory: Callable[[Stage0ProbeType, Mapping[str, Any]], Mapping[str, Any]],
        known_signal_detector: Callable[[HarnessRun, Mapping[str, Any]], bool],
        null_signal_detector: Callable[[HarnessRun, Mapping[str, Any]], bool],
    ) -> None:
        self.harness = harness
        self.target = target
        expected_environment_fingerprint = stable_fingerprint(target.model_dump(mode="json"))
        if harness.environment_fingerprint != expected_environment_fingerprint:
            raise ValueError("Stage 0 harness stable identity does not match its bound target")
        self.measurement_plan_factory = measurement_plan_factory
        self.known_signal_detector = known_signal_detector
        self.null_signal_detector = null_signal_detector
        self.target_fingerprint = target_fingerprint(target)
        self.provenance = AdapterProvenance(
            profile=harness.provenance.profile,
            capability="stage0_probe",
            adapter_name="Stage0MeasurementProbeAdapter",
            adapter_version="2",
            implementation_kind="real",
        )

    def run_probe(self, payload: Mapping[str, Any], output_dir: Path) -> Stage0ProbeOutput:
        probe_type = Stage0ProbeType(payload["probe_type"])
        if payload.get("mode") == "formal":
            return self._formal_probe(probe_type, payload, output_dir)
        if probe_type is Stage0ProbeType.FINGERPRINT:
            return self._fingerprint_probe(payload, output_dir)
        run_plan = dict(self.measurement_plan_factory(probe_type, payload))
        run_plan["mode"] = payload.get("mode")
        run_plan["_job_context"] = payload.get("_job_context", {})
        run = self.harness.run_with_evidence(run_plan, output_dir)
        if probe_type is Stage0ProbeType.TIMER:
            assert run.evidence.calibration is not None
            if run.evidence.calibration.timer_resolution_ns is None:
                raise MeasurementSafetyError("timer evidence has no measured resolution")
            summary: dict[str, Any] = {
                "timer_resolution_ns": run.evidence.calibration.timer_resolution_ns,
                "device_to_host_clock_ratio": run.evidence.calibration.ns_per_tick,
                "calibration_point_count": run.evidence.calibration.point_count,
                "max_calibration_residual_ns": run.evidence.calibration.max_residual_ns,
            }
        elif probe_type is Stage0ProbeType.NOISE:
            summary = {
                "sample_count": run.series.sample_count,
                "statistics_input_uri": run.artifact.uri,
                "statistics_input_hash": run.artifact.sha256,
                "measurement_id": str(run.series.measurement_id),
            }
        elif probe_type is Stage0ProbeType.KNOWN_SIGNAL:
            summary = {"detected": self.known_signal_detector(run, payload)}
        elif probe_type is Stage0ProbeType.NULL_SIGNAL:
            summary = {"false_positive": self.null_signal_detector(run, payload)}
        else:
            raise ValueError(f"probe type {probe_type.value} is not owned by S0-B")
        return Stage0ProbeOutput(
            summary=summary,
            raw_evidence_uri=run.artifact.uri,
            raw_evidence_hash=run.artifact.sha256,
            cleanup_evidence=run.cleanup_evidence,
            adapter_provenance=(self.provenance,),
        )

    def _formal_probe(
        self,
        probe_type: Stage0ProbeType,
        payload: Mapping[str, Any],
        output_dir: Path,
    ) -> Stage0ProbeOutput:
        if probe_type not in {
            Stage0ProbeType.FINGERPRINT,
            Stage0ProbeType.TIMER,
            Stage0ProbeType.NOISE,
            Stage0ProbeType.KNOWN_SIGNAL,
            Stage0ProbeType.NULL_SIGNAL,
        }:
            raise ValueError(f"probe type {probe_type.value} is not owned by S0-B")
        protocol = load_registered_stage0_protocol(str(payload["protocol_version"]))
        binding = self._formal_binding(probe_type, payload, protocol)
        if probe_type is Stage0ProbeType.FINGERPRINT:
            return self._formal_fingerprint_probe(
                payload,
                output_dir,
                protocol,
                binding,
            )
        plan = self._formal_plan(probe_type, protocol)
        run = self.harness.run_stage0_v2(
            binding=binding,
            plan=plan,
            adapter_provenance=(self._raw_provenance(),),
            output_dir=output_dir,
            job_context=dict(payload.get("_job_context", {})),
        )
        if probe_type is Stage0ProbeType.TIMER:
            summary: dict[str, Any] = {
                "measurement_id": str(binding.measurement_id),
                "timer_resolution_ns": run.evidence.calibration.timer_resolution_ns,
                "calibration_point_count": len(run.evidence.calibration.points),
                "sample_count": len(run.evidence.samples),
            }
        else:
            summary = {
                "measurement_id": str(binding.measurement_id),
                "sample_count": len(run.evidence.samples),
                "segment_order": list(run.evidence.plan.segment_order),
                "verdict_owner": "stage0-d-verifier",
            }
        return Stage0ProbeOutput(
            summary=summary,
            raw_evidence_uri=run.artifact.uri,
            raw_evidence_hash=run.artifact.sha256,
            cleanup_evidence=run.cleanup_evidence,
            adapter_provenance=(self.provenance,),
        )

    def _formal_fingerprint_probe(
        self,
        payload: Mapping[str, Any],
        output_dir: Path,
        protocol: LoadedStage0Protocol,
        binding: Stage0EvidenceBinding,
    ) -> Stage0ProbeOutput:
        context = dict(payload.get("_job_context", {}))
        self.harness.require_formal_context(context)
        cleanup_evidence: dict[str, Any] | None = None
        try:
            before = DynamicObservationV2(
                phase="before_run",
                captured_monotonic_ns=self.harness.clock.now_ns(),
                telemetry=self.harness.collect_formal_telemetry(),
            )
            hardware, software = self._fingerprint_inputs(protocol.protocol.protocol_version)
            summary = {
                "hardware_fingerprint": stable_fingerprint(hardware),
                "software_fingerprint": stable_fingerprint(software),
            }
            after = DynamicObservationV2(
                phase="after_run",
                captured_monotonic_ns=self.harness.clock.now_ns(),
                telemetry=self.harness.collect_formal_telemetry(),
            )
            evidence = FingerprintEvidenceV2(
                binding=binding,
                observations=(before, after),
                adapter_provenance=(self._raw_provenance(),),
                hardware_fingerprint=summary["hardware_fingerprint"],
                software_fingerprint=summary["software_fingerprint"],
            )
            artifact = write_evidence(
                output_dir
                / "stage0"
                / str(binding.stage0_run_id)
                / Stage0ProbeType.FINGERPRINT.value
                / str(binding.measurement_id)
                / "fingerprint-evidence-v2.json",
                evidence,
            )
        finally:
            cleanup_evidence = self.harness.cleanup_formal_context(context)
        return Stage0ProbeOutput(
            summary=summary,
            raw_evidence_uri=artifact.uri,
            raw_evidence_hash=artifact.sha256,
            cleanup_evidence=cleanup_evidence,
            adapter_provenance=(self.provenance,),
        )

    def _formal_binding(
        self,
        probe_type: Stage0ProbeType,
        payload: Mapping[str, Any],
        protocol: LoadedStage0Protocol,
    ) -> Stage0EvidenceBinding:
        context = dict(payload.get("_job_context", {}))
        try:
            if payload["adapter_profile"] != self.provenance.profile:
                raise ValueError("adapter_profile does not match the S0-B producer")
            lease = Stage0LeaseBinding(
                lease_id=UUID(str(context["lease_id"])),
                lease_scope=LeaseScope(str(context["lease_scope"])),
                resource_id=context["resource_id"],
                fencing_token=context["fencing_token"],
            )
            return Stage0EvidenceBinding(
                task_id=UUID(str(payload["task_id"])),
                stage0_run_id=UUID(str(payload["stage0_run_id"])),
                target_snapshot_id=UUID(str(payload["target_snapshot_id"])),
                target_id=self.target.target_id,
                target_fingerprint=self.target_fingerprint,
                environment_fingerprint=self.harness.environment_fingerprint,
                workload_id=payload["workload_id"],
                probe_type=probe_type,
                run_mode=Stage0RunMode(str(payload["mode"])),
                protocol_version=protocol.protocol.protocol_version,
                protocol_hash=protocol.protocol_hash,
                metric_name=protocol.protocol.timing_metric_name,
                unit=protocol.protocol.timing_unit,
                measurement_id=uuid4(),
                lease=lease,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MeasurementSafetyError(
                f"Formal Stage 0 control-plane binding is incomplete: {exc}"
            ) from exc

    @staticmethod
    def _formal_plan(
        probe_type: Stage0ProbeType,
        protocol: LoadedStage0Protocol,
    ) -> MeasurementPlanV2:
        sampling = protocol.protocol.sampling
        if probe_type is Stage0ProbeType.TIMER:
            segment_order = ("timer",)
            samples_per_segment = 1
        elif probe_type is Stage0ProbeType.NOISE:
            segment_order = ("noise",)
            samples_per_segment = sampling.noise_samples_per_restart
        else:
            segment_order = sampling.signal_segment_order
            samples_per_segment = sampling.signal_samples_per_segment
        return MeasurementPlanV2(
            restart_count=sampling.restart_count,
            warmup_count=sampling.warmup_count,
            batch_iterations=sampling.batch_iterations,
            segment_order=segment_order,
            samples_per_segment=samples_per_segment,
        )

    def _raw_provenance(self) -> Stage0AdapterProvenance:
        return Stage0AdapterProvenance.model_validate(self.provenance.model_dump(mode="python"))

    def _fingerprint_probe(self, payload: Mapping[str, Any], output_dir: Path) -> Stage0ProbeOutput:
        hardware, software = self._fingerprint_inputs(str(payload["protocol_version"]))
        summary = {
            "hardware_fingerprint": stable_fingerprint(hardware),
            "software_fingerprint": stable_fingerprint(software),
        }
        stage0_run_id = UUID(str(payload["stage0_run_id"]))
        artifact = write_evidence(
            output_dir / "stage0" / str(stage0_run_id) / "fingerprint.json",
            {"summary": summary},
        )
        return Stage0ProbeOutput(
            summary=summary,
            raw_evidence_uri=artifact.uri,
            raw_evidence_hash=artifact.sha256,
            cleanup_evidence=None,
            adapter_provenance=(self.provenance,),
        )

    def _fingerprint_inputs(
        self,
        protocol_version: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        hardware = {
            "execution_host": self.target.execution_host.name,
            "address": self.target.execution_host.address,
            "accelerator": self.target.execution_host.accelerator.model_dump(),
            "host_environment": self.target.execution_host.observed_host_environment.model_dump(),
        }
        software = {
            "inference_image": self.target.inference_image.model_dump(),
            "source_baseline": self.target.source_baseline.model_dump(),
            "protocol_version": protocol_version,
        }
        return hardware, software
