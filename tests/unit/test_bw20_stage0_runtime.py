# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from copy import deepcopy
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest

from hcuopt.adapters.execution import FENCING_LABEL, MANAGED_LABEL, RESOURCE_LABEL
from hcuopt.deployment import bw20_stage0_runtime as runtime
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
TARGET = load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")
RUN = UUID("11111111-2222-4333-8444-555555555555")
CID = "a" * 64
NAMESPACE_READER = runtime._namespace


def plan():
    return runtime.build_timing_plan(TARGET, run_id=RUN, fencing_token=7)


def stat(pid, parent, start):
    return f"{pid} (worker (HCU)) S {parent} " + "0 " * 17 + f"{start} 0 0\n"


@pytest.fixture
def observations(tmp_path, monkeypatch):
    def process(pid, parent, start, local_pid, namespace="pid:[22]"):
        root = tmp_path / str(pid)
        root.mkdir()
        (root / "stat").write_text(stat(pid, parent, start))
        (root / "status").write_text(f"Name:\tworker\nNSpid:\t{pid}\t{local_pid}\n")
        (root / "cgroup").write_text(f"0::/system.slice/docker-{CID}.scope\n")
        (root / "namespace").write_text(namespace)
    process(100, 99, 1000, 1)
    process(101, 100, 2000, 2)
    process(1, 0, 1, 1, namespace="pid:[11]")
    (tmp_path / "self").mkdir()
    (tmp_path / "self/namespace").write_text("pid:[11]")
    children = tmp_path / "100/task/100"
    children.mkdir(parents=True)
    (children / "children").write_text("101\n")
    monkeypatch.setattr(runtime, "_namespace", lambda path: (path / "namespace").read_text())
    inspect = {
        "Id": CID, "Name": "/" + plan().container_name,
        "State": {"Running": True, "Pid": 100, "StartedAt": "2026-09-10T01:00:00Z"},
        "HostConfig": {"PidMode": "private"},
        "Config": {"Labels": {MANAGED_LABEL: "true", RESOURCE_LABEL: runtime.RESOURCE,
                               FENCING_LABEL: "7"}},
    }
    ready = {"protocol": "hcuopt-stage0-torch-worker-v1", "event": "ready",
             "process_id": 2, "observer_process_id": 1, "proc_stat_line": stat(2, 1, 2000)}
    return tmp_path, inspect, ready


def capture(observations, **changes):
    root, inspection, ready = observations
    kwargs = dict(plan=plan(), container_id=CID, ready=ready,
                  inspect=lambda cid: deepcopy(inspection), proc_root=root)
    kwargs.update(changes)
    return runtime.capture_process_binding(**kwargs)


def test_command_is_pure_single_device_private_namespace(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("must not launch"))
    result = plan()
    args = result.argv
    assert tuple(a for a in args if a.startswith("--device=")) == (
        "--device=/dev/kfd", "--device=/dev/dri/renderD135")
    # Docker defaults to an isolated PID namespace; --pid=private is invalid.
    assert not any(arg.startswith("--pid=") for arg in args)
    assert "--ipc=private" in args
    assert "--network=none" in args and "--read-only" in args and "--cap-drop=ALL" in args
    assert "--pid=host" not in args and "--privileged" not in args and "--init" not in args
    for name in ("HIP", "ROCR", "HSA"):
        assert f"{name}_VISIBLE_DEVICES=0" in args
    assert "--memory=4g" in args and "--memory-swap=4g" in args
    assert "--pids-limit=128" in args
    assert args[-3:] == ("-m", "hcuopt.deployment.bw20_stage0_worker", "--controller")
    mounts = [args[i + 1] for i, item in enumerate(args) if item == "--mount"]
    assert len(mounts) == 4 and all(item.endswith(",readonly") for item in mounts)
    assert (
        f"type=bind,src={result.source_root}/{runtime.PASSWD_ASSET},"
        "dst=/etc/passwd,readonly"
    ) in mounts
    assert (
        f"type=bind,src={result.source_root}/{runtime.GROUP_ASSET},"
        "dst=/etc/group,readonly"
    ) in mounts
    assert TARGET.stage0_status == "pending" and TARGET.automatic_release_allowed is False


@pytest.mark.parametrize("token", [True, 0, -1, "7", 1.5])
def test_invalid_fence_rejected(token):
    with pytest.raises(ValueError):
        runtime.build_timing_plan(TARGET, run_id=RUN, fencing_token=token)


def test_wrong_resource_and_target_rejected():
    with pytest.raises(ValueError, match="resource"):
        runtime.build_timing_plan(TARGET, run_id=RUN, fencing_token=7, resource_id="hcu-7")
    old = load_target(ROOT / "config/targets/nmz36-sglang-0.5.12.yaml")
    with pytest.raises(ValueError):
        runtime.build_timing_plan(old, run_id=RUN, fencing_token=7)
    wrong = TARGET.model_copy(update={"inference_image": TARGET.inference_image.model_copy(
        update={"image_id": "sha256:" + "f" * 64})})
    with pytest.raises(ValueError, match="image"):
        runtime.build_timing_plan(wrong, run_id=RUN, fencing_token=7)


def test_binding_preserves_original_container_lifecycle(observations):
    before = deepcopy(observations[2])
    result = capture(observations)
    assert result["container_ready"] == before == observations[2]
    assert result["host_measured"]["host_pid"] == 101
    assert result["host_measured"]["start_token"] == "linux-proc-startticks:2000"
    assert result["container_ready"]["process_id"] == 2
    assert result["host_controller"]["nspid"] == (100, 1)
    assert result["measurement_authorized"] is False


