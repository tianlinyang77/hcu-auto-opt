# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import threading
from pathlib import Path, PurePosixPath
from uuid import uuid4

import pytest

from hcuopt.adapters.bw20_execution import (
    RESOURCE_ID,
    RUN_PARENT,
    SMOKE_ARGV,
    BW20SmokeExecutionAdapter,
    smoke_mounts,
)
from hcuopt.adapters.execution import FENCING_LABEL, FencingGuard
from hcuopt.contracts.platform_v1 import ExecutionRequest, MountSpec
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError, ImageIdentityError, StaleFencingToken
from hcuopt.targets import load_target
from tests.unit.test_execution_adapters import ScriptedDockerRunner

TARGET = load_target(Path(__file__).parents[2] / "config/targets/bw20-sglang-0.5.12.yaml")
ROOT = f"{RUN_PARENT}/{uuid4()}"


def request(variant="baseline", **updates):
    value = ExecutionRequest(
        target_id=TARGET.target_id, argv=list(SMOKE_ARGV), working_directory="/work",
        timeout_seconds=480, lease_scope=LeaseScope.EXCLUSIVE,
        resource_id=RESOURCE_ID, fencing_token=1,
        container_image=TARGET.inference_image.immutable_reference,
        mounts=smoke_mounts(ROOT, variant),
    )
    return value.model_copy(update=updates)


@pytest.mark.parametrize("variant", ["baseline", "noop"])
def test_bw20_policy_compiles_narrow_scope_and_preserves_lifecycle(tmp_path, variant):
    runner = ScriptedDockerRunner()
    adapter = BW20SmokeExecutionAdapter(runner, run_root=ROOT)
    result = adapter.execute(request(variant), TARGET, tmp_path)
    command = runner.commands[-1]
    assert result.status == "succeeded"  # Fake runner; NOT a hardware acceptance.
    for option in ("--network=none", "--read-only", "--cap-drop=ALL", "--memory=16g",
                   "--memory-swap=16g", "--pids-limit=512", "--shm-size=2g", "--user=65534:65534",
                   "--device=/dev/dri/renderD135", "--device=/dev/kfd",
                   "--cpuset-cpus=64-79", "--cpuset-mems=4", "--init",
                   "no-new-privileges", "/tmp:rw,exec,nosuid,nodev,size=4g",
                   "fsize=2147483648:2147483648", f"{FENCING_LABEL}=1"):
        assert option in command
    for key in ("HIP", "HSA", "ROCR"):
        assert f"{key}_VISIBLE_DEVICES=0" in command
    assert "/dev/dri" not in command and "HIP_VISIBLE_DEVICES=7" not in command
    bindings = [command[i + 1] for i, item in enumerate(command) if item == "--mount"]
    assert sum(not item.endswith(",readonly") for item in bindings) == 1
    assert result.metadata["device_index"] == 7
    assert result.metadata["logical_device_index"] == 0
    assert result.metadata["runtime_pci_verified_by_policy"] is False
    assert result.metadata["fencing_validated_before_result"] is True
    assert result.metadata["automatic_release_allowed"] is False


@pytest.mark.parametrize("updates", [
    {"lease_scope": LeaseScope.NONE}, {"lease_scope": LeaseScope.SHARED},
    {"resource_id": "nmz36-hcu7"}, {"target_id": "nmz36-sglang-0.5.12"},
    {"container_image": "sglang:latest"}, {"timeout_seconds": 481},
    {"argv": ["bash", "-c", "echo unsafe"]}, {"working_directory": "/tmp"},
    {"environment": {"HIP_VISIBLE_DEVICES": "7"}},
    {"environment": {"ROCR_VISIBLE_DEVICES": "7"}},
    {"environment": {"LD_PRELOAD": "/tmp/unsafe.so"}},
    {"environment": {"API_TOKEN": "fixture"}}, {"environment": {"PYTHONPATH": "/work"}},
])
def test_reject_before_any_docker_call(tmp_path, updates):
    runner = ScriptedDockerRunner()
    adapter = BW20SmokeExecutionAdapter(runner, run_root=ROOT)
    with pytest.raises((ExecutionSafetyError, ImageIdentityError)):
        adapter.execute(request(**updates), TARGET, tmp_path)
    assert runner.commands == []


