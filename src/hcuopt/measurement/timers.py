from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from hcuopt.measurement.models import ClockCalibration


class CalibrationError(ValueError):
    pass


class HostClock(Protocol):
    def now_ns(self) -> int: ...


class DeviceTimer(Protocol):
    def read_ticks(self) -> int: ...


@dataclass(frozen=True, slots=True)
class HostMonotonicClock:
    def now_ns(self) -> int:
        return time.monotonic_ns()


def calibrate_device_timer(
    host_clock: HostClock,
    device_timer: DeviceTimer,
    *,
    sample_count: int = 3,
    synchronize: Callable[[], None] | None = None,
    device_name: str = "device-timer",
) -> ClockCalibration:
    if sample_count < 2:
        raise CalibrationError("device timer calibration requires at least two points")
    points: list[tuple[int, int]] = []
    for _ in range(sample_count):
        if synchronize is not None:
            synchronize()
        started = host_clock.now_ns()
        ticks = device_timer.read_ticks()
        finished = host_clock.now_ns()
        points.append((ticks, started + (finished - started) // 2))

    tick_deltas = [
        points[index][0] - points[index - 1][0] for index in range(1, len(points))
    ]
    host_deltas = [
        points[index][1] - points[index - 1][1] for index in range(1, len(points))
    ]
    if any(value <= 0 for value in tick_deltas):
        raise CalibrationError("device timer ticks must strictly increase")
    if any(value <= 0 for value in host_deltas):
        raise CalibrationError("host monotonic time must strictly increase")
    ns_per_tick = sum(host_deltas) / sum(tick_deltas)
    origin_ticks, origin_ns = points[0]
    residuals = [
        abs((origin_ns + (ticks - origin_ticks) * ns_per_tick) - host_ns)
        for ticks, host_ns in points
    ]
    return ClockCalibration(
        device_name=device_name,
        device_origin_ticks=origin_ticks,
        host_origin_ns=origin_ns,
        ns_per_tick=ns_per_tick,
        max_residual_ns=max(residuals),
        point_count=len(points),
    )


def device_elapsed_ns(
    calibration: ClockCalibration,
    started_ticks: int,
    finished_ticks: int,
) -> float:
    if finished_ticks < started_ticks:
        raise CalibrationError("device timer ticks must not go backwards")
    return (finished_ticks - started_ticks) * calibration.ns_per_tick
