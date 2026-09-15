# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Unregistered BW20 M1 container plan and daemon-scope verification.

The caller must stage the frozen controller, evidence/cache directories and the
optional Candidate overlay before creating this plan.  This module never opens
SSH, acquires a lease, starts a container, or registers the M1 profile.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from hcuopt.adapters.execution import FENCING_LABEL, MANAGED_LABEL, RESOURCE_LABEL
from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY, BW20M1Policy
from hcuopt.deployment.bw20_timing_transport import (
    BW20DockerTransport,
    JsonLineChannel,
)
from hcuopt.deployment.nmz36_m1_allocator import ALLOCATOR_MOUNT_TARGET
from hcuopt.domain.errors import ExecutionSafetyError

ROOT = PurePosixPath("/home/github/hcu-auto-opt-runtime/bw20-m1")
CONTROLLER_ROOT = PurePosixPath("/home/github/hcu-auto-opt-runtime/bw20-stage0")
M1_WORKER_MODULE = "hcuopt.measurement.m1_allocator_worker"
M1_WORKER_PROTOCOL = "hcuopt-m1-allocator-worker-v1"
M1Arm = Literal["baseline", "candidate"]


@dataclass(frozen=True, slots=True)
class BW20M1ContainerPlan:
    argv: tuple[str, ...]
    container_name: str
    resource_id: str
    fencing_token: int
    run_id: UUID
    arm: M1Arm
    acquisition_ordinal: int
    source_root: str
    evidence_root: str
    cache_root: str
    artifact_path: str | None
    artifact_hash: str | None


def _inside_run(path: str, run_root: PurePosixPath, name: str) -> str:
    value = PurePosixPath(path)
    if value != run_root / name or str(value) != path:
        raise ValueError(f"BW20 M1 {name} path is outside the fresh run root")
    return path


def build_m1_container_plan(
    target: TargetSpec,
    *,
    run_id: UUID,
    arm: M1Arm,
    acquisition_ordinal: int,
    fencing_token: int,
    artifact_hash: str | None = None,
    policy: BW20M1Policy = BW20_M1_POLICY,
) -> BW20M1ContainerPlan:
    """Build one canonical baseline/Candidate process under a fresh run UUID."""

    policy.validate_target(target)
    if not isinstance(run_id, UUID):
        raise ValueError("BW20 M1 run identity must be a UUID")
    if arm not in {"baseline", "candidate"}:
        raise ValueError("BW20 M1 arm must be baseline or candidate")
    if type(acquisition_ordinal) is not int or acquisition_ordinal < 0:
        raise ValueError("BW20 M1 acquisition ordinal must be nonnegative")
    if type(fencing_token) is not int or fencing_token < 1:
        raise ValueError("BW20 M1 fencing token must be positive")
    if (artifact_hash is None) != (arm == "baseline"):
        raise ValueError("only the Candidate arm requires one Artifact Hash")
    if artifact_hash is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_hash) is None:
        raise ValueError("BW20 M1 Candidate Artifact Hash must be SHA256")

    run_root = ROOT / str(run_id)
    source_root = str(CONTROLLER_ROOT / str(run_id) / "controller")
    evidence_root = _inside_run(str(run_root / "evidence"), run_root, "evidence")
    cache_root = _inside_run(str(run_root / "cache"), run_root, "cache")
    artifact_path = (
        _inside_run(str(run_root / "candidate-overlay.py"), run_root, "candidate-overlay.py")
        if arm == "candidate"
        else None
    )
    container_name = f"hcuopt-bw20-m1-{arm}-a{acquisition_ordinal}-{run_id}"
    argv = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--pull=never",
        "--name",
        container_name,
        "--label",
        f"{MANAGED_LABEL}=true",
        "--label",
        f"{RESOURCE_LABEL}={policy.resource_id}",
        "--label",
        f"{FENCING_LABEL}={fencing_token}",
        "--ipc=private",
        *policy.docker_resource_arguments(),
    ]
    for environment in policy.container_environment():
        argv.extend(("--env", environment))
    argv.extend(
        (
            "--mount",
            f"type=bind,src={source_root},dst=/workspace,readonly",
            "--mount",
            f"type=bind,src={evidence_root},dst=/evidence",
            "--mount",
            f"type=bind,src={cache_root},dst=/cache",
            "--mount",
            "type=bind,src=/opt/hyhal,dst=/opt/hyhal,readonly",
        )
    )
    if artifact_path is not None:
        argv.extend(
            (
                "--mount",
                f"type=bind,src={artifact_path},dst={ALLOCATOR_MOUNT_TARGET},readonly",
            )
        )
    argv.extend(
        (
            "--workdir",
            "/workspace",
            "--entrypoint",
            "python",
            target.inference_image.immutable_reference,
            "-m",
            M1_WORKER_MODULE,
            "--performance-controller",
            "--arm",
            arm,
            "--acquisition-ordinal",
            str(acquisition_ordinal),
            "--profile",
            policy.profile,
            "--harness-name",
            "M1TrustedMeasurementHarness",
            "--harness-version",
            "1",
            "--image-digest",
            target.inference_image.registry_digest,
            "--cache-namespace",
            "/cache",
            "--cache-namespace-id",
            cache_root,
            "--evidence-dir",
            "/evidence",
            "--workload-seed",
            "20260825",
        )
    )
    if artifact_hash is not None:
        argv.extend(("--expected-artifact-hash", artifact_hash))
    return BW20M1ContainerPlan(
        argv=tuple(argv),
        container_name=container_name,
        resource_id=policy.resource_id,
        fencing_token=fencing_token,
        run_id=run_id,
        arm=arm,
        acquisition_ordinal=acquisition_ordinal,
        source_root=source_root,
        evidence_root=evidence_root,
        cache_root=cache_root,
        artifact_path=artifact_path,
        artifact_hash=artifact_hash,
    )


