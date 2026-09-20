# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import io
import json
import sys
from types import SimpleNamespace

import pytest

from hcuopt.deployment import bw20_stage0_worker as guard
from hcuopt.measurement import torch_worker as worker


def fake_torch(bus=177, arch="gfx936", count=1):
    properties = SimpleNamespace(pci_domain_id=0, pci_bus_id=bus, pci_device_id=0, gcnArchName=arch)
    return SimpleNamespace(version=SimpleNamespace(hip="fixture"),
        cuda=SimpleNamespace(is_available=lambda: True, device_count=lambda: count,
                             get_device_properties=lambda device: properties))


@pytest.mark.parametrize("bus,arch,count", [(176, "gfx936", 1), (177, "gfx938", 1),
                                          (177, "gfx936", 2)])
def test_wrong_device_rejected(bus, arch, count):
    with pytest.raises(RuntimeError):
        guard.validate_device(fake_torch(bus, arch, count))


def test_expected_device_passes_without_allocating():
    assert guard.validate_device(fake_torch())["pci"] == "0000:b1:00.0"


def test_guard_failure_precedes_test_tensor_creation(monkeypatch):
    class Stream(io.StringIO):
        def close(self):
            pass
    commands, responses = Stream(""), Stream()
    streams = iter([commands, responses])
    monkeypatch.setattr(worker.os, "fdopen", lambda *args, **kwargs: next(streams))
    torch = fake_torch(bus=176)
    torch.ones = lambda *args, **kwargs: pytest.fail("wrong card allocated a tensor")
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert worker._child_loop(11, 12, guard.validate_device) == 1
    result = json.loads(responses.getvalue())
    assert result["event"] == "error" and "mismatch" in result["error"]


def test_entrypoint_only_passes_child_hook(monkeypatch):
    seen = {}
    def original(argv, *, device_validator, cache_receipts):
        seen.update(argv=argv, callback=device_validator, cache_receipts=cache_receipts)
        return 0
    monkeypatch.setattr(guard, "worker_main", original)
    assert guard.main(["--controller"]) == 0
    assert seen == {"argv": ["--controller"], "callback": guard.validate_device,
                    "cache_receipts": True}


def test_allocator_receipt_records_actual_order_not_hardware_flush(monkeypatch):
    actions = []
    torch = SimpleNamespace(cuda=SimpleNamespace(
        synchronize=lambda: actions.append("sync"), empty_cache=lambda: actions.append("empty")))
    times = iter([100, 110])
    monkeypatch.setattr(worker.time, "monotonic_ns", lambda: next(times))
    receipt = worker._allocator_cache_receipt(torch, 1)
    assert actions == ["sync", "empty", "sync"]
    assert receipt["hardware_cache_flushed"] is False
    assert receipt["scope"] == "pytorch_unused_allocator_blocks_only"
    assert receipt["started_monotonic_ns"] == 100 and receipt["finished_monotonic_ns"] == 110


def test_cache_failure_does_not_issue_success_receipt():
    def fail():
        raise RuntimeError("allocator failed")
    torch = SimpleNamespace(cuda=SimpleNamespace(synchronize=lambda: None, empty_cache=fail))
    with pytest.raises(RuntimeError, match="allocator failed"):
        worker._allocator_cache_receipt(torch, 1)


