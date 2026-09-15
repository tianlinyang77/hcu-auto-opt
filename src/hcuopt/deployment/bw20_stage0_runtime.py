# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Unregistered BW20 Stage 0 launch policy and read-only PID binding.

No launcher, clock mutation, profile registration or measurement verdict here.
The original Event worker and D lifecycle records keep container-local PIDs;
host telemetry must use the separately verified host PID and start token.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from hcuopt.adapters.bw20_execution import IMAGE_ID
from hcuopt.adapters.execution import FENCING_LABEL, MANAGED_LABEL, RESOURCE_LABEL
from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.deployment.bw20_environment import build_probe_plan
from hcuopt.measurement.nmz36_runtime import _proc_start_token

RESOURCE = "bw20-sglang-0.5.12:hcu:7"
ROOT = "/home/github/hcu-auto-opt-runtime/bw20-stage0"
PASSWD_ASSET = "src/hcuopt/deployment/assets/bw20-passwd"
GROUP_ASSET = "src/hcuopt/deployment/assets/bw20-group"


class ProcessBindingError(ValueError):
    """Missing/ambiguous host identity must not be marked as managed work."""


@dataclass(frozen=True)
class BW20TimingPlan:
    argv: tuple[str, ...]
    container_name: str
    resource_id: str
    fencing_token: int
    source_root: str


def build_timing_plan(target: TargetSpec, *, run_id: UUID, fencing_token: int,
                      resource_id: str = RESOURCE) -> BW20TimingPlan:
    # Reuse the existing frozen host/device/image/mount identity checks.
    build_probe_plan(target)
    if target.inference_image.image_id != IMAGE_ID:
        raise ValueError("unexpected BW20 image identity")
    if not isinstance(run_id, UUID) or type(fencing_token) is not int or fencing_token < 1:
        raise ValueError("a run UUID and positive fencing token are required")
    if resource_id != RESOURCE:
        raise ValueError("wrong BW20 resource scope")
    name = f"hcuopt-bw20-stage0-{run_id}"
    source_root = f"{ROOT}/{run_id}/controller"
    argv = (
        "docker", "run", "--rm", "--interactive", "--pull=never", "--name", name,
        "--label", f"{MANAGED_LABEL}=true", "--label", f"{RESOURCE_LABEL}={RESOURCE}",
        "--label", f"{FENCING_LABEL}={fencing_token}",
        "--network=none", "--ipc=private", "--read-only", "--cap-drop=ALL",
        "--user=1002:1002",
        "--security-opt=no-new-privileges", "--cpuset-cpus=64-79", "--cpuset-mems=4",
        "--memory=4g", "--memory-swap=4g", "--pids-limit=128", "--shm-size=1g",
        "--device=/dev/kfd", "--device=/dev/dri/renderD135",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=1g",
        "--env", "HIP_VISIBLE_DEVICES=0", "--env", "ROCR_VISIBLE_DEVICES=0",
        "--env", "HSA_VISIBLE_DEVICES=0", "--env", "HOME=/tmp",
        "--env", "PYTHONPATH=/workspace/src", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--mount", f"type=bind,src={source_root},dst=/workspace,readonly",
        "--mount", "type=bind,src=/opt/hyhal,dst=/opt/hyhal,readonly",
        "--mount", f"type=bind,src={source_root}/{PASSWD_ASSET},dst=/etc/passwd,readonly",
        "--mount", f"type=bind,src={source_root}/{GROUP_ASSET},dst=/etc/group,readonly",
        "--workdir", "/workspace", "--entrypoint", "python",
        target.inference_image.immutable_reference,
        "-B", "-m", "hcuopt.deployment.bw20_stage0_worker", "--controller",
    )
    # Controller is PID1 and already owns fork/waitpid. No --init or host PID mode.
    return BW20TimingPlan(argv, name, RESOURCE, fencing_token, source_root)


