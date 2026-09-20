# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in deployment wiring for B's five probes, not a complete Stage0 profile.

Uses the original Worker callback, staging, transport and Harness. Construction
does not contact the host. No clock writes, blocker waivers or catalog mutation.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from uuid import uuid4

from hcuopt.adapters.profiles import STAGE0_CAPABILITIES, AdapterProfile
from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.deployment.bw20_environment import build_probe_plan
from hcuopt.deployment.bw20_stage0_adapter import BW20Stage0ProbeAdapter
from hcuopt.deployment.bw20_stage0_guards import guarded_job_session
from hcuopt.deployment.bw20_stage0_harness import PROFILE
from hcuopt.deployment.bw20_stage0_runtime import build_timing_plan
from hcuopt.deployment.bw20_stage0_staging import ControllerBundle, stage_controller
from hcuopt.deployment.bw20_stage0_telemetry import require_endpoint
from hcuopt.deployment.bw20_timing_transport import BW20DockerTransport
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.targets import target_fingerprint

PROBES = frozenset({"fingerprint", "timer", "noise", "known_signal", "null_signal"})


@dataclass(frozen=True)
class BW20MeasurementAdmission:
    """Deployment-owned pins; never construct these from a submitted job payload.

    The target revision must already have its Stage0 blockers resolved through the
    existing review process. Pins are not approval signatures or measurement results.
    """

    target_hash: str
    protocol_hash: str
    workload_id: str
    protocol_version: str = "s0-g0-v2"

    def __post_init__(self):
        if self.protocol_version not in {
            "s0-g0-v2",
            "s0-g0-bw20-v1",
            "s0-g0-bw20-v2",
            "s0-g0-bw20-v3",
            "s0-g0-bw20-v4",
        }:
            raise ValueError("unsupported BW20 protocol version")
        for digest in (self.target_hash, self.protocol_hash):
            if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise ValueError("deployment-pinned SHA256 required")
        if not isinstance(self.workload_id, str) or not self.workload_id.strip():
            raise ValueError("frozen workload identity required")

    def validate(self, target, payload, *, probe_types=PROBES):
        build_probe_plan(target)
        if target_fingerprint(target) != self.target_hash:
            raise MeasurementSafetyError("deployment target revision changed")
        submitted = TargetSpec.model_validate(payload["target"])
        if (
            target_fingerprint(submitted) != self.target_hash
            or payload.get("target_fingerprint") != self.target_hash
        ):
            raise MeasurementSafetyError("job target differs from deployment pins")
        if (
            payload.get("mode") != "formal"
            or payload.get("adapter_profile") != PROFILE
            or payload.get("probe_type") not in probe_types
            or payload.get("workload_id") != self.workload_id
            or payload.get("protocol_version") != self.protocol_version
        ):
            raise MeasurementSafetyError("job is outside BW20 measurement scope")
        protocol = load_registered_stage0_protocol(self.protocol_version)
        if protocol.sha256 != self.protocol_hash:
            raise MeasurementSafetyError("registered measurement protocol changed")
        # Reuse the original blocker rules without registering a partial profile.
        AdapterProfile(PROFILE, "real", STAGE0_CAPABILITIES).validate_target(target, scope="stage0")
        return protocol


class _StagedJobSessions:
    def __init__(self, *, target, runner, bundle, context, restart_count, budget_seconds):
        BW20Stage0ProbeAdapter._key({"_job_context": context})
        self.target, self.runner, self.bundle = target, runner, bundle
        self.context = dict(context)
        self.restart_count, self.budget_seconds = restart_count, budget_seconds
        self.staging_attempts = []
        self._used = set()
        self._lock = threading.Lock()

    def __call__(self, role, restart):
        with self._lock:
            if not (
                (role == "timer" and restart is None)
                or (
                    role in PROBES - {"fingerprint"}
                    and type(restart) is int
                    and 0 <= restart < self.restart_count
                )
            ):
                raise MeasurementSafetyError("unexpected measurement session role")
            key = role, restart
            if key in self._used or len(self._used) >= self.restart_count + 1:
                raise MeasurementSafetyError("session identity reused or process budget exceeded")
            guard = self.context.get("assert_live_lease")
            if not callable(guard) or guard() is not None:
                raise MeasurementSafetyError("trusted live lease required before staging")
            self._used.add(key)  # A failed upload must not be silently retried in-place.
            plan = build_timing_plan(
                self.target, run_id=uuid4(), fencing_token=self.context["fencing_token"]
            )
            record = dict(
                role=role,
                restart_ordinal=restart,
                source_root=plan.source_root,
                manifest_sha256=self.bundle.manifest_sha256,
                archive_sha256=self.bundle.archive_sha256,
                staged=False,
                session_returned=False,
            )
            self.staging_attempts.append(record)
            try:
                source_guard = stage_controller(plan=plan, bundle=self.bundle, runner=self.runner)
                record.update(staged=True, checks=list(source_guard.observations))
                if guard() is not None:
                    raise MeasurementSafetyError("live lease check failed after staging")
                session = guarded_job_session(
                    plan=plan,
                    transport=BW20DockerTransport(runner=self.runner, target=self.target),
                    source_guard=source_guard,
                    context=self.context,
                    cancelled=lambda: False,
                    budget_seconds=self.budget_seconds,
                )
                record["session_returned"] = True
                return session  # Original factory owns open/close/cleanup, not this builder.
            except Exception as exc:
                record["error_type"] = type(exc).__name__
                raise


def compose_measurement_adapter(
    *,
    target,
    runner,
    bundle: ControllerBundle,
    manifest_sha256: str,
    admission: BW20MeasurementAdmission,
    clock_session_factory,
    session_budget_seconds: int = 480,
):
    """Compose B only; do not put this partial adapter into the public catalog.

    Caller freezes a controller archive once and supplies an independently pinned
    manifest. Each session gets a fresh remote directory; no shared snapshot overwrite.
    The original adapter latches cancellation and preserves staging failure receipts.
    """
    require_endpoint(runner)
    target = target.model_copy(deep=True)
    build_probe_plan(target)
    if not isinstance(admission, BW20MeasurementAdmission):
        raise ValueError("deployment-owned measurement admission required")
    if (
        not isinstance(bundle, ControllerBundle)
        or not isinstance(manifest_sha256, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256) is None
        or bundle.manifest_sha256 != manifest_sha256
    ):
        raise ValueError("controller bundle differs from pinned source manifest")
    if type(session_budget_seconds) is not int or not 1 <= session_budget_seconds <= 480:
        raise ValueError("session budget must be 1..480 seconds")
    if not callable(clock_session_factory):
        raise ValueError("deployment-owned clock session factory required")

    def assert_admission(payload):
        admission.validate(target, payload)

    def session_builder(context, output_dir):
        protocol = load_registered_stage0_protocol(admission.protocol_version)
        if protocol.sha256 != admission.protocol_hash:
            raise MeasurementSafetyError("protocol changed during composition")
        return _StagedJobSessions(
            target=target,
            runner=runner,
            bundle=bundle,
            context=context,
            restart_count=protocol.protocol.sampling.restart_count,
            budget_seconds=session_budget_seconds,
        )

    adapter = BW20Stage0ProbeAdapter(
        target=target,
        runner=runner,
        assert_admission=assert_admission,
        session_builder=session_builder,
        clock_session_factory=clock_session_factory,
    )
    adapter.admission = admission
    return adapter
