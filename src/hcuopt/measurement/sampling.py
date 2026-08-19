from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from hcuopt.measurement.models import MeasurementPlan, RawSample
from hcuopt.measurement.timers import HostClock


class SampledWorkload(Protocol):
    def synchronize(self) -> None: ...

    def warmup(self) -> None: ...

    def run_batch(self, iterations: int) -> None: ...


def collect_samples(
    plan: MeasurementPlan,
    *,
    clock: HostClock,
    workload_factory: Callable[[int], SampledWorkload],
) -> list[RawSample]:
    """Collect raw host-monotonic samples across explicit restart groups."""

    samples: list[RawSample] = []
    for restart_ordinal in range(plan.process_restart_count + 1):
        workload = workload_factory(restart_ordinal)
        for _ in range(plan.warmup_count):
            workload.synchronize()
            workload.warmup()
        for sample_ordinal in range(plan.repeat_count):
            workload.synchronize()
            started = clock.now_ns()
            workload.run_batch(plan.batched_loop_count)
            workload.synchronize()
            finished = clock.now_ns()
            samples.append(
                RawSample(
                    restart_ordinal=restart_ordinal,
                    sample_ordinal=sample_ordinal,
                    started_monotonic_ns=started,
                    finished_monotonic_ns=finished,
                    batch_iterations=plan.batched_loop_count,
                )
            )
    return samples
