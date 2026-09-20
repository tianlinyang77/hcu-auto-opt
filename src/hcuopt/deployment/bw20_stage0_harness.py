# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Deployment composition for the original Harness; intentionally unregistered.

No parallel statistics, pass verdicts, leases or publication authority. Cache
attestation, clock policy and target admission still have to pass their own gates.
"""

from __future__ import annotations

import json
from uuid import UUID

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.deployment.bw20_environment import build_probe_plan
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE
from hcuopt.deployment.bw20_stage0_telemetry import (
    BW20TelemetryCollector,
    BW20TimingCleaner,
    require_endpoint,
)
from hcuopt.deployment.bw20_timing_session import BW20TimingWorkloadFactory
from hcuopt.measurement.harness import EvidenceMeasurementHarness, MeasurementSafetyError
from hcuopt.measurement.nmz36_runtime import DockerProcessLifecycleRecorder

PROFILE = "bw20-stage0-v1"  # Opt-in only; never added to the default catalog.

# Python3.6 lacks monotonic_ns; use integer timespec rather than a lossy float.
# Never mix the Windows controller monotonic epoch with Linux child timestamps.
HOST_CLOCK = r"""
import ctypes,json,platform
class Timespec(ctypes.Structure):
    _fields_=[('tv_sec',ctypes.c_long),('tv_nsec',ctypes.c_long)]
ts=Timespec()
lib=ctypes.CDLL(None,use_errno=True)
clock=lib.clock_gettime
clock.argtypes=[ctypes.c_int,ctypes.POINTER(Timespec)]
clock.restype=ctypes.c_int
if clock(1,ctypes.byref(ts))!=0: raise OSError(ctypes.get_errno())
with open('/proc/sys/kernel/random/boot_id') as f: boot=f.read(128).strip()
print(json.dumps(dict(host=platform.node(),boot_id=boot,
                     monotonic_ns=ts.tv_sec*1000000000+ts.tv_nsec)))
"""


class BW20HostClock:
    def __init__(self, runner, *, boot_id=None):
        require_endpoint(runner)
        if boot_id is not None:
            boot_id = str(UUID(boot_id))
        self.runner, self.boot_id, self.last = runner, boot_id, -1
        self.observations = []

    def now_ns(self):
        result = self.runner.run(("python3", "-c", HOST_CLOCK), timeout=10)
        if result.returncode or len(result.stdout) > 4096 or result.stderr:
            raise MeasurementSafetyError("BW20 host clock unavailable")
        record = json.loads(result.stdout)
        value, boot = record["monotonic_ns"], record["boot_id"]
        UUID(boot)
        if (
            record["host"] != "github-bw20"
            or type(value) is not int
            or value <= self.last
            or (self.boot_id is not None and boot != self.boot_id)
        ):
            raise MeasurementSafetyError("BW20 clock host/epoch changed or did not advance")
        self.boot_id, self.last = boot, value
        self.observations.append(record)
        return value


class BW20EvidenceMeasurementHarness(EvidenceMeasurementHarness):
    @staticmethod
    def _formal_device_index(binding):
        if binding.lease.resource_id != RESOURCE:
            raise MeasurementSafetyError("BW20 measurement resource mismatch")
        return 7  # Physical telemetry index; child-local Event index remains zero.

    @staticmethod
    def _require_live_lease(context):
        EvidenceMeasurementHarness._require_live_lease(context)
        guard = context.get("assert_live_lease")
        if not callable(guard) or guard() is not None:
            raise MeasurementSafetyError("trusted BW20 live lease callback required")


def build_bw20_harness(*, target, runner, session_factory, context):
    """Compose a job-bound factory without starting any accelerator workload.

    Caller supplies guarded_job_session instances with fresh staged plans and
    must enforce Target/registered protocol admission before invoking the Harness.
    Initial host observation is evidence, NOT an assertion that the card is idle.
    """
    build_probe_plan(target)
    if (
        context.get("resource_id") != RESOURCE
        or context.get("lease_scope") != "exclusive"
        or type(context.get("fencing_token")) is not int
        or context["fencing_token"] < 1
    ):
        raise MeasurementSafetyError("exclusive BW20 job context required")
    UUID(str(context["lease_id"]))
    UUID(str(context["job_id"]))
    BW20EvidenceMeasurementHarness._require_live_lease(context)

    def bound_session(role, restart):
        session = session_factory(role, restart)
        if (
            session.plan.resource_id != RESOURCE
            or session.plan.fencing_token != context["fencing_token"]
            or session.assert_lease is not context["assert_live_lease"]
        ):
            raise MeasurementSafetyError("session belongs to a different Worker lease")
        return session

    factory = BW20TimingWorkloadFactory(bound_session)
    telemetry = BW20TelemetryCollector(runner=runner, live_bindings=factory.live_bindings)
    initial = telemetry.collect()["device"]
    cleaner = BW20TimingCleaner(
        factory=factory,
        telemetry=telemetry,
        fencing_token=context["fencing_token"],
        initial_clock_state=dict(
            mode=initial["performance_level"],
            sclk_mhz=initial["sclk_mhz"],
            mclk_mhz=initial["mclk_mhz"],
        ),
    )

    class LazyTimer:
        # Fingerprint needs no torch process; calibration creates it lazily.
        value = None

        def get(self):
            if self.value is None:
                self.value = factory.timer()
            return self.value

        def read_ticks(self):
            return self.get().read_ticks()

        def sample_resolution_ticks(self, count):
            return self.get().sample_resolution_ticks(count)

        def measure_resolution_ns(self, count):
            return self.get().measure_resolution_ns(count)

        def capture_calibration_inputs(self, sample_count, resolution_sample_count):
            return self.get().capture_calibration_inputs(
                sample_count, resolution_sample_count
            )

        def capture_spaced_calibration_inputs(self, sample_count, resolution_sample_count):
            return self.get().capture_spaced_calibration_inputs(
                sample_count, resolution_sample_count
            )

        def synchronize(self):
            return self.get().synchronize()

    def no_legacy(*args):
        raise MeasurementSafetyError("BW20 deployment only supports original Formal V2")

    timer = LazyTimer()
    harness = BW20EvidenceMeasurementHarness(
        provenance=AdapterProvenance(
            profile=PROFILE,
            capability="measurement_harness",
            adapter_name="BW20EvidenceMeasurementHarness",
            adapter_version="1",
            implementation_kind="real",
        ),
        stable_identity=target.model_dump(mode="json"),
        workload_factory=no_legacy,
        telemetry=telemetry,
        device_timer=timer,
        synchronize=timer.synchronize,
        clock=BW20HostClock(runner, boot_id=telemetry.boot_id),
        cleaner=cleaner,
        formal_workload_factory=factory.workload,
        lifecycle_recorder=DockerProcessLifecycleRecorder(),
    )
    return harness, factory
