from __future__ import annotations

import pytest

from hcuopt.measurement.fixtures import KnownSignalFixture, NullSignalFixture
from hcuopt.measurement.models import MeasurementPlan
from hcuopt.measurement.sampling import collect_samples
from hcuopt.measurement.timers import (
    CalibrationError,
    DeviceTimerUnavailableError,
    TorchCudaEventTimer,
    calibrate_device_timer,
)


class ScriptedClock:
    def __init__(self, values: list[int]) -> None:
        self._values = iter(values)

    def now_ns(self) -> int:
        return next(self._values)


class ScriptedDeviceTimer:
    def __init__(self, values: list[int]) -> None:
        self._values = iter(values)

    def read_ticks(self) -> int:
        return next(self._values)


class Workload:
    def __init__(self) -> None:
        self.warmups = 0
        self.batches: list[int] = []

    def synchronize(self) -> None:
        return None

    def warmup(self) -> None:
        self.warmups += 1

    def run_batch(self, iterations: int) -> None:
        self.batches.append(iterations)


class FakeEvent:
    def __init__(self, cuda: FakeCuda) -> None:
        self._cuda = cuda
        self.tick = 0

    def record(self) -> None:
        self._cuda.next_tick += 1
        self.tick = self._cuda.next_tick

    def elapsed_time(self, other: FakeEvent) -> float:
        return float(other.tick - self.tick)


class FakeCuda:
    def __init__(self, *, available: bool = True, device_count: int = 1) -> None:
        self.available = available
        self.count = device_count
        self.next_tick = 0
        self.selected_devices: list[int] = []

    def is_available(self) -> bool:
        return self.available

    def device_count(self) -> int:
        return self.count

    def set_device(self, index: int) -> None:
        self.selected_devices.append(index)

    def synchronize(self) -> None:
        return None

    def Event(self, *, enable_timing: bool) -> FakeEvent:
        assert enable_timing is True
        return FakeEvent(self)


class FakeTorch:
    def __init__(self, cuda: FakeCuda) -> None:
        self.cuda = cuda


def _plan() -> MeasurementPlan:
    return MeasurementPlan(
        protocol_version="s0-measurement-v1",
        metric_name="kernel_elapsed",
        unit="ns",
        warmup_count=2,
        repeat_count=2,
        process_restart_count=1,
        batched_loop_count=8,
        environment_fingerprint="sha256:" + "a" * 64,
    )


def test_sampling_keeps_each_restart_and_raw_duration() -> None:
    workloads: list[Workload] = []

    def make_workload(_restart: int) -> Workload:
        workload = Workload()
        workloads.append(workload)
        return workload

    samples = collect_samples(
        _plan(),
        clock=ScriptedClock([0, 10, 20, 40, 50, 80, 90, 130]),
        workload_factory=make_workload,
    )

    assert [(item.restart_ordinal, item.sample_ordinal) for item in samples] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    assert [item.elapsed_ns for item in samples] == [10, 20, 30, 40]
    assert all(workload.warmups == 2 for workload in workloads)
    assert all(workload.batches == [8, 8] for workload in workloads)


def test_device_timer_calibration_rejects_non_monotonic_ticks() -> None:
    with pytest.raises(CalibrationError, match="strictly increase"):
        calibrate_device_timer(
            ScriptedClock([0, 2, 10, 12]),
            ScriptedDeviceTimer([5, 5]),
            sample_count=2,
        )


def test_torch_cuda_event_timer_exposes_a_monotonic_device_time_axis() -> None:
    cuda = FakeCuda()
    timer = TorchCudaEventTimer(torch_module=FakeTorch(cuda))

    assert cuda.selected_devices == [0]
    assert timer.read_ticks() == 1_000_000
    assert timer.read_ticks() == 2_000_000


def test_torch_cuda_event_timer_refuses_an_unavailable_device() -> None:
    with pytest.raises(DeviceTimerUnavailableError, match="no CUDA/HIP device"):
        TorchCudaEventTimer(torch_module=FakeTorch(FakeCuda(available=False)))


def test_scripted_known_and_null_signals_are_distinguished() -> None:
    assert KnownSignalFixture(delta_ns=50).detected(threshold_ns=10) is True
    assert NullSignalFixture().detected(threshold_ns=10) is False
