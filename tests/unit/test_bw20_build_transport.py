# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_build_transport import REMOTE_PARENT, cpu_command
from hcuopt.domain.errors import ExecutionSafetyError


def parameters():
    request = str(uuid4())
    root = f"{REMOTE_PARENT}/{uuid4()}"
    return dict(task_root=root,
        controller_root=f"/home/github/hcu-auto-opt-runtime/bw20-api-controller-{uuid4()}",
        request_path=f"{root}/requests/{request}.json",
        container_name="hcuopt-bw20-build-" + request.replace("-", ""))


def test_no_hcu_cpu_scope():
    command = cpu_command(**parameters())
    for item in ("--network=none", "--read-only", "--cap-drop=ALL", "--user=1002:1002",
                 "--memory=4g", "--memory-swap=4g", "--pids-limit=128", "--init"):
        assert item in command
    assert not any("--device" in arg or "privileged" in arg or "docker.sock" in arg
                   for arg in command)
    mounts = [command[i+1] for i, arg in enumerate(command) if arg == "--mount"]
    assert len(mounts) == 4 and sum(not m.endswith(",readonly") for m in mounts) == 1
    assert "hcuopt.deployment.bw20_build_worker" in command


@pytest.mark.parametrize("field,value", [
    ("task_root", "/"), ("task_root", REMOTE_PARENT),
    ("controller_root", "/tmp/controller"), ("request_path", "/tmp/request.json"),
    ("container_name", "unrelated-container"),
])
def test_paths_and_container_identity_cannot_expand(field, value):
    args = parameters()
    args[field] = value
    with pytest.raises((ValueError, ExecutionSafetyError)):
        cpu_command(**args)