@pytest.mark.parametrize("enabled", [False, True])
def test_original_measure_loop_emits_cache_receipt_when_enabled(monkeypatch, enabled):
    class Stream(io.StringIO):
        def close(self):
            pass
    class Tensor:
        def add_(self, value):
            pass
    class Event:
        counter = 0
        def record(self):
            Event.counter += 1
            self.tick = Event.counter
        def elapsed_time(self, other):
            return (other.tick - self.tick) * .01
    commands = Stream(json.dumps(dict(op="measure", probe_type="noise", segment="noise",
                                     iterations=2)) + '\n' + '{"op":"close"}\n')
    responses = Stream()
    streams = iter([commands, responses])
    monkeypatch.setattr(worker.os, "fdopen", lambda *a, **kw: next(streams))
    torch = fake_torch()
    torch.ones = lambda *a, **kw: Tensor()
    torch.float32 = "fixture-fp32"
    torch.cuda.set_device = lambda device: None
    torch.cuda.synchronize = lambda: None
    torch.cuda.empty_cache = lambda: None
    torch.cuda.Event = lambda **kw: Event()
    monkeypatch.setitem(sys.modules, "torch", torch)
    ticks = iter([100, 110, 120, 130])
    monkeypatch.setattr(worker.time, "monotonic_ns", lambda: next(ticks))
    assert worker._child_loop(11, 12, cache_receipts=enabled) == 0
    measured = json.loads(responses.getvalue().splitlines()[1])
    assert measured["batch_iterations"] == 2 and measured["segment"] == "noise"
    assert ("cache_receipt" in measured) is enabled
    if enabled:
        assert measured["cache_receipt"]["process_id"] == measured["process_id"]
        assert (measured["cache_receipt"]["finished_monotonic_ns"]
                <= measured["started_monotonic_ns"])


def test_warmup_executes_and_acknowledges_the_requested_complete_batch(monkeypatch):
    class Stream(io.StringIO):
        def close(self):
            pass

    class Tensor:
        def __init__(self):
            self.calls = 0

        def add_(self, value):
            self.calls += 1

    class Event:
        def record(self):
            pass

    commands = Stream(
        json.dumps(
            dict(op="warmup", probe_type="noise", segment="noise", iterations=7)
        )
        + '\n{"op":"close"}\n'
    )
    responses = Stream()
    streams = iter([commands, responses])
    monkeypatch.setattr(worker.os, "fdopen", lambda *args, **kwargs: next(streams))
    tensor = Tensor()
    torch = fake_torch()
    torch.ones = lambda *args, **kwargs: tensor
    torch.float32 = "fixture-fp32"
    torch.cuda.set_device = lambda device: None
    torch.cuda.Event = lambda **kwargs: Event()
    torch.cuda.synchronize = lambda: None
    monkeypatch.setitem(sys.modules, "torch", torch)

    assert worker._child_loop(11, 12, cache_receipts=True) == 0
    warmed = json.loads(responses.getvalue().splitlines()[1])
    assert warmed["event"] == "warmed"
    assert warmed["batch_iterations"] == 7
    assert tensor.calls == 7


def test_atomic_calibration_captures_both_axes_inside_measured_child(monkeypatch):
    class Stream(io.StringIO):
        def close(self):
            pass

    class Tensor:
        def add_(self, value):
            pass

    class Event:
        counter = 0

        def record(self):
            Event.counter += 1
            self.tick = Event.counter

        def elapsed_time(self, other):
            return (other.tick - self.tick) * 0.001

    commands = Stream(
        json.dumps(
            dict(op="calibration", sample_count=3, resolution_sample_count=4)
        )
        + '\n{"op":"close"}\n'
    )
    responses = Stream()
    streams = iter([commands, responses])
    monkeypatch.setattr(worker.os, "fdopen", lambda *a, **kw: next(streams))
    torch = fake_torch()
    torch.ones = lambda *a, **kw: Tensor()
    torch.float32 = "fixture-fp32"
    torch.cuda.set_device = lambda device: None
    torch.cuda.synchronize = lambda: None
    torch.cuda.empty_cache = lambda: None
    torch.cuda.Event = lambda **kw: Event()
    monkeypatch.setitem(sys.modules, "torch", torch)
    host_times = iter([100, 102, 110, 112, 120, 122])
    monkeypatch.setattr(worker.time, "monotonic_ns", lambda: next(host_times))

    assert worker._child_loop(11, 12, cache_receipts=True) == 0
    captured = json.loads(responses.getvalue().splitlines()[1])
    assert [point["point_ordinal"] for point in captured["points"]] == [0, 1, 2]
    assert [point["host_started_monotonic_ns"] for point in captured["points"]] == [
        100,
        110,
        120,
    ]
    assert len(captured["resolution_tick_deltas"]) == 4