def _read(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as stream:
            value = stream.read(65537)
        if len(value) > 65536:
            raise ProcessBindingError("oversized process observation")
        return value
    except (OSError, UnicodeError) as exc:
        raise ProcessBindingError("process observation unavailable") from exc


def _namespace(path: Path) -> str:
    try:
        value = os.readlink(path / "ns/pid")
    except OSError as exc:
        raise ProcessBindingError("PID namespace unavailable") from exc
    if re.fullmatch(r"pid:\[[0-9]+\]", value) is None:
        raise ProcessBindingError("invalid PID namespace")
    return value


def _nspid(status: str) -> tuple[int, ...]:
    lines = [line for line in status.splitlines() if line.startswith("NSpid:")]
    if len(lines) != 1 or re.fullmatch(r"NSpid:\s+[0-9]+\s+[0-9]+\s*", lines[0]) is None:
        raise ProcessBindingError("expected one isolated PID namespace")
    return tuple(int(value) for value in lines[0].split()[1:])


def _parent(stat: str) -> int:
    try:
        return int(stat[stat.rindex(") ") + 2:].split()[1])
    except (ValueError, IndexError) as exc:
        raise ProcessBindingError("invalid process parent") from exc


def _start(stat: str, pid: int) -> str:
    try:
        return _proc_start_token(stat, pid)
    except (ValueError, RuntimeError) as exc:
        raise ProcessBindingError("invalid process start identity") from exc


def _belongs(cgroup: str, container_id: str) -> bool:
    paths = [line.split(":", 2)[2] for line in cgroup.splitlines() if line.count(":") == 2]
    return any(path.endswith(f"/docker/{container_id}") or
               path.endswith(f"/docker-{container_id}.scope") for path in paths)


def _snapshot(root: Path, pid: int, container_id: str, *, read: Callable,
              namespace_reader: Callable) -> dict:
    path = root / str(pid)
    before = read(path / "stat")
    status = read(path / "status")
    cgroup, namespace = read(path / "cgroup"), namespace_reader(path)
    if not isinstance(namespace, str) or re.fullmatch(r"pid:\[[0-9]+\]", namespace) is None:
        raise ProcessBindingError("invalid PID namespace")
    after = read(path / "stat")
    start = _start(before, pid)
    if start != _start(after, pid) or _parent(before) != _parent(after):
        raise ProcessBindingError("process identity changed during capture")
    if not _belongs(cgroup, container_id):
        raise ProcessBindingError("process belongs to another container")
    return {"host_pid": pid, "start_token": start, "namespace": namespace,
            "nspid": _nspid(status), "parent_pid": _parent(after),
            "stat_before": before, "stat_after": after, "status": status, "cgroup": cgroup}


def _inspect(raw: Mapping, container_id: str, plan: BW20TimingPlan) -> tuple[int, str]:
    try:
        config, state, host = raw["Config"], raw["State"], raw["HostConfig"]
        labels = config["Labels"]
        valid = (raw["Id"] == container_id and raw["Name"] == "/" + plan.container_name
                 and state["Running"] is True and type(state["Pid"]) is int and state["Pid"] > 1
                 and isinstance(state["StartedAt"], str) and bool(state["StartedAt"])
                 and host["PidMode"] in ("", "private")
                 and labels[MANAGED_LABEL] == "true"
                 and labels[RESOURCE_LABEL] == plan.resource_id
                 and labels[FENCING_LABEL] == str(plan.fencing_token))
    except (KeyError, TypeError) as exc:
        raise ProcessBindingError("incomplete container identity") from exc
    if not valid:
        raise ProcessBindingError("container identity or ownership mismatch")
    return state["Pid"], state["StartedAt"]


def capture_process_binding(*, plan: BW20TimingPlan, container_id: str,
                            ready: Mapping, inspect: Callable[[str], Mapping],
                            proc_root: Path = Path("/proc"),
                            read_proc: Callable[[Path], str] | None = None,
                            read_namespace: Callable[[Path], str] | None = None,
                            worker_protocol: str = "hcuopt-stage0-torch-worker-v1") -> dict:
    """Observe a live child; injected inspect must read the trusted local Docker daemon.

    Never rewrite the container's lifecycle records to host PIDs.
    Proc readers may be injected by a trusted SSH transport; this does not require
    installing the Python3.10 product on the Python3.6 target host. Injected readers
    must retain failures and read the target host, not the local controller's /proc.
    This observation alone is neither a lease grant nor a live registry entry.
    Recheck at each telemetry/cleanup boundary and persist raw observations separately.
    """
    if not isinstance(container_id, str) or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise ProcessBindingError("full container ID required")
    if worker_protocol not in ("hcuopt-stage0-torch-worker-v1", "hcuopt-stage0-cpu-rehearsal-v1"):
        raise ProcessBindingError("unsupported process binding protocol")
    child_pid = ready.get("process_id")
    if (ready.get("protocol") != worker_protocol
            or ready.get("event") != "ready" or ready.get("observer_process_id") != 1
            or type(ready.get("observer_process_id")) is not int
            or type(child_pid) is not int or child_pid < 2):
        raise ProcessBindingError("invalid isolated worker ready record")
    child_stat = ready.get("proc_stat_line")
    if not isinstance(child_stat, str) or _parent(child_stat) != 1:
        raise ProcessBindingError("measured child is not owned by PID1 controller")
    start = _start(child_stat, child_pid)
    def read(path):
        try:
            value = (read_proc or _read)(path)
        except (OSError, UnicodeError) as exc:
            raise ProcessBindingError("host procfs transport unavailable") from exc
        if not isinstance(value, str) or len(value) > 65536:
            raise ProcessBindingError("invalid host procfs transport response")
        return value

    def namespace(path):
        try:
            return (read_namespace or _namespace)(path)
        except OSError as exc:
            raise ProcessBindingError("host namespace transport unavailable") from exc

    def snapshot(pid):
        return _snapshot(proc_root, pid, container_id, read=read, namespace_reader=namespace)
    identity = _inspect(inspect(container_id), container_id, plan)
    init = snapshot(identity[0])
    host_namespace = namespace(proc_root / "self")
    if (not isinstance(host_namespace, str)
            or re.fullmatch(r"pid:\[[0-9]+\]", host_namespace) is None):
        raise ProcessBindingError("invalid host PID namespace")
    if init["nspid"] != (identity[0], 1) or init["namespace"] == host_namespace:
        raise ProcessBindingError("controller is not isolated PID1")
    children = read(proc_root / str(identity[0]) / "task" / str(identity[0]) / "children").split()
    if len(children) > 128 or any(not value.isdecimal() or int(value) < 2 for value in children):
        raise ProcessBindingError("invalid child process inventory")
    matches = []
    for pid in children:
        observed = snapshot(int(pid))
        if (observed["nspid"] == (int(pid), child_pid)
                and observed["start_token"] == start and observed["parent_pid"] == identity[0]
                and observed["namespace"] == init["namespace"]):
            matches.append(observed)
    if len(matches) != 1:
        raise ProcessBindingError("measured host identity missing or ambiguous")
    if _inspect(inspect(container_id), container_id, plan) != identity:
        raise ProcessBindingError("container restarted during identity capture")
    final_init = snapshot(identity[0])
    final_child = snapshot(matches[0]["host_pid"])
    for previous, current in ((init, final_init), (matches[0], final_child)):
        if any(previous[key] != current[key] for key in
               ("host_pid", "start_token", "namespace", "nspid", "parent_pid")):
            raise ProcessBindingError("process changed before binding completed")
    return {"schema_version": "bw20-stage0-process-binding-v1", "container_id": container_id,
            "resource_id": plan.resource_id, "fencing_token": plan.fencing_token,
            "container_started_at": identity[1], "container_ready": dict(ready),
            "host_controller": init, "host_measured": matches[0],
            "final_controller": final_init, "final_measured": final_child,
            "worker_protocol": worker_protocol,
            "measurement_authorized": False, "scope": "live_observation_not_lease_authority"}
