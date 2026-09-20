from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.bw20_endpoint_execution import (
    ENDPOINT_ARGV,
    RUN_PARENT,
    SIGNED_ARTIFACT_PATH,
    TARGET_MODULE_PATH,
    BW20EndpointExecutionAdapter,
    endpoint_mounts,
)
from hcuopt.adapters.bw20_execution import RESOURCE_ID
from hcuopt.adapters.execution import FENCING_LABEL
from hcuopt.contracts.platform_v1 import ExecutionRequest, MountSpec
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.targets import load_target
from tests.unit.test_execution_adapters import ScriptedDockerRunner

TARGET = load_target(Path(__file__).parents[2] / "config/targets/bw20-sglang-0.5.12.yaml")
ROOT = f"{RUN_PARENT}/{uuid4()}"


def _request(arm: str = "baseline", ordinal: int = 0, **updates) -> ExecutionRequest:
    value = ExecutionRequest(
        target_id=TARGET.target_id,
        argv=list(ENDPOINT_ARGV),
        working_directory="/work",
        timeout_seconds=600,
        lease_scope=LeaseScope.EXCLUSIVE,
        resource_id=RESOURCE_ID,
        fencing_token=51,
        container_image=TARGET.inference_image.immutable_reference,
        mounts=endpoint_mounts(ROOT, arm, ordinal),
    )
    return value.model_copy(update=updates)


@pytest.mark.parametrize(("arm", "ordinal"), [("baseline", 0), ("candidate", 1)])
def test_endpoint_policy_compiles_exact_single_hcu_scope(tmp_path, arm, ordinal) -> None:
    runner = ScriptedDockerRunner()
    result = BW20EndpointExecutionAdapter(runner, run_root=ROOT).execute(
        _request(arm, ordinal), TARGET, tmp_path
    )
    command = runner.commands[-1]

    assert result.status == "succeeded"  # Scripted Docker only; not HCU acceptance.
    assert result.metadata["arm"] == arm
    assert result.metadata["run_mode"] == "provisional"
    assert result.metadata["automatic_release_allowed"] is False
    for value in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--device=/dev/kfd",
        "--device=/dev/dri/renderD135",
        "--cpuset-cpus=64-79",
        "--cpuset-mems=4",
        f"{FENCING_LABEL}=51",
        "PYTHONPATH=/opt/hcuopt/activation:/opt/hcuopt/src",
        "HIP_VISIBLE_DEVICES=0",
    ):
        assert value in command
    assert "/dev/dri" not in command
    bindings = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
    assert sum(not item.endswith(",readonly") for item in bindings) == 1
    artifact = [item for item in bindings if f"dst={TARGET_MODULE_PATH}" in item]
    assert bool(artifact) is (arm == "candidate")
    if artifact:
        assert f"src={SIGNED_ARTIFACT_PATH}" in artifact[0]


@pytest.mark.parametrize(
    "updates",
    [
        {"lease_scope": LeaseScope.NONE},
        {"lease_scope": LeaseScope.SHARED},
        {"resource_id": "another-resource"},
        {"timeout_seconds": 601},
        {"argv": ["bash"]},
        {"environment": {"HIP_VISIBLE_DEVICES": "7"}},
        {"environment": {"HCUOPT_ENDPOINT_TARGET_SHA256": "sha256:" + "0" * 64}},
    ],
)
def test_endpoint_policy_rejects_scope_expansion_before_docker(tmp_path, updates) -> None:
    runner = ScriptedDockerRunner()
    with pytest.raises(ExecutionSafetyError):
        BW20EndpointExecutionAdapter(runner, run_root=ROOT).execute(
            _request(**updates), TARGET, tmp_path
        )
    assert runner.commands == []


@pytest.mark.parametrize("mutation", ["extra", "writable_source", "wrong_artifact", "missing"])
def test_endpoint_policy_rejects_mount_drift(tmp_path, mutation) -> None:
    mounts = endpoint_mounts(ROOT, "candidate", 1)
    if mutation == "extra":
        mounts.append(MountSpec(source="/", target="/host", read_only=True))
    elif mutation == "writable_source":
        mounts[1] = mounts[1].model_copy(update={"read_only": False})
    elif mutation == "wrong_artifact":
        mounts[-1] = mounts[-1].model_copy(update={"source": "/tmp/fake.py"})
    else:
        mounts.pop()
    runner = ScriptedDockerRunner()
    with pytest.raises(ExecutionSafetyError, match="mounts"):
        BW20EndpointExecutionAdapter(runner, run_root=ROOT).execute(
            _request("candidate", 1, mounts=mounts), TARGET, tmp_path
        )
    assert runner.commands == []


@pytest.mark.parametrize("path", ["/", RUN_PARENT, ROOT + "/", "/tmp/" + str(uuid4())])
def test_endpoint_policy_rejects_unsafe_run_root(path) -> None:
    with pytest.raises(ExecutionSafetyError):
        BW20EndpointExecutionAdapter(ScriptedDockerRunner(), run_root=path)
