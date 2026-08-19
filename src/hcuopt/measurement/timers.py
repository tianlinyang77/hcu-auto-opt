from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Any, Protocol

from hcuopt.measurement.models import ClockCalibration


class CalibrationError(ValueError):
    pass


class DeviceTimerUnavailableError(RuntimeError):
    pass


class HostClock(Protocol):
    def now_ns(self) -> int: ...


class DeviceTimer(Protocol):
    def read_ticks(self) -> int: ...


@dataclass(frozen=True, slots=True)
class HostMonotonicClock:
    def now_ns(self) -> int:
        return time.monotonic_ns()


class TorchCudaEventTimer:
    """A device-time axis built from CUDA/HIP events.

    The caller owns device visibility.  On nmz36 this means setting only
    ``ROCR_VISIBLE_DEVICES=7`` before importing PyTorch: ROCR maps physical
    HCU 7 to the container's logical device 0.
    """

    def __init__(self, *, device_index: int = 0, torch_module: Any | None = None) -> None:
        if device_index < 0:
            raise ValueError("device_index must be non-negative")
        if torch_module is None:
            try:
                import torch as torch_module
            except ImportError as error:  # pragma: no cover - depends on target image
                raise DeviceTimerUnavailableError(
                    "PyTorch is required for CUDA/HIP timing"
                ) from error

        self._torch = torch_module
        self._device_index = device_index
        if not self._torch.cuda.is_available():
            raise DeviceTimerUnavailableError("PyTorch reports no CUDA/HIP device is available")
        if self._torch.cuda.device_count() <= device_index:
            raise DeviceTimerUnavailableError(
                f"requested device {device_index}, but only "
                f"{self._torch.cuda.device_count()} are visible"
            )

        self._torch.cuda.set_device(device_index)
        self._torch.cuda.synchronize()
        self._origin = self._torch.cuda.Event(enable_timing=True)
        self._origin.record()
        self._torch.cuda.synchronize()
        self._last_ticks: int | None = None

    def read_ticks(self) -> int:
        snapshot = self._torch.cuda.Event(enable_timing=True)
        snapshot.record()
        self._torch.cuda.synchronize()
        elapsed_ms = float(self._origin.elapsed_time(snapshot))
        if not isfinite(elapsed_ms) or elapsed_ms < 0:
            raise CalibrationError(f"invalid CUDA/HIP event elapsed time: {elapsed_ms!r} ms")
        ticks = round(elapsed_ms * 1_000_000)
        if self._last_ticks is not None and ticks <= self._last_ticks:
            raise CalibrationError("CUDA/HIP event timer did not strictly advance")
        self._last_ticks = ticks
        return ticks


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
