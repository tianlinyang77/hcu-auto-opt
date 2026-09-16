# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path

import pytest

from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY
from hcuopt.deployment.nmz36_m1_allocator import _base_docker_argv
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]


def _target():
    return load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


def _context(resource_id=BW20_M1_POLICY.resource_id, scope="exclusive"):
    return {
        "_job_context": {
            "lease_id": "00000000-0000-0000-0000-000000000001",
            "lease_scope": scope,
            "resource_id": resource_id,
            "fencing_token": 9,
        }
    }


def test_bw20_m1_policy_binds_target_resource_and_logical_device_zero() -> None:
    BW20_M1_POLICY.validate_target(_target())
    assert BW20_M1_POLICY.require_job_context(
        _context(), LeaseScope.EXCLUSIVE
    )["fencing_token"] == 9
    resources = BW20_M1_POLICY.docker_resource_arguments()
    environment = BW20_M1_POLICY.container_environment()
    assert "--device=/dev/dri/renderD135" in resources
    assert "--device=/dev/dri" not in resources
    assert "--cpuset-cpus=64-79" in resources
    assert "--cpuset-mems=4" in resources
    assert "HIP_VISIBLE_DEVICES=0" in environment
    assert "ROCR_VISIBLE_DEVICES=0" in environment
    assert "HSA_VISIBLE_DEVICES=0" in environment


def test_bw20_m1_policy_rejects_nmz36_or_cross_target_lease() -> None:
    with pytest.raises(ExecutionSafetyError, match="Target binding"):
        BW20_M1_POLICY.validate_target(
            load_target(ROOT / "config/targets/nmz36-sglang-0.5.12.yaml")
        )
    with pytest.raises(ExecutionSafetyError, match="cross-target"):
        BW20_M1_POLICY.require_job_context(_context("hcu-7"), LeaseScope.EXCLUSIVE)
    with pytest.raises(ExecutionSafetyError, match="cross-target"):
        BW20_M1_POLICY.require_job_context(_context(scope="shared"), LeaseScope.EXCLUSIVE)


def test_bw20_m1_target_requires_observe_only_auto_clock_policy() -> None:
    changed = _target().model_copy(deep=True)
    changed.execution_host.accelerator.expected_performance_level = "manual"
    with pytest.raises(ExecutionSafetyError, match="Target binding"):
        BW20_M1_POLICY.validate_target(changed)


def test_bw20_m1_docker_plan_uses_isolated_pid_and_exact_render_device(tmp_path) -> None:
    source = tmp_path / "source"
    evidence = tmp_path / "evidence"
    cache = tmp_path / "cache"
    for directory in (source, evidence, cache):
        directory.mkdir()

    argv = _base_docker_argv(
        target=_target(),
        source_root=source,
        evidence_dir=evidence,
        cache_dir=cache,
        container_name="hcuopt-bw20-m1-test",
        resource_id=BW20_M1_POLICY.resource_id,
        fencing_token=9,
        artifact=None,
        deployment_policy=BW20_M1_POLICY,
    )

    assert "--pid=host" not in argv
    assert "--device=/dev/dri/renderD135" in argv
    assert "--device=/dev/dri" not in argv
    assert argv.count("HIP_VISIBLE_DEVICES=0") == 1
    assert argv.count("ROCR_VISIBLE_DEVICES=0") == 1
    assert argv.count("HSA_VISIBLE_DEVICES=0") == 1
    assert _target().inference_image.immutable_reference in argv
