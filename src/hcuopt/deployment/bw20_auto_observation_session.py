# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""No-write lifecycle marker for BW20 measurements that remain in host auto mode.

Clock state itself is captured by the existing telemetry sidecars and adjudicated
by D. This context manager only binds the measurement to the live exclusive lease;
it contains no clock backend, journal, sysfs writer or hy-smi mutation command.
"""

from collections.abc import Mapping

from hcuopt.deployment.bw20_stage0_runtime import RESOURCE


class BW20AutoObservationSession:
    def __init__(self, *, assert_live_lease):
        if not callable(assert_live_lease):
            raise ValueError("process-local live lease check required")
        self.assert_live_lease = assert_live_lease
        self.entered = False
        self.completed = False
        self.quarantined = True

    def _owned(self):
        if self.assert_live_lease() is not None:
            raise RuntimeError("live lease was not confirmed")

    def __enter__(self):
        if self.entered:
            raise RuntimeError("auto observation session cannot be reused")
        self._owned()
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        try:
            self._owned()
        except BaseException:
            self.quarantined = True
            raise
        self.completed = True
        self.quarantined = False
        return False

    def receipt(self):
        return {
            "schema_version": "bw20-auto-observation-session-v1",
            "policy": "host_auto_observe_only_v1",
            "state": "completed" if self.completed else "incomplete",
            "restored": self.completed and not self.quarantined,
            "restoration_not_required": True,
            "quarantined": self.quarantined,
            "clock_mutation_performed": False,
            "clock_backend_required": False,
            "stage0_accepted": False,
            "automatic_release_allowed": False,
        }


class BW20AutoObservationSessionFactory:
    """Bind no-write auto-mode measurement sessions to trusted Worker context."""

    policy = "host_auto_observe_only_v1"

    def __call__(self, context, output_dir):
        del output_dir
        if not isinstance(context, Mapping):
            raise ValueError("trusted job context required")
        if (
            context.get("resource_id") != RESOURCE
            or context.get("lease_scope") != "exclusive"
            or type(context.get("fencing_token")) is not int
            or context["fencing_token"] < 1
        ):
            raise ValueError("exclusive BW20 resource identity required")
        return BW20AutoObservationSession(
            assert_live_lease=context.get("assert_live_lease")
        )