@pytest.mark.parametrize("mutation", ["extra", "duplicate", "rw_model", "broad_model",
                                     "wrong_output", "traversal", "missing", "rw_artifact"])
def test_reject_mount_expansion(tmp_path, mutation):
    mounts = smoke_mounts(ROOT, "noop")
    if mutation == "extra":
        mounts.append(MountSpec(source="/", target="/host", read_only=True))
    elif mutation == "duplicate":
        mounts.append(mounts[0])
    elif mutation == "rw_model":
        mounts[4] = mounts[4].model_copy(update={"read_only": False})
    elif mutation == "broad_model":
        mounts[4] = mounts[4].model_copy(
            update={"source": str(PurePosixPath(mounts[4].source).parent)})
    elif mutation == "wrong_output":
        mounts[3] = mounts[3].model_copy(update={"source": "/tmp"})
    elif mutation == "traversal":
        mounts[1] = mounts[1].model_copy(update={"source": f"{ROOT}/input/../runner.py"})
    elif mutation == "missing":
        mounts.pop(4)
    else:
        mounts[-1] = mounts[-1].model_copy(update={"read_only": False})
    runner = ScriptedDockerRunner()
    with pytest.raises(ExecutionSafetyError):
        BW20SmokeExecutionAdapter(runner, run_root=ROOT).execute(
            request(mounts=mounts), TARGET, tmp_path)
    assert runner.commands == []


@pytest.mark.parametrize("drift", ["host", "numa", "cpu", "device", "image", "workroot"])
def test_reject_target_drift(tmp_path, drift):
    target = TARGET.model_copy(deep=True)
    if drift == "host":
        target.execution_host.address = "10.17.1.2"
    elif drift == "numa":
        target.execution_host.accelerator.numa_node = 7
    elif drift == "cpu":
        target.execution_host.accelerator.cpu_affinity = "112-127"
    elif drift == "device":
        target.execution_host.accelerator.device_index = 0
    elif drift == "image":
        target.inference_image.image_id = "sha256:" + "0" * 64
    else:
        target.execution_host.work_root = "/tmp"
    runner = ScriptedDockerRunner()
    with pytest.raises(ExecutionSafetyError):
        BW20SmokeExecutionAdapter(runner, run_root=ROOT).execute(request(), target, tmp_path)
    assert runner.commands == []


def test_bw20_timeout_removes_only_owned_container(tmp_path):
    runner = ScriptedDockerRunner(process_returncode=None)
    result = BW20SmokeExecutionAdapter(runner, run_root=ROOT, poll_interval_seconds=.001).execute(
        request(timeout_seconds=1), TARGET, tmp_path)
    assert result.status == "timed_out"
    removed = [cmd for cmd in runner.commands if cmd[:3] == ("docker", "rm", "--force")]
    assert removed == [("docker", "rm", "--force", f"hcuopt-{result.request_id.hex}")]


def test_bw20_cancel_reuses_owned_lifecycle(tmp_path):
    runner = ScriptedDockerRunner(process_returncode=None)
    adapter = BW20SmokeExecutionAdapter(runner, run_root=ROOT, poll_interval_seconds=.001)
    req, results = request(), []
    thread = threading.Thread(target=lambda: results.append(adapter.execute(req, TARGET, tmp_path)))
    thread.start()
    try:
        assert runner.started.wait(2)
    finally:
        adapter.cancel(req.request_id)
        thread.join(5)
    assert not thread.is_alive()
    assert results[0].status == "cancelled"


def test_bw20_stale_fence_never_launches(tmp_path):
    guard = FencingGuard()
    guard.before_execute(RESOURCE_ID, 2)
    runner = ScriptedDockerRunner()
    with pytest.raises(StaleFencingToken):
        BW20SmokeExecutionAdapter(runner, run_root=ROOT, fencing_guard=guard).execute(
            request(), TARGET, tmp_path)
    assert runner.commands == []


@pytest.mark.parametrize("path", [
    "/", RUN_PARENT, ROOT + "/", ROOT + "/../other", "/tmp/" + str(uuid4()),
])
def test_reject_unsafe_run_root(path):
    with pytest.raises(ExecutionSafetyError):
        BW20SmokeExecutionAdapter(ScriptedDockerRunner(), run_root=path)
