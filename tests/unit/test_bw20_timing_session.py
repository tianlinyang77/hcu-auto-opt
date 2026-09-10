# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from hcuopt.adapters.bw20_execution import IMAGE_ID
from hcuopt.adapters.execution import FENCING_LABEL, MANAGED_LABEL, RESOURCE_LABEL
from hcuopt.deployment.bw20_environment import IMAGE
from hcuopt.deployment.bw20_stage0_runtime import build_timing_plan
from hcuopt.deployment.bw20_timing_session import (
    BW20TimingSession,
    BW20TimingWorkloadFactory,
    TimingSessionError,
)
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.nmz36_runtime import (
    DockerFormalStage0Workload,
    DockerProcessLifecycleRecorder,
)
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
TARGET = load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")
PLAN = build_timing_plan(
    TARGET, run_id=UUID("11111111-2222-4333-8444-555555555555"), fencing_token=7)
CID = "a" * 64
STAT = "2 (worker) S 1 " + "0 " * 17 + "2000 0 0"
READY = {"protocol": "hcuopt-stage0-torch-worker-v1", "event": "ready", "process_id": 2,
         "observer_process_id": 1, "proc_stat_line": STAT,
         "device_identity": {"pci": "0000:b1:00.0", "architecture": "gfx936",
                             "logical_device_index": 0}}


def raw_container():
    return {
        "Id": CID, "Name": "/" + PLAN.container_name, "Image": IMAGE_ID,
        "State": {"Running": False},
        "Config": {"Image": IMAGE, "Entrypoint": ["python"], "Cmd": list(PLAN.argv[-4:]),
                   "Labels": {MANAGED_LABEL: "true", RESOURCE_LABEL: PLAN.resource_id,
                              FENCING_LABEL: "7"},
                   "Env": [PLAN.argv[i + 1] for i, v in enumerate(PLAN.argv) if v == "--env"]},
        "HostConfig": {
            "AutoRemove": True, "RestartPolicy": {"Name": "no"},
            "NetworkMode": "none", "PidMode": "private", "IpcMode": "private",
            "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"], "CpusetCpus": "64-79", "CpusetMems": "4",
            "Memory": 4 * 1024**3, "MemorySwap": 4 * 1024**3, "PidsLimit": 128,
            "ShmSize": 1024**3, "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=1g"},
            "Devices": [{"PathOnHost": p, "PathInContainer": p, "CgroupPermissions": "rwm"}
                        for p in ("/dev/kfd", "/dev/dri/renderD135")]},
        "Mounts": [{"Type": "bind", "Source": PLAN.source_root,
                    "Destination": "/workspace", "RW": False},
                   {"Type": "bind", "Source": "/opt/hyhal", "Destination": "/opt/hyhal",
                    "RW": False}],
    }


class Channel:
    def __init__(self):
        self.ready = deepcopy(READY)
        self.requests = []
        self.aborted = False
        self.failure = None
        self.returncode = None
        self.close_status = 0

    def receive(self, timeout):
        return self.ready

    def request(self, payload, timeout):
        self.requests.append(dict(payload))
        if self.failure:
            raise self.failure
        if payload["op"] == "close":
            self.returncode = 0
            return {**self.ready, "event": "closing", "waitpid_result_pid": 2,
                    "wait_status": self.close_status}
        return {"protocol": READY["protocol"], "event": "synchronized"}

    def wait(self, timeout):
        return self.returncode

    def poll(self):
        return self.returncode

    def abort(self):
        self.aborted = True


class Transport:
    def __init__(self):
        self.raw = raw_container()
        self.channel = Channel()
        self.calls = []
        self.exists = False
        self.remove_failure = False
        self.create_result = CID

    def create(self, plan, timeout):
        self.calls.append(("create", plan.container_name))
        self.exists = True
        return self.create_result

    def inspect(self, cid, timeout):
        self.calls.append(("inspect", cid))
        return deepcopy(self.raw) if self.exists else None

    def start(self, cid, timeout):
        self.calls.append(("start", cid))
        self.raw["State"]["Running"] = True
        return self.channel

    def remove(self, cid, timeout):
        self.calls.append(("remove", cid))
        if self.remove_failure:
            raise OSError("unreachable daemon")
        self.exists = False


def make_session(transport=None, **kwargs):
    transport = transport or Transport()
    def bind(cid, ready):
        return {"container_id": cid, "resource_id": PLAN.resource_id,
                "fencing_token": 7, "container_ready": deepcopy(ready)}
    values = dict(plan=PLAN, transport=transport, assert_staging=lambda plan: None,
                  assert_lease=lambda: None, cancelled=lambda: False, bind_process=bind)
    values.update(kwargs)
    return BW20TimingSession(**values), transport


def test_session_reuses_original_workload_and_lifecycle():
    session, transport = make_session()
    session.open()
    workload = DockerFormalStage0Workload(session, probe_type=Stage0ProbeType.NOISE)
    recorder = DockerProcessLifecycleRecorder()
    started = recorder.record_started(workload, restart_ordinal=0, captured_monotonic_ns=100)
    assert started.process_id == 2 and session.is_alive()
    workload.synchronize()
    workload.close()
    reaped = recorder.record_reaped(workload, restart_ordinal=0, captured_monotonic_ns=200)
    assert reaped.waitpid_result_pid == 2 and reaped.wait_status == 0
    assert session.cleanup_complete and not session.is_alive()
    assert transport.channel.aborted
    assert [v for k, v in transport.calls if k == "remove"] == [CID]
    assert len(session.identity_observations) == 4


