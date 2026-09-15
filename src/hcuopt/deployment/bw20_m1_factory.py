# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Unregistered BW20 M1 workload and device-timer factories."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from threading import RLock
from uuid import UUID, uuid4

from hcuopt.adapters.profiles import BW20_MANUAL_CANDIDATE_PROFILE
from hcuopt.contracts.platform_v1 import AdapterProvenance, ArtifactManifest, TargetSpec
from hcuopt.deployment.bw20_m1_evidence import BW20M1EvidenceMirror
from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY, BW20M1Policy
from hcuopt.deployment.bw20_m1_runtime import (
    M1_WORKER_PROTOCOL,
    BW20M1DockerTransport,
    build_m1_container_plan,
)
from hcuopt.deployment.bw20_m1_session import (
    BW20M1PairedWorkload,
    BW20M1ProcessSession,
    BW20M1SessionError,
)
from hcuopt.deployment.bw20_m1_staging import stage_m1_run
from hcuopt.deployment.bw20_stage0_guards import guarded_job_session
from hcuopt.deployment.bw20_stage0_runtime import (
    build_timing_plan,
    capture_process_binding,
)
from hcuopt.deployment.bw20_stage0_staging import ControllerBundle, stage_controller
from hcuopt.deployment.bw20_timing_transport import BW20DockerTransport
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.measurement.m1_models import M1Arm
from hcuopt.measurement.nmz36_runtime import DockerTorchEventTimer
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import target_fingerprint


