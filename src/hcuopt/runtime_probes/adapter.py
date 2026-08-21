from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hcuopt.adapters.interfaces import ResourceCleaner
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.contracts.v1 import Stage0Budget
from hcuopt.domain.enums import LeaseScope, Stage0ProbeType, Stage0RunMode
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_verifier import (
    HotpatchEvidenceV2,
    HotpatchExecutionObservationV2,
    HotpatchPhaseManifestV2,
    HotpatchStateEntryV2,
    OverlayMountEvidenceV2,
    ProfilerEvidenceV2,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.harness import TelemetryCollector
from hcuopt.measurement.models import (
    DynamicObservationV2,
    RawEvidenceFileV2,
    Stage0AdapterProvenance,
    Stage0EvidenceBinding,
    Stage0LeaseBinding,
    TelemetrySnapshotV2,
)
from hcuopt.measurement.stage0 import Stage0ProbeOutput
from hcuopt.measurement.timers import HostClock, HostMonotonicClock
from hcuopt.runtime_probes.evidence import (
    EvidencePublisher,
    LocalContentAddressedEvidencePublisher,
)
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.runtime_probes.profiler import ProfilerCapabilityProbe
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import target_fingerprint


class RuntimeProbeAdapter:
    """S0-C adapter for the profiler and reversible-overlay probe jobs."""

    supported_probe_types = frozenset({Stage0ProbeType.PROFILER, Stage0ProbeType.HOTPATCH})

    def __init__(
        self,
        profiler: ProfilerCapabilityProbe,
        overlay: OverlayCapabilityProbe,
        cleaner: ResourceCleaner,
        target: TargetSpec,
        configuration: RuntimeProbeProfile,
        evidence_publisher: EvidencePublisher | None = None,
        telemetry: TelemetryCollector | None = None,
        clock: HostClock | None = None,
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
        self.evidence_publisher = evidence_publisher or LocalContentAddressedEvidencePublisher()
        self.telemetry = telemetry
        self.clock = clock or HostMonotonicClock()
        self.target_fingerprint = expected_fingerprint
        self.provenance = AdapterProvenance(
            profile=configuration.profile,
            capability="stage0_probe",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def run_probe(self, payload: Mapping[str, Any], output_dir: Path) -> Stage0ProbeOutput:
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
        formal = mode == "formal"
        if formal and not self.evidence_publisher.authorizes_formal_results:
            raise ValueError(
                "formal runtime probes require a deployment-authorized evidence publisher"
            )
        if formal and self.telemetry is None:
            raise ValueError("formal runtime probes require typed deployment telemetry")
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
            raw_resource_id if isinstance(raw_resource_id, str) and raw_resource_id else None
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
            raise ValueError("leased runtime probe requires lease, resource, and fencing token")
        if probe_type is Stage0ProbeType.HOTPATCH and not lease_supplied:
            raise ValueError("hotpatch probe requires an exclusive lease")
        binding = self._formal_binding(probe_type, payload, context) if formal else None
        if formal and probe_type is Stage0ProbeType.HOTPATCH:
            assert binding is not None
            configuration = self._prepare_formal_hotpatch_configuration(
                output_dir,
                binding,
                configuration,
            )

        cleanup_evidence = None
        before_observation = self._formal_observation("before_run") if formal else None
        formal_inputs: dict[str, Any] | None = None
        try:
            if probe_type is Stage0ProbeType.PROFILER:
                details = self.profiler.run(
                    configuration,
                    target=target,
                    output_dir=output_dir,
                    resource_id=resource_id,
                    fencing_token=fencing_token if valid_fencing_token else None,
                    max_wall_seconds=budget.max_wall_seconds,
                    require_formal_raw=formal,
                )
                formal_inputs = details.pop("_formal_evidence")
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
                formal_inputs = details.pop("_formal_evidence", None)
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
            after_observation = self._formal_observation("after_run") if formal else None
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

        if formal:
            assert binding is not None
            assert before_observation is not None
            assert after_observation is not None
            evidence = self._publish_formal_evidence(
                output_dir,
                probe_type=probe_type,
                binding=binding,
                observations=(before_observation, after_observation),
                formal_inputs=formal_inputs,
            )
            return Stage0ProbeOutput(
                raw_evidence_uri=evidence.uri,
                raw_evidence_hash=evidence.sha256,
                summary=summary,
                cleanup_evidence=cleanup_evidence,
                synthetic=False,
                adapter_provenance=(self.provenance,),
            )

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

    def _formal_binding(
        self,
        probe_type: Stage0ProbeType,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Stage0EvidenceBinding:
        try:
            if payload["adapter_profile"] != self.provenance.profile:
                raise ValueError("adapter_profile does not match the S0-C producer")
            protocol = load_registered_stage0_protocol(str(payload["protocol_version"]))
            lease = Stage0LeaseBinding(
                lease_id=UUID(str(context["lease_id"])),
                lease_scope=LeaseScope(str(context["lease_scope"])),
                resource_id=str(context["resource_id"]),
                fencing_token=context["fencing_token"],
            )
            return Stage0EvidenceBinding(
                task_id=UUID(str(payload["task_id"])),
                stage0_run_id=UUID(str(payload["stage0_run_id"])),
                target_snapshot_id=UUID(str(payload["target_snapshot_id"])),
                target_id=self._configuration.target_id,
                target_fingerprint=self.target_fingerprint,
                environment_fingerprint=stable_fingerprint(
                    TargetSpec.model_validate(payload["target"]).model_dump(mode="json")
                ),
                workload_id=str(payload["workload_id"]),
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
            raise ValueError(f"Formal Stage 0 runtime binding is incomplete: {exc}") from exc

    def _formal_observation(self, phase: str) -> DynamicObservationV2:
        assert self.telemetry is not None
        try:
            snapshot = TelemetrySnapshotV2.model_validate(dict(self.telemetry.collect()))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Formal runtime telemetry is invalid: {exc}") from exc
        return DynamicObservationV2(
            phase=phase,
            captured_monotonic_ns=self.clock.now_ns(),
            telemetry=snapshot,
        )

    def _publish_formal_evidence(
        self,
        output_dir: Path,
        *,
        probe_type: Stage0ProbeType,
        binding: Stage0EvidenceBinding,
        observations: tuple[DynamicObservationV2, DynamicObservationV2],
        formal_inputs: dict[str, Any] | None,
    ) -> Any:
        if probe_type is Stage0ProbeType.HOTPATCH:
            return self._publish_formal_hotpatch(
                output_dir,
                binding=binding,
                observations=observations,
                formal_inputs=formal_inputs,
            )
        if formal_inputs is None:
            raise ValueError("selected profiler is not a D-supported raw tool/parser combination")
        version = self._publish_raw(
            output_dir,
            binding,
            "profiler-version.txt",
            formal_inputs["tool_version_output"],
        )
        raw_name = (
            "rocprof-output.csv"
            if formal_inputs["parser_version"] == "rocprof-csv-v1"
            else "torch-trace.json"
        )
        raw_output = self._publish_raw(
            output_dir,
            binding,
            raw_name,
            formal_inputs["raw_output"],
        )
        envelope = ProfilerEvidenceV2(
            binding=binding,
            observations=observations,
            adapter_provenance=(self._raw_provenance(),),
            tool_name=formal_inputs["tool_name"],
            parser_version=formal_inputs["parser_version"],
            tool_version_output=version,
            raw_output=raw_output,
        )
        return self.evidence_publisher.publish(
            output_dir,
            stage0_run_id=str(binding.stage0_run_id),
            probe_type=probe_type.value,
            payload=envelope.model_dump(mode="json"),
        )

    def _prepare_formal_hotpatch_configuration(
        self,
        output_dir: Path,
        binding: Stage0EvidenceBinding,
        configuration: dict[str, Any],
    ) -> dict[str, Any]:
        artifact = ArtifactManifest.model_validate(configuration["artifact"])
        artifact_bytes = self._read_regular_uri(artifact.uri, "hotpatch Artifact")
        artifact_reference = self._publish_raw(
            output_dir,
            binding,
            "candidate-artifact.bin",
            artifact_bytes,
        )
        if artifact_reference.sha256 != artifact.content_hash:
            raise ValueError("hotpatch Artifact bytes do not match its manifest")
        published_artifact = artifact.model_copy(update={"uri": artifact_reference.uri})
        prepared = dict(configuration)
        prepared["artifact"] = published_artifact.model_dump(mode="json")
        candidate = dict(prepared["candidate"])
        candidate["implementation_source_uri"] = artifact_reference.uri
        prepared["candidate"] = candidate
        return prepared

    def _publish_formal_hotpatch(
        self,
        output_dir: Path,
        *,
        binding: Stage0EvidenceBinding,
        observations: tuple[DynamicObservationV2, DynamicObservationV2],
        formal_inputs: dict[str, Any] | None,
    ) -> Any:
        if formal_inputs is None:
            raise ValueError("formal hotpatch execution evidence is missing")
        configuration = formal_inputs["configuration"]
        if configuration.workload_kind != "sglang_python_triton":
            raise ValueError("Formal hotpatch requires the registered SGLang workload")
        phases = (configuration.baseline, configuration.candidate, configuration.recovery)
        if any(
            phase.evidence_directory_uri is None or phase.implementation_source_uri is None
            for phase in phases
        ):
            raise ValueError(
                "Formal hotpatch requires phase evidence directories and implementation sources"
            )

        baseline_source = self._publish_contract(
            output_dir,
            binding,
            "source-baseline.json",
            configuration.baseline_source,
        )
        candidate_source = self._publish_contract(
            output_dir,
            binding,
            "source-candidate.json",
            configuration.candidate_source,
        )
        artifact = configuration.artifact
        artifact_bytes = self._read_regular_uri(artifact.uri, "published hotpatch Artifact")
        artifact_reference = self._publish_raw(
            output_dir,
            binding,
            "candidate-artifact.bin",
            artifact_bytes,
        )
        if (
            artifact_reference.uri != artifact.uri
            or artifact_reference.sha256 != artifact.content_hash
        ):
            raise ValueError("Formal hotpatch did not execute the published Artifact")
        artifact_manifest = self._publish_contract(
            output_dir,
            binding,
            "artifact-manifest.json",
            artifact,
        )

        phase_references: dict[str, RawEvidenceFileV2] = {}
        for ordinal, phase_name in enumerate(("baseline", "candidate", "recovery")):
            phase_configuration = getattr(configuration, phase_name)
            phase_manifest = self._formal_phase_manifest(
                output_dir,
                binding,
                phase_name=phase_name,
                restart_ordinal=ordinal,
                source_snapshot=(
                    configuration.candidate_source
                    if phase_name == "candidate"
                    else configuration.baseline_source
                ),
                phase_configuration=phase_configuration,
                request=formal_inputs["requests"][phase_name],
                result=formal_inputs["results"][phase_name],
                observation=formal_inputs["observations"][phase_name],
            )
            phase_references[phase_name] = self._publish_contract(
                output_dir,
                binding,
                f"hotpatch-phase-{phase_name}.json",
                phase_manifest,
            )

        candidate_result = formal_inputs["results"]["candidate"]
        container_id = candidate_result.metadata.get("container_name")
        if not isinstance(container_id, str) or not container_id:
            raise ValueError("candidate execution has no real container identity")
        envelope = HotpatchEvidenceV2(
            binding=binding,
            observations=observations,
            adapter_provenance=(self._raw_provenance(),),
            activation_mode="startup_overlay",
            baseline_source=baseline_source,
            candidate_source=candidate_source,
            artifact_manifest=artifact_manifest,
            artifact=artifact_reference,
            baseline_state=phase_references["baseline"],
            candidate_state=phase_references["candidate"],
            recovery_state=phase_references["recovery"],
            overlay_mount=OverlayMountEvidenceV2(
                source_uri=artifact_reference.uri,
                source_hash=artifact_reference.sha256,
                target_path=configuration.overlay_mount_target,
                read_only=True,
                container_id=container_id,
            ),
        )
        return self.evidence_publisher.publish(
            output_dir,
            stage0_run_id=str(binding.stage0_run_id),
            probe_type=binding.probe_type.value,
            payload=envelope.model_dump(mode="json"),
        )

    def _formal_phase_manifest(
        self,
        output_dir: Path,
        binding: Stage0EvidenceBinding,
        *,
        phase_name: str,
        restart_ordinal: int,
        source_snapshot: SourceSnapshot,
        phase_configuration: Any,
        request: Any,
        result: Any,
        observation: dict[str, Any],
    ) -> HotpatchPhaseManifestV2:
        assert phase_configuration.evidence_directory_uri is not None
        assert phase_configuration.implementation_source_uri is not None
        evidence_dir = file_uri_to_path(phase_configuration.evidence_directory_uri).resolve(
            strict=True
        )
        if evidence_dir.is_symlink() or not evidence_dir.is_dir():
            raise ValueError(f"{phase_name} evidence directory is not a real directory")

        output = self._publish_raw(
            output_dir,
            binding,
            f"{phase_name}-stdout.json",
            self._read_regular_uri(result.stdout_uri, f"{phase_name} stdout"),
        )
        normalized_output = self._publish_raw(
            output_dir,
            binding,
            f"{phase_name}-normalized-output.json",
            self._read_regular_path(
                evidence_dir / "normalized-output.json",
                f"{phase_name} normalized output",
            ),
        )
        cache_namespace = self._publish_raw(
            output_dir,
            binding,
            f"{phase_name}-cache-namespace.json",
            self._read_regular_path(
                evidence_dir / "cache-namespace.json",
                f"{phase_name} cache namespace",
            ),
        )
        process_start = self._publish_raw(
            output_dir,
            binding,
            f"{phase_name}-process-start.json",
            self._read_regular_path(
                evidence_dir / "process-start.json",
                f"{phase_name} process start",
            ),
        )
        process_exit = self._publish_raw(
            output_dir,
            binding,
            f"{phase_name}-process-exit.json",
            self._read_regular_path(
                evidence_dir / "process-exit.json",
                f"{phase_name} process exit",
            ),
        )
        implementation = self._publish_raw(
            output_dir,
            binding,
            f"{phase_name}-implementation.bin",
            self._read_regular_uri(
                phase_configuration.implementation_source_uri,
                f"{phase_name} implementation",
            ),
        )
        parsed_observation = HotpatchExecutionObservationV2.model_validate(observation)
        start_payload = self._read_canonical_json(process_start)
        process_id = start_payload.get("process_id")
        proc_stat_line = start_payload.get("proc_stat_line")
        if isinstance(process_id, bool) or not isinstance(process_id, int):
            raise ValueError(f"{phase_name} process start has no process_id")
        if not isinstance(proc_stat_line, str):
            raise ValueError(f"{phase_name} process start has no procfs stat line")
        if process_id != parsed_observation.process_id:
            raise ValueError(f"{phase_name} stdout and lifecycle process differ")
        process_start_token = self._proc_start_token(proc_stat_line, process_id)
        container_id = result.metadata.get("container_name")
        if not isinstance(container_id, str) or not container_id:
            raise ValueError(f"{phase_name} execution has no container identity")
        bound_result = result.model_copy(update={"stdout_uri": output.uri})
        return HotpatchPhaseManifestV2(
            phase=phase_name,
            source_snapshot_id=source_snapshot.snapshot_id,
            execution_request=request,
            execution_result=bound_result,
            process_id=process_id,
            process_start_token=process_start_token,
            process_start_record=process_start,
            process_exit_record=process_exit,
            container_id=container_id,
            entries=(
                HotpatchStateEntryV2(
                    path="implementation/loaded-artifact.bin",
                    content=implementation,
                ),
            ),
            output=output,
            normalized_output=normalized_output,
            cache_namespace=cache_namespace,
        )

    def _publish_contract(
        self,
        output_dir: Path,
        binding: Stage0EvidenceBinding,
        artifact_name: str,
        value: Any,
    ) -> RawEvidenceFileV2:
        return self._publish_raw(
            output_dir,
            binding,
            artifact_name,
            canonical_json_bytes(value),
        )

    @staticmethod
    def _read_regular_uri(uri: str | None, label: str) -> bytes:
        if not isinstance(uri, str) or not uri.startswith("file:"):
            raise ValueError(f"{label} must use a file URI")
        return RuntimeProbeAdapter._read_regular_path(file_uri_to_path(uri), label)

    @staticmethod
    def _read_regular_path(path: Path, label: str) -> bytes:
        path = path.resolve(strict=True)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} must be a regular non-symlink file")
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError(f"{label} exceeds the 64 MiB evidence limit")
        return path.read_bytes()

    def _read_canonical_json(self, reference: RawEvidenceFileV2) -> dict[str, Any]:
        import json

        encoded = self._read_regular_uri(reference.uri, "published JSON evidence")
        if self._sha256_bytes(encoded) != reference.sha256:
            raise ValueError("published JSON evidence hash changed")
        value = json.loads(encoded.decode("utf-8", errors="strict"))
        if not isinstance(value, dict) or canonical_json_bytes(value) != encoded:
            raise ValueError("published lifecycle evidence is not canonical JSON")
        return value

    @staticmethod
    def _sha256_bytes(encoded: bytes) -> str:
        import hashlib

        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _proc_start_token(proc_stat_line: str, expected_pid: int) -> str:
        match = re.fullmatch(r"([1-9][0-9]*) \((.*)\) ([A-Za-z]) (.*)", proc_stat_line)
        if match is None or int(match.group(1)) != expected_pid:
            raise ValueError("lifecycle procfs stat does not identify the process")
        remaining = match.group(4).split()
        if len(remaining) < 19:
            raise ValueError("lifecycle procfs stat does not contain starttime")
        try:
            start_ticks = int(remaining[18])
        except ValueError as exc:
            raise ValueError("lifecycle procfs starttime is invalid") from exc
        if start_ticks < 1:
            raise ValueError("lifecycle procfs starttime must be positive")
        return f"linux-proc-startticks:{start_ticks}"

    def _publish_raw(
        self,
        output_dir: Path,
        binding: Stage0EvidenceBinding,
        artifact_name: str,
        encoded: bytes,
    ) -> RawEvidenceFileV2:
        published = self.evidence_publisher.publish_bytes(
            output_dir,
            stage0_run_id=str(binding.stage0_run_id),
            probe_type=binding.probe_type.value,
            artifact_name=artifact_name,
            encoded=encoded,
        )
        if published.publication_authority != self.evidence_publisher.publication_authority:
            raise ValueError("evidence publisher returned inconsistent authority")
        return RawEvidenceFileV2(uri=published.uri, sha256=published.sha256)

    def _raw_provenance(self) -> Stage0AdapterProvenance:
        return Stage0AdapterProvenance.model_validate(self.provenance.model_dump(mode="python"))

    def _cleanup_resource(self, resource_id: str, fencing_token: int) -> dict[str, dict[str, Any]]:
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
            raise ValueError("runtime probe cleanup failed: " + ", ".join(failures))
        return cleanup_evidence

    @staticmethod
    def _mapping(value: Any, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object")
        return value