@pytest.mark.parametrize("guard", ["assert_staging", "assert_lease"])
def test_prelaunch_guard_failure_creates_nothing(guard):
    def reject(*args):
        raise RuntimeError("not authorized")
    session, transport = make_session(**{guard: reject})
    with pytest.raises(RuntimeError):
        session.open()
    assert not transport.calls and session.cleanup_complete


@pytest.mark.parametrize("key,value", [
    ("Privileged", True), ("NetworkMode", "host"), ("PidMode", "host"),
    ("CapAdd", ["SYS_ADMIN"]), ("ReadonlyRootfs", False), ("Memory", 0),
    ("DeviceCgroupRules", ["a *:* rwm"]),
    ("AutoRemove", False), ("RestartPolicy", {"Name": "always"}),
])
def test_daemon_scope_checked_before_start(key, value):
    transport = Transport()
    transport.raw["HostConfig"][key] = value
    session, _ = make_session(transport)
    with pytest.raises(TimingSessionError):
        session.open()
    assert not any(k == "start" for k, _ in transport.calls)
    assert not session.cleanup_complete  # Untrusted object retained, never deleted by name.


def test_missing_device_attestation_removes_only_own_container():
    transport = Transport()
    del transport.channel.ready["device_identity"]
    session, _ = make_session(transport)
    with pytest.raises(TimingSessionError, match="attestation"):
        session.open()
    assert session.cleanup_complete and not transport.exists


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), RuntimeError("disconnect")])
def test_request_failure_cleans_owned_container(failure):
    session, transport = make_session()
    session.open()
    transport.channel.failure = failure
    with pytest.raises(type(failure)):
        session.request({"op": "synchronize"})
    assert session.cleanup_complete and not transport.exists


@pytest.mark.parametrize("kind", ["lease", "cancel", "budget"])
def test_lost_authority_stops_before_next_command(kind):
    state = {"lost": False, "time": 0}
    def lease():
        if kind == "lease" and state["lost"]:
            raise RuntimeError("lease lost")
    session, transport = make_session(assert_lease=lease,
        cancelled=lambda: kind == "cancel" and state["lost"], monotonic=lambda: state["time"])
    session.open()
    state.update(lost=True, time=481 if kind == "budget" else 0)
    with pytest.raises((RuntimeError, TimeoutError)):
        session.request({"op": "measure"})
    assert not transport.channel.requests and session.cleanup_complete


def test_unknown_creation_does_not_remove_by_name():
    transport = Transport()
    transport.create_result = "short-id"
    session, _ = make_session(transport)
    with pytest.raises(TimingSessionError, match="authoritative"):
        session.open()
    assert not session.cleanup_complete
    assert not any(k == "remove" for k, _ in transport.calls)


def test_cleanup_failure_never_reports_complete():
    session, transport = make_session()
    session.open()
    transport.remove_failure = True
    session.force_close()
    assert not session.cleanup_complete
    with pytest.raises(TimingSessionError, match="unconfirmed"):
        session.close()


def test_invalid_waitpid_status_is_not_successful_lifecycle():
    session, transport = make_session()
    session.open()
    transport.channel.close_status = 256
    with pytest.raises(TimingSessionError, match="reaping"):
        session.close()
    assert session.exit_observation is None and session.cleanup_complete


def test_binding_failure_is_not_a_measured_sample():
    session, transport = make_session(bind_process=lambda cid, ready: {})
    with pytest.raises(TimingSessionError, match="binding"):
        session.open()
    assert session.cleanup_complete and not transport.channel.requests


def test_authority_loss_after_response_does_not_return_sample():
    state = {"lost": False}
    def lease():
        if state["lost"]:
            raise RuntimeError("lease lost during request")
    session, transport = make_session(assert_lease=lease)
    session.open()
    original = transport.channel.request
    def request(payload, timeout):
        result = original(payload, timeout)
        state["lost"] = True
        return result
    transport.channel.request = request
    with pytest.raises(RuntimeError, match="during request"):
        session.request({"op": "synchronize"})
    assert session.cleanup_complete


def test_foreign_identity_is_not_deleted_during_cleanup():
    session, transport = make_session()
    session.open()
    transport.raw["Config"]["Labels"][FENCING_LABEL] = "8"
    session.force_close()
    assert not session.cleanup_complete
    assert not any(k == "remove" for k, _ in transport.calls)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, 0, -1])
def test_invalid_timeout_fails_before_request(value):
    session, transport = make_session()
    session.open()
    with pytest.raises(ValueError):
        session.request({"op": "measure"}, timeout_seconds=value)
    assert not transport.channel.requests and session.cleanup_complete


def test_factory_connects_original_timer_and_rejects_reused_identity():
    created = []
    def sessions(role, restart):
        session, transport = make_session()
        created.append((role, restart, session, transport))
        return session
    factory = BW20TimingWorkloadFactory(sessions)
    timer = factory.timer()
    timer.synchronize()
    assert created[0][0:2] == ("timer", None)
    with pytest.raises(TimingSessionError, match="fresh"):
        factory.workload(Stage0ProbeType.NOISE, 0)
    assert not created[1][3].calls
    assert factory.force_close()


def test_factory_connects_original_workload_and_preserves_cleanup_failure():
    session, transport = make_session()
    factory = BW20TimingWorkloadFactory(lambda role, restart: session)
    workload = factory.workload(Stage0ProbeType.NOISE, 0)
    assert workload.process_identity().pid == 2
    transport.remove_failure = True
    assert factory.force_close() is False
