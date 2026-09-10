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
    def original(argv, *, device_validator):
        seen.update(argv=argv, callback=device_validator)
        return 0
    monkeypatch.setattr(guard, "worker_main", original)
    assert guard.main(["--controller"]) == 0
    assert seen == {"argv": ["--controller"], "callback": guard.validate_device}


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
