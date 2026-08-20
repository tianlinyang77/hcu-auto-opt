from __future__ import annotations

import os
from collections.abc import Callable
from typing import Protocol

from hcuopt.measurement.models import MeasurementPlan, ProcessIdentity, RawSample
from hcuopt.measurement.timers import DeviceTimer, HostClock


class SampledWorkload(Protocol):
    def process_identity(self) -> ProcessIdentity: ...

    def synchronize(self) -> None: ...

    def warmup(self) -> None: ...

    def run_batch(self, iterations: int) -> None: ...

    def close(self) -> None: ...

    def is_alive(self) -> bool: ...


class SamplingSafetyError(RuntimeError):
    pass


def collect_samples(
    plan: MeasurementPlan,
    *,
    clock: HostClock,
    workload_factory: Callable[[int], SampledWorkload],
    device_timer: DeviceTimer | None = None,
    before_sample: Callable[[], None] | None = None,
    require_fresh_processes: bool = False,
) -> list[RawSample]:
    """Collect raw host-monotonic samples across explicit restart groups."""

    samples: list[RawSample] = []
    process_identities: set[tuple[int, str]] = set()
    for restart_ordinal in range(plan.process_restart_count + 1):
        workload = workload_factory(restart_ordinal)
        try:
            identity = ProcessIdentity.model_validate(workload.process_identity())
            identity_key = (identity.pid, identity.start_token)
            if identity_key in process_identities:
                raise SamplingSafetyError(
                    "process restart groups must use distinct process identities"
                )
            if require_fresh_processes and identity.pid == os.getpid():
                raise SamplingSafetyError(
                    "formal measurement workloads must run outside the harness process"
                )
            process_identities.add(identity_key)
            for _ in range(plan.warmup_count):
                workload.synchronize()
                workload.warmup()
            for sample_ordinal in range(plan.repeat_count):
                if before_sample is not None:
                    before_sample()
                workload.synchronize()
                started = clock.now_ns()
                started_ticks = device_timer.read_ticks() if device_timer is not None else None
                workload.run_batch(plan.batched_loop_count)
                workload.synchronize()
                finished_ticks = device_timer.read_ticks() if device_timer is not None else None
                finished = clock.now_ns()
                samples.append(
                    RawSample(
                        restart_ordinal=restart_ordinal,
                        sample_ordinal=sample_ordinal,
                        process_id=identity.pid,
                        process_start_token=identity.start_token,
                        started_monotonic_ns=started,
                        finished_monotonic_ns=finished,
                        batch_iterations=plan.batched_loop_count,
                        started_device_ticks=started_ticks,
                        finished_device_ticks=finished_ticks,
                    )
                )
        finally:
            workload.close()
            if workload.is_alive():
                raise SamplingSafetyError("measurement workload remained alive after close")
    return samples
