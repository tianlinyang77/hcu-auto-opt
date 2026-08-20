from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.evidence import write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.harness import (
    EvidenceMeasurementHarness,
    HarnessRun,
    MeasurementSafetyError,
)
from hcuopt.measurement.models import MeasurementPlan
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
        if probe_type is Stage0ProbeType.FINGERPRINT:
            return self._fingerprint_probe(payload, output_dir)
        run_plan = dict(self.measurement_plan_factory(probe_type, payload))
        run_plan["mode"] = payload.get("mode")
        run_plan["_job_context"] = payload.get("_job_context", {})
        measurement_plan = MeasurementPlan.model_validate(run_plan.get("measurement_plan"))
        if (
            payload.get("mode") == "formal"
            and probe_type is Stage0ProbeType.NOISE
            and measurement_plan.process_restart_count < 1
        ):
            raise MeasurementSafetyError(
                "formal noise measurement requires at least one verified process restart"
            )
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

    def _fingerprint_probe(self, payload: Mapping[str, Any], output_dir: Path) -> Stage0ProbeOutput:
        hardware = {
            "execution_host": self.target.execution_host.name,
            "address": self.target.execution_host.address,
            "accelerator": self.target.execution_host.accelerator.model_dump(),
            "host_environment": self.target.execution_host.observed_host_environment.model_dump(),
        }
        software = {
            "inference_image": self.target.inference_image.model_dump(),
            "source_baseline": self.target.source_baseline.model_dump(),
            "protocol_version": payload["protocol_version"],
        }
        summary = {
            "hardware_fingerprint": stable_fingerprint(hardware),
            "software_fingerprint": stable_fingerprint(software),
        }
        stage0_run_id = UUID(str(payload["stage0_run_id"]))
        artifact = write_evidence(
            output_dir / "stage0" / str(stage0_run_id) / "fingerprint.json",
            {"summary": summary},
        )
        cleanup_evidence = None
        if payload.get("mode") == "formal":
            context = payload.get("_job_context", {})
            cleanup_evidence = self.harness.cleanup_formal_context(context)
        return Stage0ProbeOutput(
            summary=summary,
            raw_evidence_uri=artifact.uri,
            raw_evidence_hash=artifact.sha256,
            cleanup_evidence=cleanup_evidence,
            adapter_provenance=(self.provenance,),
        )