def test_cpu_protocol_requires_explicit_opt_in(observations):
    protocol = "hcuopt-stage0-cpu-rehearsal-v1"
    observations[2]["protocol"] = protocol
    with pytest.raises(runtime.ProcessBindingError):
        capture(observations)
    result = capture(observations, worker_protocol=protocol)
    assert result["worker_protocol"] == protocol
    assert result["measurement_authorized"] is False


def test_cgroup_v1_membership_supported_without_namespace_changes(observations):
    for pid in (100, 101):
        (observations[0] / str(pid) / "cgroup").write_text(f"8:memory:/docker/{CID}\n")
    assert capture(observations)["host_measured"]["host_pid"] == 101


def test_injected_host_readers_do_not_read_controller_proc(observations, monkeypatch):
    paths = []
    def read(path):
        paths.append(str(path))
        return (observations[0] / path.relative_to("/proc")).read_text()
    def namespace(path):
        return read(path / "namespace")
    def forbidden(*args):
        pytest.fail("must not read local controller procfs")
    monkeypatch.setattr(runtime, "_read", forbidden)
    monkeypatch.setattr(runtime, "_namespace", forbidden)
    result = capture(observations, proc_root=PurePosixPath("/proc"),
                     read_proc=read, read_namespace=namespace)
    assert result["host_measured"]["host_pid"] == 101
    assert paths and all(path.startswith("/proc/") for path in paths)


def test_invalid_injected_namespace_rejected(observations):
    with pytest.raises(runtime.ProcessBindingError, match="namespace"):
        capture(observations, read_namespace=lambda path: "")


def test_shared_host_namespace_rejected_even_with_private_inspect(observations):
    (observations[0] / "100/namespace").write_text("pid:[11]")
    with pytest.raises(runtime.ProcessBindingError, match="not isolated"):
        capture(observations)


@pytest.mark.parametrize("key,value", [
    ("process_id", True), ("process_id", 1), ("observer_process_id", True),
    ("observer_process_id", 2), ("event", "other"), ("protocol", "other"),
    ("proc_stat_line", stat(2, 5, 2000)), ("proc_stat_line", "2 (x) S 1"),
])
def test_invalid_ready_record_rejected(observations, key, value):
    observations[2][key] = value
    with pytest.raises(runtime.ProcessBindingError):
        capture(observations)


@pytest.mark.parametrize("container_id", ["a" * 12, "A" * 64, "../1", None, True])
def test_requires_full_exact_container_id(observations, container_id):
    with pytest.raises(runtime.ProcessBindingError):
        capture(observations, container_id=container_id)


@pytest.mark.parametrize("filename,content", [
    ("status", "Name: worker\n"),
    ("status", "NSpid: 101 99\n"),
    ("status", "NSpid: 101 2 5\n"),
    ("status", "NSpid: 101 2\nNSpid: 101 2\n"),
    ("cgroup", "0::/system.slice/docker-" + "b" * 64 + ".scope\n"),
    ("namespace", "pid:[33]"),
    ("stat", stat(101, 100, 9999)),
    ("stat", stat(101, 50, 2000)),
])
def test_unmatched_pid_never_becomes_managed(observations, filename, content):
    (observations[0] / "101" / filename).write_text(content)
    with pytest.raises(runtime.ProcessBindingError):
        capture(observations)


@pytest.mark.parametrize("change", ["fence", "resource", "id", "host_pid", "stopped", "init"])
def test_wrong_container_identity_rejected(observations, change):
    inspection = observations[1]
    if change == "fence":
        inspection["Config"]["Labels"][FENCING_LABEL] = "6"
    elif change == "resource":
        inspection["Config"]["Labels"][RESOURCE_LABEL] = "hcu-7"
    elif change == "id":
        inspection["Id"] = "b" * 64
    elif change == "host_pid":
        inspection["HostConfig"]["PidMode"] = "host"
    elif change == "stopped":
        inspection["State"]["Running"] = False
    else:
        inspection["State"]["Pid"] = True
    with pytest.raises(runtime.ProcessBindingError):
        capture(observations)


def test_pid_reuse_during_capture_rejected(observations):
    calls = 0
    def inspect(cid):
        nonlocal calls
        calls += 1
        if calls == 2:
            (observations[0] / "101/stat").write_text(stat(101, 100, 9999))
        return deepcopy(observations[1])
    with pytest.raises(runtime.ProcessBindingError, match="changed"):
        capture(observations, inspect=inspect)


def test_container_restart_during_capture_rejected(observations):
    calls = 0
    def inspect(cid):
        nonlocal calls
        calls += 1
        result = deepcopy(observations[1])
        if calls == 2:
            result["State"]["StartedAt"] = "2026-09-10T02:00:00Z"
        return result
    with pytest.raises(runtime.ProcessBindingError, match="restarted"):
        capture(observations, inspect=inspect)


def test_missing_and_duplicate_children_fail_closed(observations):
    children = observations[0] / "100/task/100/children"
    for value in ("", "101 101", "-1", "102"):
        children.write_text(value)
        with pytest.raises(runtime.ProcessBindingError):
            capture(observations)


def test_permission_error_does_not_mark_managed(observations, monkeypatch):
    def denied(path):
        raise PermissionError()
    monkeypatch.setattr(runtime.os, "readlink", denied)
    # Exercise the real namespace reader independently of the fixture mock.
    with pytest.raises(runtime.ProcessBindingError, match="namespace unavailable"):
        NAMESPACE_READER(observations[0])
    (observations[0] / "101/status").unlink()
    with pytest.raises(runtime.ProcessBindingError, match="unavailable"):
        capture(observations)


def test_procfs_permission_error_is_explicit(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError()
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(runtime.ProcessBindingError, match="unavailable"):
        runtime._read(tmp_path / "stat")