def _trusted_context(payload, policy: BW20M1Policy):
    context = policy.require_job_context(payload, LeaseScope.EXCLUSIVE)
    guard = context.get("assert_live_lease")
    if not callable(guard):
        raise ExecutionSafetyError("BW20 M1 requires a trusted live-lease callback")
    try:
        UUID(str(context["lease_id"]))
        UUID(str(context["job_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionSafetyError("BW20 M1 Job/Lease identity is incomplete") from exc
    return context


class BW20M1WorkloadFactory:
    def __init__(
        self,
        *,
        target: TargetSpec,
        runner,
        bundle: ControllerBundle,
        local_evidence_root: Path,
        harness_provenance: AdapterProvenance,
        baseline_module_hash: str,
        session_budget_seconds: int = 480,
        policy: BW20M1Policy = BW20_M1_POLICY,
    ) -> None:
        policy.validate_target(target)
        if (
            harness_provenance.profile != BW20_MANUAL_CANDIDATE_PROFILE
            or harness_provenance.capability != "measurement_harness"
            or harness_provenance.implementation_kind != "real"
        ):
            raise ValueError("BW20 M1 factory requires its real Harness provenance")
        if not isinstance(bundle, ControllerBundle):
            raise ValueError("BW20 M1 factory requires a frozen Controller Bundle")
        if not baseline_module_hash.startswith("sha256:") or len(baseline_module_hash) != 71:
            raise ValueError("BW20 M1 Baseline module Hash is invalid")
        if type(session_budget_seconds) is not int or not 1 <= session_budget_seconds <= 480:
            raise ValueError("BW20 M1 session budget must be 1..480 seconds")
        self.target = target.model_copy(deep=True)
        self.runner = runner
        self.bundle = bundle
        self.local_evidence_root = local_evidence_root.resolve()
        self.harness_provenance = harness_provenance
        self.baseline_module_hash = baseline_module_hash
        self.session_budget_seconds = session_budget_seconds
        self.policy = policy
        self.sessions: list[BW20M1ProcessSession] = []
        self._keys: set[tuple[M1Arm, int]] = set()
        self._closing = False
        self._lock = RLock()

    def __call__(self, arm, acquisition_ordinal, payload, output_dir):
        del output_dir
        with self._lock:
            if self._closing:
                raise BW20M1SessionError("BW20 M1 factory is closing")
            context = _trusted_context(payload, self.policy)
            submitted = TargetSpec.model_validate(payload["target"])
            if (
                submitted != self.target
                or payload.get("target_fingerprint") != target_fingerprint(self.target)
                or payload.get("adapter_profile") != self.policy.profile
            ):
                raise ExecutionSafetyError("BW20 M1 performance Target/Profile binding drifted")
            artifact = ArtifactManifest.model_validate(payload["artifact"])
            if artifact.synthetic or artifact.kind != "python_overlay":
                raise ExecutionSafetyError("BW20 M1 requires a real Python Overlay Artifact")
            key = (arm, acquisition_ordinal)
            if key in self._keys:
                raise ExecutionSafetyError("BW20 M1 acquisition identity was reused")
            self._keys.add(key)
            run_id = uuid4()
            plan = build_m1_container_plan(
                self.target,
                run_id=run_id,
                arm=arm,
                acquisition_ordinal=acquisition_ordinal,
                fencing_token=int(context["fencing_token"]),
                artifact_hash=artifact.content_hash if arm == "candidate" else None,
                policy=self.policy,
            )
            artifact_path = file_uri_to_path(artifact.uri) if arm == "candidate" else None
            staged = stage_m1_run(
                plan=plan,
                bundle=self.bundle,
                runner=self.runner,
                artifact=artifact_path,
            )
            transport = BW20M1DockerTransport(runner=self.runner, target=self.target)
            session = BW20M1ProcessSession(
                plan=plan,
                target=self.target,
                transport=transport,
                assert_staging=staged.source_guard,
                assert_lease=context["assert_live_lease"],
                bind_process=lambda cid, ready: capture_process_binding(
                    plan=plan,
                    container_id=cid,
                    ready=ready,
                    inspect=lambda value: transport.inspect(value, 10),
                    proc_root=PurePosixPath("/proc"),
                    read_proc=transport.read_proc,
                    read_namespace=transport.read_namespace,
                    worker_protocol=M1_WORKER_PROTOCOL,
                ),
                cancelled=lambda: bool(
                    getattr(context.get("lease_lost_event"), "is_set", lambda: False)()
                ),
                expected_module_hash=(
                    artifact.content_hash if arm == "candidate" else self.baseline_module_hash
                ),
                budget_seconds=self.session_budget_seconds,
            )
            self.sessions.append(session)
            session.open()
            mirror = BW20M1EvidenceMirror(
                plan=plan,
                runner=self.runner,
                local_root=self.local_evidence_root / str(run_id),
            )
            return BW20M1PairedWorkload(
                process=session,
                target=self.target,
                artifact=artifact,
                mirror=mirror,
            )

    def live_bindings(self):
        with self._lock:
            values = []
            for session in self.sessions:
                if session.closed:
                    if not session.cleanup_complete:
                        raise BW20M1SessionError("closed BW20 M1 session is not clean")
                    continue
                session._guard()
                session._binding()
                values.append(session.identity_observations[-1])
            return values

    def force_close(self) -> bool:
        with self._lock:
            self._closing = True
            for session in self.sessions:
                session.force_close()
            return all(session.cleanup_complete for session in self.sessions)

    def finish(self) -> bool:
        with self._lock:
            self._closing = True
            for session in self.sessions:
                if not session.closed:
                    try:
                        session.close()
                    except Exception:
                        pass
            return self.force_close()


class BW20M1DeviceTimerFactory:
    """Reuse the accepted BW20 Event Timer under the current M1 exclusive lease."""

    def __init__(
        self,
        *,
        target: TargetSpec,
        runner,
        bundle: ControllerBundle,
        session_budget_seconds: int = 480,
        policy: BW20M1Policy = BW20_M1_POLICY,
    ) -> None:
        policy.validate_target(target)
        self.target = target.model_copy(deep=True)
        self.runner = runner
        self.bundle = bundle
        self.session_budget_seconds = session_budget_seconds
        self.policy = policy

    def __call__(self, payload, output_dir):
        del output_dir
        context = _trusted_context(payload, self.policy)
        plan = build_timing_plan(
            self.target,
            run_id=uuid4(),
            fencing_token=int(context["fencing_token"]),
            resource_id=self.policy.resource_id,
        )
        source_guard = stage_controller(plan=plan, bundle=self.bundle, runner=self.runner)
        transport = BW20DockerTransport(runner=self.runner, target=self.target)
        session = guarded_job_session(
            plan=plan,
            transport=transport,
            source_guard=source_guard,
            context=context,
            cancelled=lambda: bool(
                getattr(context.get("lease_lost_event"), "is_set", lambda: False)()
            ),
            budget_seconds=self.session_budget_seconds,
        )
        return DockerTorchEventTimer(session.open())


__all__ = ["BW20M1DeviceTimerFactory", "BW20M1WorkloadFactory"]
