from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from statistics import fmean
from typing import Any, Literal, Protocol

from hcuopt.measurement.models import (
    ClockCalibration,
    ClockCalibrationPointV2,
    ClockCalibrationV2,
)


class CalibrationError(ValueError):
    pass


class DeviceTimerUnavailableError(RuntimeError):
    pass


class HostClock(Protocol):
    def now_ns(self) -> int: ...


class DeviceTimer(Protocol):
    def read_ticks(self) -> int: ...

    def measure_resolution_ns(self, sample_count: int) -> float: ...

    def sample_resolution_ticks(self, sample_count: int) -> Sequence[int]: ...


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

    def measure_resolution_ns(self, sample_count: int = 64) -> float:
        """Measure the smallest positive interval observable by paired events."""

        return float(min(self.sample_resolution_ticks(sample_count)))

    def sample_resolution_ticks(self, sample_count: int = 64) -> tuple[int, ...]:
        """Return the raw positive event deltas used by Formal Stage 0."""

        if sample_count < 2:
            raise CalibrationError("timer resolution requires at least two event pairs")
        pairs = []
        for _ in range(sample_count):
            started = self._torch.cuda.Event(enable_timing=True)
            finished = self._torch.cuda.Event(enable_timing=True)
            started.record()
            finished.record()
            pairs.append((started, finished))
        self._torch.cuda.synchronize()
        intervals_ns: list[int] = []
        for started, finished in pairs:
            elapsed_ns = float(started.elapsed_time(finished)) * 1_000_000
            if not isfinite(elapsed_ns) or elapsed_ns < 0:
                raise CalibrationError(
                    f"invalid CUDA/HIP event resolution sample: {elapsed_ns!r} ns"
                )
            if elapsed_ns > 0:
                intervals_ns.append(round(elapsed_ns))
        if not intervals_ns:
            raise CalibrationError("CUDA/HIP event timer produced no positive resolution sample")
        return tuple(intervals_ns)


def calibrate_device_timer(
    host_clock: HostClock,
    device_timer: DeviceTimer,
    *,
    sample_count: int = 3,
    resolution_sample_count: int = 64,
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

    tick_deltas = [points[index][0] - points[index - 1][0] for index in range(1, len(points))]
    host_deltas = [points[index][1] - points[index - 1][1] for index in range(1, len(points))]
    if any(value <= 0 for value in tick_deltas):
        raise CalibrationError("device timer ticks must strictly increase")
    if any(value <= 0 for value in host_deltas):
        raise CalibrationError("host monotonic time must strictly increase")
    ns_per_tick = sum(host_deltas) / sum(tick_deltas)
    timer_resolution_ns = float(device_timer.measure_resolution_ns(resolution_sample_count))
    if not isfinite(timer_resolution_ns) or timer_resolution_ns <= 0:
        raise CalibrationError(
            f"device timer returned invalid resolution: {timer_resolution_ns!r} ns"
        )
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
        timer_resolution_ns=timer_resolution_ns,
        max_residual_ns=max(residuals),
        point_count=len(points),
    )


