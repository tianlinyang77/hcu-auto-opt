# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import importlib.metadata
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hcuopt.adapters.profiles import (
    FRAMEWORK_SMOKE_CAPABILITIES,
    MANUAL_CANDIDATE_CAPABILITIES,
    STAGE0_CAPABILITIES,
    AdapterProfile,
    AdapterProfileCatalog,
)
from hcuopt.deployment.bw20_environment import PROBE, build_probe_plan, main
from hcuopt.deployment.nmz36_m1_allocator import _require_target
from hcuopt.domain.errors import TargetNotReady
from hcuopt.targets import TargetCatalog, load_target, target_fingerprint

ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = ROOT / "config/targets/bw20-sglang-0.5.12.yaml"


def test_bw20_target_is_independent_framework_ready_but_not_measurement_ready():
    target = TargetCatalog(ROOT / "config/targets").load("bw20-sglang-0.5.12")
    old = load_target(ROOT / "config/targets/nmz36-sglang-0.5.12.yaml")
    assert target.stage0_status == "pending"
    assert target.automatic_release_allowed is False
    assert target_fingerprint(target) != target_fingerprint(old)
    assert target.inference_image == old.inference_image
    assert old.execution_host.accelerator.architecture == "gfx938"
    assert old.execution_host.accelerator.numa_node == 7
    assert target.execution_host.accelerator.architecture == "gfx936"
    assert target.execution_host.accelerator.numa_node == 4
    assert target.execution_host.observed_host_environment.dtk_version == "not_installed"
    profile = AdapterProfile(
        "test-only", "real",
        FRAMEWORK_SMOKE_CAPABILITIES | STAGE0_CAPABILITIES | MANUAL_CANDIDATE_CAPABILITIES,
    )
    profile.validate_target(target, scope="framework_smoke")
    profile.validate_target(target, scope="stage0")
    for scope in ("optimization", "release"):
        with pytest.raises(TargetNotReady, match="open blockers"):
            profile.validate_target(target, scope=scope)
    with pytest.raises(ValueError, match="locked to nmz36"):
        _require_target(target)
    assert not any("bw20" in item.name for item in AdapterProfileCatalog().list())


def test_probe_plan_has_only_the_reviewed_single_device_scope():
    target = load_target(TARGET_PATH)
    plan = build_probe_plan(target)
    assert plan == build_probe_plan(target)
    argv = plan["argv"]
    assert [x for x in argv if x.startswith("--device=")] == [
        "--device=/dev/kfd", "--device=/dev/dri/renderD135",
    ]
    assert argv[argv.index("--mount") + 1] == (
        "type=bind,src=/opt/hyhal,dst=/opt/hyhal,readonly"
    )
    assert "--network=none" in argv and "--read-only" in argv and "--pull=never" in argv
    assert "--cpuset-cpus=64-79" in argv and "--cpuset-mems=4" in argv
    assert "--kill-after=10s" in argv and "120s" in argv
    assert "--privileged" not in argv and "--network=host" not in argv
    assert plan["executed"] is False and plan["production_profile_registered"] is False
    assert plan["stage0_accepted"] is False and plan["automatic_release_allowed"] is False
    assert json.loads(argv[-1])["target_fingerprint"] == target_fingerprint(target)
    compile(PROBE, "probe", "exec")


@pytest.mark.parametrize("field,value", [
    ("device_index", 0), ("numa_node", 7), ("cpu_affinity", "112-127"),
    ("architecture", "gfx938"),
])
def test_probe_rejects_topology_drift(field, value):
    target = load_target(TARGET_PATH)
    setattr(target.execution_host.accelerator, field, value)
    with pytest.raises(ValueError, match="reviewed BW20 target"):
        build_probe_plan(target)


def test_probe_rejects_old_target_and_mount_expansion():
    with pytest.raises(ValueError, match="reviewed BW20 target"):
        build_probe_plan(load_target(ROOT / "config/targets/nmz36-sglang-0.5.12.yaml"))
    target = load_target(TARGET_PATH)
    target.execution_host.runtime_mounts.append(target.execution_host.runtime_mounts[0])
    with pytest.raises(ValueError, match="only the read-only HYHAL mount"):
        build_probe_plan(target)
    with pytest.raises(ValueError, match="container name"):
        build_probe_plan(load_target(TARGET_PATH), container_name="someone-elses-container")


@pytest.mark.parametrize("count,bus,arch", [
    (8, 177, "gfx936"), (0, 177, "gfx936"), (1, 159, "gfx936"), (1, 177, "gfx938"),
])
def test_generated_probe_rejects_wrong_device_before_allocation(monkeypatch, count, bus, arch):
    plan = build_probe_plan(load_target(TARGET_PATH))
    allocations = []
    device = SimpleNamespace(
        pci_domain_id=0, pci_bus_id=bus, pci_device_id=0, gcnArchName=arch,
    )
    torch = SimpleNamespace(
        cuda=SimpleNamespace(device_count=lambda: count, get_device_properties=lambda _: device),
        ones=lambda *a, **kw: allocations.append(True),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(sys, "argv", ["probe", plan["argv"][-1]])
    monkeypatch.setattr(sys, "version_info", (3, 10))
    monkeypatch.setattr(importlib.metadata, "version", lambda _: (
        load_target(TARGET_PATH).inference_image.sglang_package_version
    ))
    with pytest.raises(AssertionError):
        exec(PROBE, {})
    assert allocations == []


def test_cli_prints_only_a_plan(capsys):
    assert main([str(TARGET_PATH)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["purpose"] == "environment_probe_only"
    assert result["executed"] is False
    assert result["performance_conclusion"] == "not_measured"
