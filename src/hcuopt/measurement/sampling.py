from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from hcuopt.measurement.models import MeasurementPlan, RawSample
from hcuopt.measurement.timers import DeviceTimer, HostClock


class SampledWorkload(Protocol):
    def synchronize(self) -> None: ...

    def warmup(self) -> None: ...

    def run_batch(self, iterations: int) -> None: ...


def collect_samples(
    plan: MeasurementPlan,
    *,
    clock: HostClock,
    workload_factory: Callable[[int], SampledWorkload],
    device_timer: DeviceTimer | None = None,
    before_sample: Callable[[], None] | None = None,
) -> list[RawSample]:
    """Collect raw host-monotonic samples across explicit restart groups."""

    samples: list[RawSample] = []
    for restart_ordinal in range(plan.process_restart_count + 1):
        workload = workload_factory(restart_ordinal)
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
                    started_monotonic_ns=started,
                    finished_monotonic_ns=finished,
                    batch_iterations=plan.batched_loop_count,
                    started_device_ticks=started_ticks,
                    finished_device_ticks=finished_ticks,
                )
            )
    return samples