def calibrate_device_timer_v2(
    host_clock: HostClock,
    device_timer: DeviceTimer,
    *,
    device_index: int,
    sample_count: int = 5,
    resolution_sample_count: int = 64,
    synchronize: Callable[[], None] | None = None,
    device_name: str = "device-timer",
    capture_mode: Literal[
        "measurement_process_atomic_v1", "measurement_process_spaced_v2"
    ] | None = None,
) -> ClockCalibrationV2:
    """Capture raw calibration inputs and a separately reproducible producer fit."""

    if sample_count < 3:
        raise CalibrationError("Formal device timer calibration requires at least three points")
    if capture_mode in {
        "measurement_process_atomic_v1",
        "measurement_process_spaced_v2",
    }:
        method_name = (
            "capture_spaced_calibration_inputs"
            if capture_mode == "measurement_process_spaced_v2"
            else "capture_calibration_inputs"
        )
        capture = getattr(device_timer, method_name, None)
        if not callable(capture):
            raise DeviceTimerUnavailableError(
                f"{capture_mode} requires measurement-process capture support"
            )
        raw = capture(sample_count, resolution_sample_count)
        if not isinstance(raw, Mapping):
            raise CalibrationError("atomic calibration returned an invalid envelope")
        try:
            points = [ClockCalibrationPointV2.model_validate(point) for point in raw["points"]]
            resolution_tick_deltas = tuple(raw["resolution_tick_deltas"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CalibrationError("atomic calibration returned invalid raw inputs") from exc
        if len(points) != sample_count:
            raise CalibrationError("atomic calibration returned the wrong point count")
    elif capture_mode is None:
        points = []
        for point_ordinal in range(sample_count):
            if synchronize is not None:
                synchronize()
            started = host_clock.now_ns()
            ticks = device_timer.read_ticks()
            finished = host_clock.now_ns()
            if finished <= started:
                raise CalibrationError("Formal clock calibration requires positive host intervals")
            points.append(
                ClockCalibrationPointV2(
                    point_ordinal=point_ordinal,
                    device_ticks=ticks,
                    host_started_monotonic_ns=started,
                    host_finished_monotonic_ns=finished,
                )
            )
        sampler = getattr(device_timer, "sample_resolution_ticks", None)
        if not callable(sampler):
            raise DeviceTimerUnavailableError(
                "Formal Stage 0 requires raw device timer resolution tick samples"
            )
        resolution_tick_deltas = tuple(sampler(resolution_sample_count))
    else:
        raise CalibrationError(f"unsupported calibration capture mode: {capture_mode!r}")
    if len(resolution_tick_deltas) < 3:
        raise CalibrationError(
            "Formal device timer calibration requires at least three resolution samples"
        )
    timer_resolution_ns, ns_per_tick, max_residual_ns = _fit_v2_calibration(
        points,
        resolution_tick_deltas,
    )
    return ClockCalibrationV2(
        device_name=device_name,
        device_index=device_index,
        timer_resolution_ns=timer_resolution_ns,
        ns_per_tick=ns_per_tick,
        max_residual_ns=max_residual_ns,
        points=tuple(points),
        resolution_tick_deltas=resolution_tick_deltas,
    )


def _fit_v2_calibration(
    points: Sequence[ClockCalibrationPointV2],
    resolution_tick_deltas: Sequence[int],
) -> tuple[float, float, float]:
    """Producer-side fit; D recomputes the same claims from raw inputs independently."""

    device_ticks = [float(point.device_ticks) for point in points]
    host_midpoints = [
        point.host_started_monotonic_ns
        + (point.host_finished_monotonic_ns - point.host_started_monotonic_ns) / 2.0
        for point in points
    ]
    mean_ticks = fmean(device_ticks)
    mean_host = fmean(host_midpoints)
    sum_squares = sum((value - mean_ticks) ** 2 for value in device_ticks)
    if sum_squares <= 0:
        raise CalibrationError("Formal device timer calibration has no tick variance")
    slope = (
        sum(
            (device - mean_ticks) * (host - mean_host)
            for device, host in zip(device_ticks, host_midpoints, strict=True)
        )
        / sum_squares
    )
    if not isfinite(slope) or slope <= 0:
        raise CalibrationError("Formal device timer ns_per_tick must be finite and positive")
    intercept = mean_host - slope * mean_ticks
    max_residual = max(
        abs(host - (intercept + slope * device))
        + (point.host_finished_monotonic_ns - point.host_started_monotonic_ns) / 2.0
        for point, device, host in zip(
            points,
            device_ticks,
            host_midpoints,
            strict=True,
        )
    )
    try:
        resolution = min(resolution_tick_deltas) * slope
    except (TypeError, ValueError) as exc:
        raise CalibrationError("Formal device timer resolution samples are invalid") from exc
    if not isfinite(resolution) or resolution <= 0:
        raise CalibrationError("Formal device timer resolution must be finite and positive")
    return resolution, slope, max_residual


def device_elapsed_ns(
    calibration: ClockCalibration,
    started_ticks: int,
    finished_ticks: int,
) -> float:
    if finished_ticks < started_ticks:
        raise CalibrationError("device timer ticks must not go backwards")
    return (finished_ticks - started_ticks) * calibration.ns_per_tick
