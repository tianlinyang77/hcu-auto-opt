# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Job-scoped BW20 composition; not registered for admission or automatic release."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.deployment.bw20_stage0_harness import PROFILE, build_bw20_harness
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.measurement.evidence import write_evidence
from hcuopt.measurement.harness import MeasurementSafetyError, _cleanup_is_healthy
from hcuopt.measurement.stage0 import Stage0MeasurementProbeAdapter
from hcuopt.targets import target_fingerprint


@dataclass
class _JobState:
    context: dict
    output_dir: Path
    stopped: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    harness: object = None
    factory: object = None
    session_builder: object = None
    cleanup: dict | None = None
    clock: object = None
    clock_receipt: dict | None = None


class BW20Stage0ProbeAdapter:
    """Use the original Worker/Handler/Probe/D paths, with job-owned cleanup routing.

    Deployment must supply an admission check and a staged guarded-session builder.
    This constructor does not waive Target blockers or invent a database lease.
    A worker instance retains up to 64 job contexts for late cleanup reconciliation;
    recycle it after that bound rather than forgetting a live/unknown reservation.
    """

    def __init__(self, *, target, runner, session_builder, assert_admission,
                 clock_session_factory):
        if not all(callable(value) for value in (
            session_builder, assert_admission, clock_session_factory
        )):
            raise ValueError("trusted admission, session and clock factories required")
        self.target, self.runner = target, runner
        self.session_builder, self.assert_admission = session_builder, assert_admission
        self.clock_session_factory = clock_session_factory
        self.target_fingerprint = target_fingerprint(target)
        self.provenance = AdapterProvenance(
            profile=PROFILE,
            capability="stage0_probe",
            adapter_name="Stage0MeasurementProbeAdapter",
            adapter_version="2",
            implementation_kind="real",
        )
        self._jobs, self._lock = {}, threading.RLock()

    @staticmethod
    def _key(payload):
        context = payload["_job_context"]
        if (
            context.get("resource_id") != RESOURCE
            or context.get("lease_scope") != "exclusive"
            or type(context.get("fencing_token")) is not int
            or context["fencing_token"] < 1
        ):
            raise MeasurementSafetyError("BW20 exclusive job identity required")
        return (
            str(UUID(str(context["job_id"]))),
            str(UUID(str(context["lease_id"]))),
            context["fencing_token"],
        )

    def run_probe(self, payload, output_dir):
        if payload.get("mode") != "formal" or payload.get("adapter_profile") != PROFILE:
            raise MeasurementSafetyError("BW20 adapter requires its explicit Formal profile")
        key = self._key(payload)
        context = dict(payload["_job_context"])
        root = Path(output_dir) / "bw20-stage0-jobs" / key[0] / str(key[2])
        with self._lock:
            if key in self._jobs or len(self._jobs) >= 64:
                raise MeasurementSafetyError(
                    "job context already exists or reconciliation limit hit"
                )
            state = _JobState(context, root)
            self._jobs[key] = state
        result = cleanup = None
        try:
            if self.assert_admission(payload) is not None:
                raise MeasurementSafetyError("admission must raise on failure")
            if payload["probe_type"] == "fingerprint":
                result = self._execute_probe(payload, output_dir, state, context, root)
                cleanup = self.cleanup_probe(payload)
            else:
                state.clock = self.clock_session_factory(context, root)
                if not all(callable(getattr(state.clock, name, None))
                           for name in ("__enter__", "__exit__", "receipt")):
                    raise MeasurementSafetyError("invalid deployment clock session")
                # Workload cleanup happens before restoring the host clock. If
                # restoration fails, the journal remains unresolved and Worker
                # resource_guard forces quarantine at settlement.
                with state.clock:
                    try:
                        result = self._execute_probe(payload, output_dir, state, context, root)
                    finally:
                        cleanup = self.cleanup_probe(payload)
        finally:
            if cleanup is None:
                cleanup = self.cleanup_probe(payload)
            if state.clock is not None:
                try:
                    receipt = state.clock.receipt()
                except Exception as exc:
                    receipt = {
                        "state": "receipt_failed",
                        "restored": False,
                        "quarantined": True,
                        "error_type": type(exc).__name__,
                        "stage0_accepted": False,
                        "automatic_release_allowed": False,
                    }
                if not isinstance(receipt, dict):
                    receipt = {"state": "invalid_receipt", "restored": False,
                               "quarantined": True, "stage0_accepted": False,
                               "automatic_release_allowed": False}
                state.clock_receipt = dict(receipt)
                cleanup = {**cleanup, "clock": state.clock_receipt}
                if not (
                    state.clock_receipt.get("restored") is True
                    and state.clock_receipt.get("quarantined") is False
                ):
                    cleanup = {
                        **cleanup,
                        "health": {
                            **dict(cleanup.get("health") or {}),
                            "healthy": False,
                            "quarantined": True,
                        },
                    }
                state.cleanup = dict(cleanup)
            diagnostics = {
                "schema_version": "bw20-stage0-job-diagnostics-v1",
                "job_id": key[0],
                "lease_id": key[1],
                "fencing_token": key[2],
                "resource_id": RESOURCE,
                "cleanup_evidence": cleanup,
                "stage0_accepted": False,
                "automatic_release_allowed": False,
                "verdict_owner": "stage0-d-verifier",
                "staging_attempts": list(getattr(state.session_builder, "staging_attempts", ())),
                "clock": state.clock_receipt or {
                    "state": "not_required_for_readonly_fingerprint",
                    "restored": True,
                    "quarantined": False,
                    "stage0_accepted": False,
                    "automatic_release_allowed": False,
                },
            }
            if result is not None:
                diagnostics["primary_evidence"] = {
                    "uri": result.raw_evidence_uri, "sha256": result.raw_evidence_hash}
            if state.harness is not None:
                diagnostics.update(
                    telemetry=state.harness.telemetry.observations,
                    host_clock=state.harness.clock.observations,
                    sessions=[
                        {
                            "container_id": s.container_id,
                            "container_name": s.plan.container_name,
                            "cleanup_complete": s.cleanup_complete,
                            "bindings": s.identity_observations,
                            "cache_receipts": s.cache_receipts,
                            "start": s.start_observation,
                            "exit": s.exit_observation,
                        }
                        for s in state.factory.sessions
                    ],
                )
            artifact = write_evidence(root / "diagnostics.json", diagnostics)
        if not _cleanup_is_healthy(cleanup):
            raise MeasurementSafetyError("BW20 job cleanup requires reconciliation")
        return replace(
            result,
            cleanup_evidence={
                **cleanup,
                "diagnostics": {"uri": artifact.uri, "sha256": artifact.sha256},
            },
        )

    def _execute_probe(self, payload, output_dir, state, context, root):
        with state.lock:
            if state.stopped.is_set():
                raise MeasurementSafetyError("job stopped before harness construction")
            builder = self.session_builder(context, root)
            state.session_builder = builder

            def session(role, restart):
                if state.stopped.is_set():
                    raise MeasurementSafetyError("job stopped before session construction")
                value = builder(role, restart)
                cancelled = value.cancelled
                value.cancelled = lambda: state.stopped.is_set() or cancelled()
                return value

            state.harness, state.factory = build_bw20_harness(
                target=self.target, runner=self.runner, session_factory=session, context=context
            )
        if state.stopped.is_set():
            raise MeasurementSafetyError("job stopped during harness construction")

        def no_legacy(*args):
            raise MeasurementSafetyError("Formal verdicts belong to the original D verifier")

        delegate = Stage0MeasurementProbeAdapter(
            state.harness,
            self.target,
            measurement_plan_factory=no_legacy,
            known_signal_detector=no_legacy,
            null_signal_detector=no_legacy,
        )
        # Original evidence already namespaces run/probe/measurement UUIDs.
        return delegate.run_probe(payload, Path(output_dir))

    def cleanup_probe(self, payload):
        key = self._key(payload)
        with self._lock:
            state = self._jobs.get(key)
        if state is None:
            return {
                "fence": {"fenced": False, "reason": "job_context_unknown"},
                "health": {"healthy": False, "quarantined": True},
            }
        # Latch before waiting for construction/open; no later process may start.
        state.stopped.set()
        with state.lock:
            if state.cleanup is not None:
                return dict(state.cleanup)
            if state.harness is None:
                state.cleanup = {
                    "fence": {"fenced": False, "reason": "setup_not_completed"},
                    "health": {"healthy": False, "quarantined": True},
                }
            else:
                try:
                    # Cleanup must remain usable after authority loss: it never renews
                    # or releases a lease, and only closes this job's owned sessions.
                    state.cleanup = state.harness._cleanup(state.context)
                except Exception as exc:
                    state.cleanup = {
                        "fence": {"fenced": False, "error_type": type(exc).__name__},
                        "health": {"healthy": False, "quarantined": True},
                    }
            return dict(state.cleanup)