def validate_m1_container(
    raw: Mapping[str, object],
    container_id: str,
    plan: BW20M1ContainerPlan,
    target: TargetSpec,
    *,
    policy: BW20M1Policy = BW20_M1_POLICY,
) -> None:
    """Fail closed unless daemon state exactly matches the canonical M1 plan."""

    policy.validate_target(target)
    try:
        config = raw["Config"]
        host = raw["HostConfig"]
        mounts = raw["Mounts"]
        state = raw["State"]
        if not all(isinstance(value, Mapping) for value in (config, host, state)):
            raise TypeError
        labels = config["Labels"]
        if not isinstance(labels, Mapping) or not isinstance(mounts, list):
            raise TypeError
        required = {
            "NetworkMode": "none",
            "IpcMode": "private",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CpusetCpus": policy.cpu_affinity,
            "CpusetMems": str(policy.numa_node),
            "Memory": 16 * 1024**3,
            "MemorySwap": 16 * 1024**3,
            "PidsLimit": 512,
            "ShmSize": 2 * 1024**3,
        }
        valid = (
            raw["Id"] == container_id
            and raw["Name"] == "/" + plan.container_name
            and raw["Image"] == policy.image_id
            and config["Image"] == target.inference_image.immutable_reference
            and config["User"] == "65534:65534"
            and config["Entrypoint"] == ["python"]
            and config["Cmd"] == list(plan.argv[plan.argv.index("-m") :])
            and labels.get(MANAGED_LABEL) == "true"
            and labels.get(RESOURCE_LABEL) == plan.resource_id
            and labels.get(FENCING_LABEL) == str(plan.fencing_token)
            and host.get("PidMode") in ("", "private")
            and host.get("AutoRemove") is True
            and host.get("RestartPolicy", {}).get("Name") == "no"
            and host.get("CapDrop") == ["ALL"]
            and not host.get("CapAdd")
            and host.get("SecurityOpt")
            in (["no-new-privileges"], ["no-new-privileges:true"])
            and not host.get("DeviceRequests")
            and not host.get("DeviceCgroupRules")
            and not host.get("VolumesFrom")
            and state.get("Running") is False
            and all(host.get(key) == value for key, value in required.items())
        )
        devices = sorted(
            (item["PathOnHost"], item["PathInContainer"], item["CgroupPermissions"])
            for item in host["Devices"]
        )
        valid = valid and devices == [
            (policy.render_node, policy.render_node, "rwm"),
            ("/dev/kfd", "/dev/kfd", "rwm"),
        ]
        expected_mounts = {
            ("bind", plan.source_root, "/workspace", False),
            ("bind", plan.evidence_root, "/evidence", True),
            ("bind", plan.cache_root, "/cache", True),
            ("bind", "/opt/hyhal", "/opt/hyhal", False),
        }
        if plan.artifact_path is not None:
            expected_mounts.add(
                ("bind", plan.artifact_path, ALLOCATOR_MOUNT_TARGET, False)
            )
        observed_mounts = {
            (item["Type"], item["Source"], item["Destination"], item["RW"])
            for item in mounts
        }
        valid = valid and observed_mounts == expected_mounts
        environment = config["Env"]
        for expected in policy.container_environment():
            name = expected.split("=", 1)[0]
            valid = valid and [
                item for item in environment if item.startswith(name + "=")
            ] == [expected]
        valid = valid and host.get("Tmpfs") == {
            "/tmp": "rw,exec,nosuid,nodev,size=4g"
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionSafetyError("incomplete BW20 M1 daemon scope evidence") from exc
    if not valid:
        raise ExecutionSafetyError("BW20 M1 daemon container scope mismatch")


class BW20M1DockerTransport(BW20DockerTransport):
    """Exact-CID remote transport that validates M1 scope before start/delete."""

    def create(self, plan: BW20M1ContainerPlan, timeout: float) -> str:
        if not isinstance(plan, BW20M1ContainerPlan):
            raise ValueError("BW20 M1 transport requires its canonical plan type")
        canonical = build_m1_container_plan(
            self.target,
            run_id=plan.run_id,
            arm=plan.arm,
            acquisition_ordinal=plan.acquisition_ordinal,
            fencing_token=plan.fencing_token,
            artifact_hash=plan.artifact_hash,
        )
        if plan != canonical:
            raise ValueError("noncanonical BW20 M1 container plan")
        return self._create(plan, timeout)

    def start(self, container_id: str, timeout: float) -> JsonLineChannel:
        raw = self.inspect(container_id, timeout)
        if raw is None:
            raise RuntimeError("created BW20 M1 container disappeared")
        self._ownership(raw, container_id)
        validate_m1_container(raw, container_id, self.owned[container_id], self.target)
        command = self.runner.wrapped_argv(
            ("docker", "start", "--attach", "--interactive", container_id)
        )
        return JsonLineChannel(command)


__all__ = [
    "BW20M1ContainerPlan",
    "BW20M1DockerTransport",
    "CONTROLLER_ROOT",
    "M1_WORKER_PROTOCOL",
    "ROOT",
    "build_m1_container_plan",
    "validate_m1_container",
]
