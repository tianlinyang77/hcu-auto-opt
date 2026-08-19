from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.stage0 import Stage0ProbeOutput
from hcuopt.runtime_probes.evidence import write_immutable_json
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profiler import ProfilerCapabilityProbe


class RuntimeProbeAdapter:
    """S0-C adapter for the profiler and reversible-overlay probe jobs."""

    supported_probe_types = frozenset(
        {Stage0ProbeType.PROFILER, Stage0ProbeType.HOTPATCH}
    )

    def __init__(
        self,
        profiler: ProfilerCapabilityProbe,
        overlay: OverlayCapabilityProbe,
        *,
        profile: str = "nmz36-stage0-v1",
    ) -> None:
        self.profiler = profiler
        self.overlay = overlay
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="stage0_probe",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def run_probe(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> Stage0ProbeOutput:
        probe_type = Stage0ProbeType(str(payload.get("probe_type")))
        if probe_type not in self.supported_probe_types:
            raise ValueError(f"S0-C adapter does not implement probe type {probe_type.value}")
        configuration = self._configuration(payload, probe_type)
        context = self._mapping(payload.get("_job_context", {}), "job context")
        target = TargetSpec.model_validate(payload.get("target"))

        if probe_type is Stage0ProbeType.PROFILER:
            resource_id = context.get("resource_id")
            fencing_token = context.get("fencing_token")
            details = self.profiler.run(
                configuration,
                target=target,
                output_dir=output_dir,
                resource_id=resource_id if isinstance(resource_id, str) else None,
                fencing_token=fencing_token if isinstance(fencing_token, int) else None,
            )
            summary = {
                "capability": details["capability"],
                "selected_tool": details["selected_tool"],
                "observed_fields": details["observed_fields"],
                "missing_fields": details["missing_fields"],
            }
        else:
            resource_id = context.get("resource_id")
            fencing_token = context.get("fencing_token")
            if not isinstance(resource_id, str) or not resource_id:
                raise ValueError("hotpatch probe requires a leased resource_id")
            if not isinstance(fencing_token, int):
                raise ValueError("hotpatch probe requires a fencing_token")
            details = self.overlay.run(
                configuration,
                target=target,
                output_dir=output_dir,
                resource_id=resource_id,
                fencing_token=fencing_token,
            )
            summary = {
                name: details[name]
                for name in (
                    "capability",
                    "activation_mode",
                    "execution_succeeded",
                    "activation_proved",
                    "correctness_passed",
                    "recovery_passed",
                    "resource_healthy",
                    "baseline_source_hash_before",
                    "baseline_source_hash_after",
                    "candidate_source_hash",
                    "artifact_hash",
                )
            }

        evidence_payload = {
            "stage0_run_id": str(payload["stage0_run_id"]),
            "target_snapshot_id": str(payload["target_snapshot_id"]),
            "target_fingerprint": str(payload["target_fingerprint"]),
            "probe_type": probe_type.value,
            "protocol_version": str(payload["protocol_version"]),
            "adapter_provenance": self.provenance.model_dump(mode="json"),
            "summary": summary,
            "details": details,
        }
        evidence_uri, evidence_hash = write_immutable_json(
            output_dir,
            stage0_run_id=str(payload["stage0_run_id"]),
            probe_type=probe_type.value,
            payload=evidence_payload,
        )
        return Stage0ProbeOutput(
            raw_evidence_uri=evidence_uri,
            raw_evidence_hash=evidence_hash,
            summary=summary,
            cleanup_evidence=None,
            synthetic=False,
        )

    @classmethod
    def _configuration(
        cls, payload: Mapping[str, Any], probe_type: Stage0ProbeType
    ) -> Mapping[str, Any]:
        root = cls._mapping(payload.get("runtime_probe"), "runtime_probe")
        return cls._mapping(root.get(probe_type.value), probe_type.value)

    @staticmethod
    def _mapping(value: Any, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object")
        return value