def test_spaced_calibration_separates_points_without_clock_mutation(monkeypatch):
    class Stream(io.StringIO):
        def close(self):
            pass

    class Tensor:
        def add_(self, value):
            pass

    class Event:
        counter = 0

        def record(self):
            Event.counter += 1
            self.tick = Event.counter

        def elapsed_time(self, other):
            return (other.tick - self.tick) * 0.001

    commands = Stream(
        json.dumps(dict(op="calibration_spaced", sample_count=3, resolution_sample_count=4))
        + '\n{"op":"close"}\n'
    )
    responses = Stream()
    streams = iter([commands, responses])
    monkeypatch.setattr(worker.os, "fdopen", lambda *a, **kw: next(streams))
    torch = fake_torch()
    torch.ones = lambda *a, **kw: Tensor()
    torch.float32 = "fixture-fp32"
    torch.cuda.set_device = lambda device: None
    torch.cuda.synchronize = lambda: None
    torch.cuda.Event = lambda **kw: Event()
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(worker.time, "monotonic_ns", lambda: Event.counter * 10_000 + 100)
    sleeps = []
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)

    assert worker._child_loop(11, 12, cache_receipts=True) == 0
    captured = json.loads(responses.getvalue().splitlines()[1])
    assert sleeps == [0.01, 0.01]
    assert [point["device_ticks"] for point in captured["points"]] == [1_000, 2_000, 3_000]


def test_fixture_size_is_configured_once_before_measurement(monkeypatch):
    class Stream(io.StringIO):
        def close(self):
            pass

    class Tensor:
        def add_(self, value):
            pass

    class Event:
        counter = 0

        def record(self):
            Event.counter += 1
            self.tick = Event.counter

        def elapsed_time(self, other):
            return (other.tick - self.tick) * 0.01

    commands = Stream(
        '{"op":"configure_fixture","workload_elements":16777216}\n'
        '{"op":"measure","probe_type":"noise","segment":"noise","iterations":2}\n'
        '{"op":"close"}\n'
    )
    responses = Stream()
    streams = iter([commands, responses])
    monkeypatch.setattr(worker.os, "fdopen", lambda *a, **kw: next(streams))
    allocations = []
    torch = fake_torch()
    torch.ones = lambda *a, **kw: allocations.append((a, kw)) or Tensor()
    torch.float32 = "fixture-fp32"
    torch.cuda.set_device = lambda device: None
    torch.cuda.synchronize = lambda: None
    torch.cuda.empty_cache = lambda: None
    torch.cuda.Event = lambda **kw: Event()
    monkeypatch.setitem(sys.modules, "torch", torch)
    ticks = iter([100, 110, 120, 130])
    monkeypatch.setattr(worker.time, "monotonic_ns", lambda: next(ticks))

    assert worker._child_loop(11, 12, cache_receipts=True) == 0
    replies = [json.loads(line) for line in responses.getvalue().splitlines()]
    assert replies[1]["workload_elements"] == 1 << 24
    assert [entry[0][0] for entry in allocations] == [1 << 18, 1 << 24]


@pytest.mark.parametrize("guarded", [False, True])
def test_default_worker_and_guarded_worker_share_original_child_loop(monkeypatch, guarded):
    class Stream(io.StringIO):
        def close(self):
            pass
    class Event:
        def record(self):
            pass
    commands, responses = Stream(""), Stream()
    streams = iter([commands, responses])
    monkeypatch.setattr(worker.os, "fdopen", lambda *args, **kwargs: next(streams))
    torch = fake_torch()
    allocations = []
    torch.ones = lambda *args, **kwargs: allocations.append((args, kwargs))
    torch.float32 = "fixture-fp32"
    torch.cuda.set_device = lambda device: None
    torch.cuda.Event = lambda **kwargs: Event()
    torch.cuda.synchronize = lambda: None
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert worker._child_loop(11, 12, guard.validate_device if guarded else None) == 0
    result = json.loads(responses.getvalue())
    assert result["event"] == "ready" and len(allocations) == 1
    assert ("device_identity" in result) is guarded
