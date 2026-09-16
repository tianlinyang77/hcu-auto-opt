# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY
from hcuopt.deployment.bw20_m1_runtime import (
    BW20M1DockerTransport,
    build_m1_container_plan,
    validate_m1_container,
)
from hcuopt.deployment.nmz36_m1_allocator import ALLOCATOR_MOUNT_TARGET
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = UUID("00000000-0000-0000-0000-000000000123")
CID = "a" * 64
ARTIFACT_HASH = "sha256:" + "b" * 64


def _target():
    return load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


def _inspect(plan):
    mounts = [
        {"Type": "bind", "Source": plan.source_root, "Destination": "/workspace", "RW": False},
        {"Type": "bind", "Source": plan.evidence_root, "Destination": "/evidence", "RW": True},
        {"Type": "bind", "Source": plan.cache_root, "Destination": "/cache", "RW": True},
        {"Type": "bind", "Source": "/opt/hyhal", "Destination": "/opt/hyhal", "RW": False},
    ]
    if plan.artifact_path is not None:
        mounts.append(
            {
                "Type": "bind",
                "Source": plan.artifact_path,
                "Destination": ALLOCATOR_MOUNT_TARGET,
                "RW": False,
            }
        )
    return {
        "Id": CID,
        "Name": "/" + plan.container_name,
        "Image": BW20_M1_POLICY.image_id,
        "State": {"Running": False},
        "Config": {
            "Image": _target().inference_image.immutable_reference,
            "User": "65534:65534",
            "Entrypoint": ["python"],
            "Cmd": list(plan.argv[plan.argv.index("-m") :]),
            "Labels": {
                "io.hcuopt.managed": "true",
                "io.hcuopt.resource-id": plan.resource_id,
                "io.hcuopt.fencing-token": str(plan.fencing_token),
            },
            "Env": list(BW20_M1_POLICY.container_environment()),
        },
        "HostConfig": {
            "NetworkMode": "none",
            "PidMode": "",
            "IpcMode": "private",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CpusetCpus": "64-79",
            "CpusetMems": "4",
            "Memory": 16 * 1024**3,
            "MemorySwap": 16 * 1024**3,
            "PidsLimit": 512,
            "ShmSize": 2 * 1024**3,
            "AutoRemove": True,
            "RestartPolicy": {"Name": "no"},
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges"],
            "DeviceRequests": None,
            "DeviceCgroupRules": None,
            "VolumesFrom": None,
            "Devices": [
                {
                    "PathOnHost": "/dev/kfd",
                    "PathInContainer": "/dev/kfd",
                    "CgroupPermissions": "rwm",
                },
                {
                    "PathOnHost": "/dev/dri/renderD135",
                    "PathInContainer": "/dev/dri/renderD135",
                    "CgroupPermissions": "rwm",
                },
            ],
            "Tmpfs": {"/tmp": "rw,exec,nosuid,nodev,size=4g"},
        },
        "Mounts": mounts,
    }


def test_builds_distinct_canonical_baseline_and_candidate_plans() -> None:
    baseline = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="baseline",
        acquisition_ordinal=0,
        fencing_token=9,
    )
    candidate = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="candidate",
        acquisition_ordinal=1,
        fencing_token=9,
        artifact_hash=ARTIFACT_HASH,
    )

    assert baseline.resource_id == candidate.resource_id == BW20_M1_POLICY.resource_id
    assert baseline.artifact_path is None
    assert candidate.artifact_path.endswith("/candidate-overlay.py")
    assert "--pid=host" not in candidate.argv
    assert "--device=/dev/dri/renderD135" in candidate.argv
    assert "--device=/dev/dri" not in candidate.argv
    assert candidate.argv[-2:] == ("--expected-artifact-hash", ARTIFACT_HASH)


@pytest.mark.parametrize(
    ("arm", "artifact_hash"),
    [("baseline", ARTIFACT_HASH), ("candidate", None)],
)
def test_plan_rejects_artifact_arm_mismatch(arm, artifact_hash) -> None:
    with pytest.raises(ValueError, match="Artifact Hash"):
        build_m1_container_plan(
            _target(),
            run_id=RUN_ID,
            arm=arm,
            acquisition_ordinal=0,
            fencing_token=9,
            artifact_hash=artifact_hash,
        )


def test_daemon_scope_accepts_canonical_plan_and_rejects_scope_drift() -> None:
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="candidate",
        acquisition_ordinal=1,
        fencing_token=9,
        artifact_hash=ARTIFACT_HASH,
    )
    raw = _inspect(plan)
    validate_m1_container(raw, CID, plan, _target())

    raw["HostConfig"]["PidMode"] = "host"
    with pytest.raises(ExecutionSafetyError, match="scope mismatch"):
        validate_m1_container(raw, CID, plan, _target())


def test_daemon_scope_rejects_broad_device_or_missing_candidate_mount() -> None:
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="candidate",
        acquisition_ordinal=1,
        fencing_token=9,
        artifact_hash=ARTIFACT_HASH,
    )
    broad = _inspect(plan)
    broad["HostConfig"]["Devices"][1]["PathOnHost"] = "/dev/dri"
    with pytest.raises(ExecutionSafetyError, match="scope mismatch"):
        validate_m1_container(broad, CID, plan, _target())

    missing = _inspect(plan)
    missing["Mounts"].pop()
    with pytest.raises(ExecutionSafetyError, match="scope mismatch"):
        validate_m1_container(missing, CID, plan, _target())


def test_remote_transport_creates_only_a_rebuilt_canonical_plan(monkeypatch) -> None:
    runner = SimpleNamespace(host="10.17.1.20", user="github", port=22)
    transport = BW20M1DockerTransport(runner=runner, target=_target())
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="baseline",
        acquisition_ordinal=0,
        fencing_token=9,
    )
    monkeypatch.setattr(transport, "_create", lambda value, timeout: CID)
    assert transport.create(plan, 10) == CID

    changed = replace(plan, argv=plan.argv + ("--privileged",))
    with pytest.raises(ValueError, match="noncanonical"):
        transport.create(changed, 10)


def test_remote_transport_validates_daemon_scope_before_attach(monkeypatch) -> None:
    commands = []
    runner = SimpleNamespace(
        host="10.17.1.20",
        user="github",
        port=22,
        wrapped_argv=lambda argv: commands.append(tuple(argv)) or ("ssh",),
    )
    transport = BW20M1DockerTransport(runner=runner, target=_target())
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="baseline",
        acquisition_ordinal=0,
        fencing_token=9,
    )
    transport.owned[CID] = plan
    raw = _inspect(plan)
    monkeypatch.setattr(transport, "inspect", lambda container_id, timeout: raw)
    channel = object()
    monkeypatch.setattr(
        "hcuopt.deployment.bw20_m1_runtime.JsonLineChannel",
        lambda command: channel,
    )

    assert transport.start(CID, 10) is channel
    assert commands == [("docker", "start", "--attach", "--interactive", CID)]

    raw["HostConfig"]["PidMode"] = "host"
    with pytest.raises(ExecutionSafetyError, match="scope mismatch"):
        transport.start(CID, 10)
