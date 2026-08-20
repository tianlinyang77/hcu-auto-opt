from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.adapters.interfaces import ResourceCleaner
from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.stage0 import Stage0ProbeOutput
from hcuopt.runtime_probes.evidence import (
    EvidencePublisher,
    LocalContentAddressedEvidencePublisher,
)
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.runtime_probes.profiler import ProfilerCapabilityProbe
from hcuopt.targets import target_fingerprint


class RuntimeProbeAdapter:
    """S0-C adapter for the profiler and reversible-overlay probe jobs."""

    supported_probe_types = frozenset(
        {Stage0ProbeType.PROFILER, Stage0ProbeType.HOTPATCH}
    )

    def __init__(
        self,
        profiler: ProfilerCapabilityProbe,
        overlay: OverlayCapabilityProbe,
        cleaner: ResourceCleaner,
        target: TargetSpec,
        configuration: RuntimeProbeProfile,
        evidence_publisher: EvidencePublisher | None = None,
    ) -> None:
        expected_fingerprint = target_fingerprint(target)
        if configuration.profile != configuration.profile.strip():
            raise ValueError("runtime probe profile name cannot contain surrounding whitespace")
        if configuration.target_id != target.target_id:
            raise ValueError("runtime probe profile is bound to a different target")
        if configuration.target_fingerprint != expected_fingerprint:
            raise ValueError("runtime probe profile target fingerprint does not match Target Lock")
        self.profiler = profiler
        self.overlay = overlay
        self.cleaner = cleaner
        self._configuration = RuntimeProbeProfile.model_validate_json(
            configuration.model_dump_json()
        )
        self.evidence_publisher = (
            evidence_publisher or LocalContentAddressedEvidencePublisher()
        )
        self.target_fingerprint = expected_fingerprint
        self.provenance = AdapterProvenance(
            profile=configuration.profile,
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
        stage0_run_id = str(UUID(str(payload.get("stage0_run_id"))))
        target_snapshot_id = str(UUID(str(payload.get("target_snapshot_id"))))
        protocol_version = payload.get("protocol_version")
        if not isinstance(protocol_version, str) or not protocol_version:
            raise ValueError("runtime probe requires a protocol_version")
        mode = payload.get("mode")
        if mode not in {"dry_run", "formal"}:
            raise ValueError("runtime probe mode must be dry_run or formal")
        context = self._mapping(payload.get("_job_context", {}), "job context")
        target = TargetSpec.model_validate(payload.get("target"))
        if (
            target_fingerprint(target) != self.target_fingerprint
            or payload.get("target_fingerprint") != self.target_fingerprint
        ):
            raise ValueError("runtime probe payload target does not match the frozen profile")
        configuration = (
            self._configuration.profiler
            if probe_type is Stage0ProbeType.PROFILER
            else self._configuration.hotpatch
        ).model_dump(mode="json")
        formal = mode == "formal"
        if formal and not self.evidence_publisher.authorizes_formal_results:
            raise ValueError(
                "formal runtime probe requires a verifier-owned evidence publisher"
            )
        resource_id = context.get("resource_id")
        fencing_token = context.get("fencing_token")
        valid_fencing_token = (
            isinstance(fencing_token, int)
            and not isinstance(fencing_token, bool)
            and fencing_token >= 1
        )
        if formal and (
            not isinstance(context.get("lease_id"), str)
            or not isinstance(resource_id, str)
            or not resource_id
            or not valid_fencing_token
        ):
            raise ValueError("formal runtime probe requires lease, resource, and fencing token")

        if probe_type is Stage0ProbeType.PROFILER:
            details = self.profiler.run(
                configuration,
                target=target,
                output_dir=output_dir,
                resource_id=resource_id if isinstance(resource_id, str) else None,
                fencing_token=fencing_token if valid_fencing_token else None,
            )
            summary = {
                "capability": details["capability"],
                "selected_tool": details["selected_tool"],
                "observed_fields": details["observed_fields"],
                "missing_fields": details["missing_fields"],
            }
        else:
            if not isinstance(resource_id, str) or not resource_id:
                raise ValueError("hotpatch probe requires a leased resource_id")
            if not valid_fencing_token:
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
                    "baseline_proved",
                    "activation_proved",
                    "correctness_passed",
                    "recovery_passed",
                    "resource_healthy",
                    "observation_contract_matches",
                    "baseline_source_hash_before",
                    "baseline_source_hash_after",
                    "candidate_source_hash",
                    "artifact_hash",
                    "workload_kind",
                    "replacement_point",
                    "generic_artifact_mount_passed",
                    "sglang_overlay_proved",
                )
            }

        cleanup_evidence = None
        if formal:
            assert isinstance(resource_id, str)
            assert valid_fencing_token
            cleanup_evidence = {
                "fence": dict(self.cleaner.fence(resource_id, fencing_token)),
                "health": dict(self.cleaner.health_check(resource_id)),
            }
            if cleanup_evidence["health"].get("healthy") is not True:
                raise ValueError("formal runtime probe cleanup health check failed")

        evidence_payload = {
            "stage0_run_id": stage0_run_id,
            "target_snapshot_id": target_snapshot_id,
            "target_fingerprint": str(payload["target_fingerprint"]),
            "probe_type": probe_type.value,
            "protocol_version": protocol_version,
            "adapter_provenance": self.provenance.model_dump(mode="json"),
            "execution_context": {
                "lease_id": context.get("lease_id"),
                "resource_id": resource_id,
                "fencing_token": fencing_token,
            },
            "publication_authority": self.evidence_publisher.publication_authority,
            "summary": summary,
            "details": details,
            "cleanup_evidence": cleanup_evidence,
        }
        evidence = self.evidence_publisher.publish(
            output_dir,
            stage0_run_id=stage0_run_id,
            probe_type=probe_type.value,
            payload=evidence_payload,
        )
        if evidence.publication_authority != self.evidence_publisher.publication_authority:
            raise ValueError("evidence publisher returned inconsistent authority")
        return Stage0ProbeOutput(
            raw_evidence_uri=evidence.uri,
            raw_evidence_hash=evidence.sha256,
            summary=summary,
            cleanup_evidence=cleanup_evidence,
            synthetic=False,
        )

    @staticmethod
    def _mapping(value: Any, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object")
        return value
