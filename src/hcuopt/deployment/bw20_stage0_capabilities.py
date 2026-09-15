# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Job-scoped BW20 C composition using the original profiler/overlay evidence path.

The controller runs on BW20 with host-visible source/output paths. No forwarding
of Windows paths to Docker, host-wide cleanup, automatic signing or clock writes.
"""

from __future__ import annotations

import hashlib
import platform
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.deployment.bw20_capability_execution import BW20CapabilityExecutionAdapter
from hcuopt.deployment.bw20_stage0_adapter import BW20Stage0ProbeAdapter
from hcuopt.deployment.bw20_stage0_harness import PROFILE, BW20HostClock
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.deployment.bw20_stage0_telemetry import BW20TelemetryCollector, BW20TimingCleaner
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence
from hcuopt.measurement.harness import MeasurementSafetyError, _cleanup_is_healthy
from hcuopt.runtime_probes.adapter import RuntimeProbeAdapter
from hcuopt.runtime_probes.evidence import DeploymentContentAddressedEvidencePublisher
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profile import RuntimeProbeProfile
from hcuopt.runtime_probes.profiler import ProfilerCapabilityProbe
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import target_fingerprint


def isolate_outputs(configuration, root):
    """Rebind only deployment-declared writable evidence directories per job."""
    root = Path(root).absolute()
    if root.resolve(strict=False) != root or root.exists():
        raise ValueError("C job output must be a fresh nonredirected directory")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    value = configuration.model_dump(mode="json")
    for phase in ("baseline", "candidate", "recovery"):
        section = value["hotpatch"][phase]
        original = file_uri_to_path(section["evidence_directory_uri"])
        writable = [m for m in section["mounts"] if not m["read_only"]]
        if len(writable) != 1 or writable[0]["source"] != OverlayCapabilityProbe._mount_source(
            original
        ):
            raise ValueError("C phase requires exactly its declared output mount")
        dest = root / phase
        dest.mkdir(parents=True, exist_ok=False)
        writable[0]["source"] = OverlayCapabilityProbe._mount_source(dest)
        section["evidence_directory_uri"] = dest.as_uri()
    profiler = value["profiler"]
    writable = [m for m in profiler["mounts"] if not m["read_only"]]
    if len(writable) > 1:
        raise ValueError("C profiler supports one declared output mount")
    if writable:
        exports = [
            t["output_host_uri"] for t in profiler["tool_candidates"] if t["output_host_uri"]
        ]
        old = file_uri_to_path(exports[0]).parent
        if writable[0]["source"] != OverlayCapabilityProbe._mount_source(old):
            raise ValueError("C profiler requires exactly its declared output mount")
        dest = root / "profiler"
        dest.mkdir(parents=True, exist_ok=False)
        for tool in profiler["tool_candidates"]:
            if tool["output_host_uri"]:
                rel = file_uri_to_path(tool["output_host_uri"]).relative_to(old)
                if rel.parts != (rel.name,):
                    raise ValueError("C trace export must be directly inside its output directory")
                tool["output_host_uri"] = (dest / rel).as_uri()
        writable[0]["source"] = OverlayCapabilityProbe._mount_source(dest)
    return RuntimeProbeProfile.model_validate(value)


@dataclass
class _Job:
    stopped: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    cleaner: object = None
    executor: object = None
    telemetry: object = None


class BW20CapabilityCleaner(BW20TimingCleaner):
    def health_check(self, resource_id):
        # Overlay checks health between phases. Observe absence without permanently
        # closing the job executor; only fence/cancel prevents the next phase.
        self.fenced = self.factory.confirm_empty()
        return super().health_check(resource_id)


class BW20RuntimeProbeAdapter:
    supported_probe_types = frozenset({"profiler", "hotpatch"})

    def __init__(
        self, *, target, runner, configuration, configuration_sha256, admission, evidence_root,
        input_guard=None,
    ):
        self.target = target.model_copy(deep=True)
        self.configuration = RuntimeProbeProfile.model_validate_json(
            configuration.model_dump_json()
        )
        digest = "sha256:" + hashlib.sha256(canonical_json_bytes(self.configuration)).hexdigest()
        if digest != configuration_sha256:
            raise ValueError("C configuration differs from deployment pin")
        self.target_fingerprint = target_fingerprint(self.target)
        if (
            configuration.target_fingerprint != self.target_fingerprint
            or configuration.profile != PROFILE
        ):
            raise ValueError("C configuration target/profile mismatch")
        self.runner, self.admission = runner, admission
        self.input_guard = input_guard
        self.configuration_sha256 = configuration_sha256
        self.evidence_root = Path(evidence_root).resolve(strict=True)
        self._jobs, self._lock = {}, threading.Lock()
        self.provenance = AdapterProvenance(
            profile=PROFILE,
            capability="stage0_probe",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )

    def run_probe(self, payload, output_dir):
        self.admission.validate(self.target, payload, probe_types=self.supported_probe_types)
        if platform.system() != "Linux" or platform.node() != "github-bw20":
            raise MeasurementSafetyError("C requires a BW20-local Python3.10 controller filesystem")
        output = Path(output_dir).resolve(strict=True)
        if output != self.evidence_root and self.evidence_root not in output.parents:
            raise MeasurementSafetyError("C output is outside deployment evidence root")
        key = BW20Stage0ProbeAdapter._key(payload)
        root = output / "bw20-c-jobs" / key[0] / str(key[2])
        if root.resolve(strict=False) != root or root.exists():
            raise MeasurementSafetyError("C job output is not fresh or is redirected")
        context = dict(payload["_job_context"])
        if (
            not callable(context.get("assert_live_lease"))
            or context["assert_live_lease"]() is not None
        ):
            raise MeasurementSafetyError("C requires original Worker live lease callback")
        if not callable(self.input_guard):
            raise MeasurementSafetyError("C requires deployment-bound prepared input verification")
        input_check = self.input_guard()
        if (not isinstance(input_check, dict)
                or input_check.get("schema_version") != "bw20-runtime-input-recheck-v1"
                or input_check.get("input_integrity_passed") is not True
                or input_check.get("target_fingerprint") != self.target_fingerprint
                or input_check.get("profile_sha256") != self.configuration_sha256):
            raise MeasurementSafetyError("C prepared input verification binding differs")
        # Hashing the model can outlive a lease; recheck before any setup or Docker action.
        if context["assert_live_lease"]() is not None:
            raise MeasurementSafetyError("C live lease invalid after input verification")
        with self._lock:
            if key in self._jobs or len(self._jobs) >= 64:
                raise MeasurementSafetyError("C job duplicate or reconciliation limit reached")
            state = _Job()
            self._jobs[key] = state
        result = None
        try:
            with state.lock:
                if state.stopped.is_set():
                    raise MeasurementSafetyError("C stopped before setup")
                config = isolate_outputs(self.configuration, root)
                executor = BW20CapabilityExecutionAdapter(
                    runner=self.runner,
                    target=self.target,
                    configuration=config,
                    context=context,
                    evidence_root=self.evidence_root,
                )
                state.executor = executor
                telemetry = BW20TelemetryCollector(runner=self.runner, live_bindings=lambda: [])
                state.telemetry = telemetry
                initial = telemetry.collect()["device"]
                cleaner = BW20CapabilityCleaner(
                    factory=executor,
                    telemetry=telemetry,
                    fencing_token=context["fencing_token"],
                    initial_clock_state=dict(
                        mode=initial["performance_level"],
                        sclk_mhz=initial["sclk_mhz"],
                        mclk_mhz=initial["mclk_mhz"],
                    ),
                )
                state.cleaner = cleaner
                runtime = RuntimeProbeAdapter(
                    ProfilerCapabilityProbe(executor=executor),
                    OverlayCapabilityProbe(executor, cleaner),
                    cleaner,
                    self.target,
                    config,
                    DeploymentContentAddressedEvidencePublisher(self.evidence_root),
                    telemetry,
                    BW20HostClock(self.runner, boot_id=telemetry.boot_id),
                )
            if state.stopped.is_set():
                raise MeasurementSafetyError("C stopped during setup")
            result = runtime.run_probe(payload, output)
        finally:
            cleanup = self.cleanup_probe(payload)
            diagnostic = dict(
                schema_version="bw20-c-job-v1",
                job_id=key[0],
                lease_id=key[1],
                resource_id=RESOURCE,
                fencing_token=key[2],
                cleanup_evidence=cleanup,
                input_integrity_check=input_check,
                containers=list(state.executor.transport.owned) if state.executor else [],
                telemetry=state.telemetry.observations if state.telemetry else [],
                performance_conclusion="not_measured",
                automatic_release_allowed=False,
            )
            if result is not None:
                diagnostic["primary_evidence"] = dict(
                    uri=result.raw_evidence_uri, sha256=result.raw_evidence_hash
                )
            receipt = write_evidence(root / "diagnostics.json", diagnostic)
        if not _cleanup_is_healthy(cleanup):
            raise MeasurementSafetyError("C cleanup requires reconciliation")
        return replace(
            result,
            cleanup_evidence={
                **cleanup,
                "diagnostics": {"uri": receipt.uri, "sha256": receipt.sha256},
            },
        )

    def cleanup_probe(self, payload):
        key = BW20Stage0ProbeAdapter._key(payload)
        with self._lock:
            state = self._jobs.get(key)
        if state is None:
            return dict(fence=dict(fenced=False), health=dict(healthy=False, quarantined=True))
        state.stopped.set()
        with state.lock:
            if state.executor is not None:
                state.executor.stopped.set()
            if state.cleaner is None:
                return dict(fence=dict(fenced=False), health=dict(healthy=False, quarantined=True))
            try:
                return dict(
                    fence=state.cleaner.fence(RESOURCE, key[2]),
                    health=state.cleaner.health_check(RESOURCE),
                )
            except Exception:
                return dict(fence=dict(fenced=False), health=dict(healthy=False, quarantined=True))
