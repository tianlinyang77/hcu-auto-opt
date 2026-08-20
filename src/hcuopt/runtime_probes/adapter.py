from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.adapters.interfaces import ResourceCleaner
from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.contracts.v1 import Stage0Budget
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
        if mode == "formal":
            raise ValueError(
                "formal runtime probes are disabled until D-side evidence "
                "verification is wired"
            )
        context = self._mapping(payload.get("_job_context", {}), "job context")
        budget = Stage0Budget.model_validate(payload.get("budget", {}))
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
        lease_id = context.get("lease_id")
        raw_resource_id = context.get("resource_id")
        fencing_token = context.get("fencing_token")
        resource_id = (
            raw_resource_id
            if isinstance(raw_resource_id, str) and raw_resource_id
            else None
        )
        valid_fencing_token = (
            isinstance(fencing_token, int)
            and not isinstance(fencing_token, bool)
            and fencing_token >= 1
        )
        lease_supplied = any(
            value is not None for value in (lease_id, raw_resource_id, fencing_token)
        )
        if lease_supplied and (
            not isinstance(lease_id, str)
            or not lease_id
            or resource_id is None
            or not valid_fencing_token
        ):
            raise ValueError(
                "leased runtime probe requires lease, resource, and fencing token"
            )
        if probe_type is Stage0ProbeType.HOTPATCH and not lease_supplied:
            raise ValueError("hotpatch probe requires an exclusive lease")

        cleanup_evidence = None
        try:
            if probe_type is Stage0ProbeType.PROFILER:
                details = self.profiler.run(
                    configuration,
                    target=target,
                    output_dir=output_dir,
                    resource_id=resource_id,
                    fencing_token=fencing_token if valid_fencing_token else None,
                    max_wall_seconds=budget.max_wall_seconds,
                )
                summary = {
                    "capability": details["capability"],
                    "selected_tool": details["selected_tool"],
                    "observed_fields": details["observed_fields"],
                    "missing_fields": details["missing_fields"],
                }
            else:
                assert resource_id is not None
                assert valid_fencing_token
                details = self.overlay.run(
                    configuration,
                    target=target,
                    output_dir=output_dir,
                    resource_id=resource_id,
                    fencing_token=fencing_token,
                    max_wall_seconds=budget.max_wall_seconds,
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
        except BaseException as probe_error:
            if lease_supplied:
                assert resource_id is not None
                assert valid_fencing_token
                try:
                    self._cleanup_resource(resource_id, fencing_token)
                except Exception as cleanup_error:
                    raise cleanup_error from probe_error
            raise

        if lease_supplied:
            assert resource_id is not None
            assert valid_fencing_token
            cleanup_evidence = self._cleanup_resource(resource_id, fencing_token)

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
            "budget": budget.model_dump(mode="json", exclude_none=True),
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
            adapter_provenance=(self.provenance,),
        )

    def _cleanup_resource(
        self, resource_id: str, fencing_token: int
    ) -> dict[str, dict[str, Any]]:
        fence_error: Exception | None = None
        health_error: Exception | None = None
        try:
            fence = dict(self.cleaner.fence(resource_id, fencing_token))
        except Exception as error:
            fence_error = error
            fence = {
                "resource_id": resource_id,
                "fenced": False,
                "error": f"{error.__class__.__name__}: {error}",
            }
        try:
            health = dict(self.cleaner.health_check(resource_id))
        except Exception as error:
            health_error = error
            health = {
                "resource_id": resource_id,
                "healthy": False,
                "error": f"{error.__class__.__name__}: {error}",
            }
        cleanup_evidence = {"fence": fence, "health": health}
        failures = []
        if fence_error is not None or fence.get("fenced") is not True:
            failures.append("fence")
        if health_error is not None or health.get("healthy") is not True:
            failures.append("health")
        if failures:
            raise ValueError(
                "runtime probe cleanup failed: " + ", ".join(failures)
            )
        return cleanup_evidence

    @staticmethod
    def _mapping(value: Any, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object")
        return value
