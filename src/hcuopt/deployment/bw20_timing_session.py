# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Unregistered lease-guarded session for the original Stage 0 workload interfaces.

All I/O is injected by the deployment. No default SSH connection or profile binding.
Creating a session is not resource authorization; trusted staging and lease guards
are mandatory, and the owning Worker remains responsible for ledger settlement.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from hcuopt.adapters.bw20_execution import IMAGE_ID
from hcuopt.adapters.execution import FENCING_LABEL, MANAGED_LABEL, RESOURCE_LABEL
from hcuopt.deployment.bw20_environment import IMAGE
from hcuopt.deployment.bw20_stage0_runtime import (
    GROUP_ASSET,
    PASSWD_ASSET,
    BW20TimingPlan,
    _start,
)
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.nmz36_runtime import DockerFormalStage0Workload, DockerTorchEventTimer


class TimingSessionError(RuntimeError):
    pass


class TimingChannel(Protocol):
    def receive(self, timeout: float) -> Mapping: ...
    def request(self, payload: Mapping, timeout: float) -> Mapping: ...
    def wait(self, timeout: float) -> int: ...
    def poll(self) -> int | None: ...
    def abort(self) -> None: ...


class TimingTransport(Protocol):
    def create(self, plan: BW20TimingPlan, timeout: float) -> str: ...
    def inspect(self, container_id: str, timeout: float) -> Mapping | None: ...
    def start(self, container_id: str, timeout: float) -> TimingChannel: ...
    def remove(self, container_id: str, timeout: float) -> None: ...


def validate_container(raw: Mapping, cid: str, plan: BW20TimingPlan) -> None:
    """Validate daemon-observed identity and sandbox before starting or deleting."""
    try:
        config, host = raw["Config"], raw["HostConfig"]
        labels = config["Labels"]
        required = {
            "NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
            "CpusetCpus": "64-79", "CpusetMems": "4", "Memory": 4 * 1024**3,
            "MemorySwap": 4 * 1024**3, "PidsLimit": 128, "ShmSize": 1024**3,
        }
        valid = (raw["Id"] == cid and raw["Name"] == "/" + plan.container_name
                 and raw["Image"] == IMAGE_ID and config["Image"] == IMAGE
                 and config["User"] == "1002:1002"
                 and labels[MANAGED_LABEL] == "true"
                 and labels[RESOURCE_LABEL] == plan.resource_id
                 and labels[FENCING_LABEL] == str(plan.fencing_token)
                 and host["PidMode"] in ("", "private") and host["IpcMode"] == "private"
                 and host["AutoRemove"] is True
                 and host["RestartPolicy"]["Name"] == "no"
                 and all(host.get(key) == value for key, value in required.items())
                 and host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
                 and host["SecurityOpt"] in (["no-new-privileges"], ["no-new-privileges:true"])
                 and not host.get("DeviceRequests") and not host.get("VolumesFrom")
                 and not host.get("DeviceCgroupRules")
                 and config["Entrypoint"] == ["python"]
                 and config["Cmd"] == list(plan.argv[-4:]))
        devices = [(d["PathOnHost"], d["PathInContainer"], d["CgroupPermissions"])
                   for d in host["Devices"]]
        valid = valid and sorted(devices) == [
            ("/dev/dri/renderD135", "/dev/dri/renderD135", "rwm"), ("/dev/kfd", "/dev/kfd", "rwm")]
        mounts = [(m["Type"], m["Source"], m["Destination"], m["RW"]) for m in raw["Mounts"]]
        valid = valid and len(mounts) == 4 and set(mounts) == {
            ("bind", plan.source_root, "/workspace", False),
            ("bind", "/opt/hyhal", "/opt/hyhal", False),
            ("bind", f"{plan.source_root}/{PASSWD_ASSET}", "/etc/passwd", False),
            ("bind", f"{plan.source_root}/{GROUP_ASSET}", "/etc/group", False)}
        valid = valid and host["Tmpfs"] == {"/tmp": "rw,nosuid,nodev,noexec,size=1g"}
        env = config["Env"]
        for name, value in (("HIP_VISIBLE_DEVICES", "0"), ("ROCR_VISIBLE_DEVICES", "0"),
                            ("HSA_VISIBLE_DEVICES", "0"), ("HOME", "/tmp"),
                            ("PYTHONPATH", "/workspace/src"), ("PYTHONDONTWRITEBYTECODE", "1")):
            valid = valid and [entry for entry in env if entry.startswith(name + "=")] == [
                name + "=" + value]
    except (KeyError, TypeError, ValueError) as exc:
        raise TimingSessionError("incomplete daemon scope evidence") from exc
    if not valid:
        raise TimingSessionError("daemon container scope mismatch")


class BW20TimingSession:
    def __init__(self, *, plan: BW20TimingPlan, transport: TimingTransport,
                 assert_staging: Callable[[BW20TimingPlan], None],
                 assert_lease: Callable[[], None],
                 bind_process: Callable[[str, Mapping], Mapping],
                 cancelled: Callable[[], bool], budget_seconds: float = 480,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if type(budget_seconds) not in (int, float) or not 0 < budget_seconds <= 480:
            raise ValueError("timing session budget must be within 480 seconds")
        self.plan, self.transport = plan, transport
        self.assert_staging, self.assert_lease = assert_staging, assert_lease
        self.bind_process, self.cancelled, self.monotonic = bind_process, cancelled, monotonic
        self.deadline = monotonic() + budget_seconds
        self.container_id = None
        self.channel = None
        self.start_observation = self.exit_observation = None
        self.identity_observations = []
        self.cache_receipts = []
        self.cleanup_complete = False
        self.create_attempted = False
        self.closed = False

    def _guard(self) -> float:
        if self.closed or self.cancelled():
            raise TimingSessionError("session closed or cancelled")
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("timing session budget exhausted")
        if self.assert_lease() is not None:
            raise TimingSessionError("lease guard must raise on failure")
        remaining = self.deadline - self.monotonic()
        if remaining <= 0 or self.cancelled():
            raise TimingSessionError("budget exhausted or cancelled during lease check")
        return min(60.0, remaining)

    def _binding(self) -> None:
        value = self.bind_process(self.container_id, self.start_observation)
        if (value.get("container_id") != self.container_id
                or value.get("resource_id") != self.plan.resource_id
                or value.get("fencing_token") != self.plan.fencing_token
                or value.get("container_ready") != self.start_observation):
            raise TimingSessionError("host process binding mismatch")
        self.identity_observations.append(value)

    def open(self) -> BW20TimingSession:
        if self.create_attempted or self.closed:
            raise TimingSessionError("session cannot be restarted")
        try:
            self._guard()
            if self.assert_staging(self.plan) is not None:
                raise TimingSessionError("staging guard must raise on failure")
            timeout = self._guard()
            self.create_attempted = True
            cid = self.transport.create(self.plan, timeout)
            if not isinstance(cid, str) or re.fullmatch(r"[0-9a-f]{64}", cid) is None:
                raise TimingSessionError("creation has no authoritative container ID")
            self.container_id = cid
            raw = self.transport.inspect(cid, self._guard())
            if raw is None:
                raise TimingSessionError("created container disappeared")
            validate_container(raw, cid, self.plan)
            if raw["State"]["Running"] is not False:
                raise TimingSessionError("container ran before scope verification")
            self.channel = self.transport.start(cid, self._guard())
            ready = dict(self.channel.receive(self._guard()))
            if (ready.get("protocol") != "hcuopt-stage0-torch-worker-v1"
                    or ready.get("event") != "ready"
                    or ready.get("device_identity") != {
                        "pci": "0000:b1:00.0", "architecture": "gfx936",
                        "logical_device_index": 0}):
                raise TimingSessionError("measured child device attestation missing")
            self.start_observation = ready
            self.process_id = ready["process_id"]
            self.process_start_token = _start(ready["proc_stat_line"], self.process_id)
            self._binding()
            self._guard()
            return self
        except BaseException:
            self.force_close()
            raise

    def request(self, payload: Mapping, *, timeout_seconds: float = 60) -> Mapping:
        if self.channel is None or self.start_observation is None:
            raise TimingSessionError("session is not open")
        try:
            if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60:
                raise ValueError("request timeout must be within 60 seconds")
            timeout = min(timeout_seconds, self._guard())
            if timeout <= 0:
                raise ValueError("request timeout must be positive")
            self._binding()
            result = self.channel.request(payload, timeout)
            self._guard()
            if (result.get("protocol") != "hcuopt-stage0-torch-worker-v1"
                    or result.get("event") == "error"):
                raise TimingSessionError("invalid worker response")
            if payload.get("op") != "close":
                self._binding()
            if payload.get("op") == "measure":
                receipt = result.get("cache_receipt")
                if not isinstance(receipt, Mapping):
                    raise TimingSessionError("missing measured cache receipt")
                expected = {
                    "schema_version": "stage0-allocator-cache-receipt-v1",
                    "process_id": self.process_id, "sequence": len(self.cache_receipts) + 1,
                    "method": "torch.cuda.synchronize/empty_cache/synchronize",
                    "scope": "pytorch_unused_allocator_blocks_only",
                    "hardware_cache_flushed": False,
                }
                start, end = (receipt.get("started_monotonic_ns"),
                              receipt.get("finished_monotonic_ns"))
                sample_start = result.get("started_monotonic_ns")
                if (any(receipt.get(key) != value for key, value in expected.items())
                        or type(receipt.get("process_id")) is not int
                        or type(receipt.get("sequence")) is not int
                        or receipt.get("hardware_cache_flushed") is not False
                        or type(start) is not int or type(end) is not int
                        or type(sample_start) is not int or not 0 <= start < end <= sample_start
                        or result.get("process_id") != self.process_id
                        or result.get("segment") != payload.get("segment")
                        or result.get("batch_iterations") != payload.get("iterations")):
                    raise TimingSessionError("invalid measured cache receipt binding")
                self.cache_receipts.append({"receipt": dict(receipt),
                    "request": dict(payload), "sample": dict(result),
                    "host_binding": self.identity_observations[-1]})
            return result
        except BaseException:
            self.force_close()
            raise

    def close(self) -> None:
        if self.closed:
            if not self.cleanup_complete:
                raise TimingSessionError("cleanup remains unconfirmed")
            return
        try:
            value = self.request({"op": "close"}, timeout_seconds=30)
            if (value.get("event") != "closing"
                    or type(value.get("process_id")) is not int
                    or type(value.get("observer_process_id")) is not int
                    or type(value.get("waitpid_result_pid")) is not int
                    or value.get("process_id") != self.process_id
                    or value.get("observer_process_id") != 1
                    or value.get("waitpid_result_pid") != self.process_id
                    or type(value.get("wait_status")) is not int or value["wait_status"] != 0
                    or _start(value["proc_stat_line"], self.process_id)
                    != self.process_start_token):
                raise TimingSessionError("invalid raw child reaping evidence")
            if self.channel.wait(self._guard()) != 0:
                raise TimingSessionError("container channel exited unsuccessfully")
            self.exit_observation = dict(value)
        finally:
            self.force_close()
        if not self.cleanup_complete:
            raise TimingSessionError("cleanup remains unconfirmed")

    def force_close(self) -> None:
        self.closed = True
        self.cleanup_complete = not self.create_attempted
        try:
            if self.container_id is not None:
                raw = self.transport.inspect(self.container_id, 15)
                if raw is not None:
                    validate_container(raw, self.container_id, self.plan)
                    self.transport.remove(self.container_id, 15)
                self.cleanup_complete = self.transport.inspect(self.container_id, 15) is None
        except Exception:
            # Keep the resource reserved for reconciliation; never delete by name/PID.
            self.cleanup_complete = False
        finally:
            if self.channel is not None:
                try:
                    self.channel.abort()
                except Exception:
                    self.cleanup_complete = False

    def is_alive(self) -> bool:
        return not self.closed and self.channel is not None and self.channel.poll() is None


class BW20TimingWorkloadFactory:
    """Plug verified sessions into the existing Harness without copying its statistics.

    Deployment supplies a fresh plan/staging and transport for each process. This
    factory is not registered in the global Adapter Catalog and does not settle leases.
    """

    def __init__(self, session_factory: Callable[[str, int | None], BW20TimingSession]):
        self.session_factory = session_factory
        self.sessions: list[BW20TimingSession] = []
        self._names: set[str] = set()
        self._lock = threading.RLock()
        self._closing = False

    def _open(self, role: str, restart: int | None) -> BW20TimingSession:
        with self._lock:
            if self._closing:
                raise TimingSessionError("job session factory has been closed")
            session = self.session_factory(role, restart)
            if (session.plan.container_name in self._names
                    or session.create_attempted or session.closed):
                raise TimingSessionError("each timing process requires a fresh session identity")
            self._names.add(session.plan.container_name)
            self.sessions.append(session)
            return session.open()

    def timer(self) -> DockerTorchEventTimer:
        return DockerTorchEventTimer(self._open("timer", None))

    def workload(
        self, probe_type: Stage0ProbeType, restart_ordinal: int,
    ) -> DockerFormalStage0Workload:
        if probe_type not in (Stage0ProbeType.TIMER, Stage0ProbeType.NOISE,
                              Stage0ProbeType.KNOWN_SIGNAL, Stage0ProbeType.NULL_SIGNAL):
            raise ValueError("not a timing workload probe")
        if type(restart_ordinal) is not int or restart_ordinal < 0:
            raise ValueError("restart ordinal must be nonnegative")
        return DockerFormalStage0Workload(
            self._open(probe_type.value, restart_ordinal), probe_type=probe_type)

    def force_close(self) -> bool:
        with self._lock:
            self._closing = True
            failed = False
            for session in self.sessions:
                try:
                    session.force_close()
                except Exception:
                    failed = True
                    session.cleanup_complete = False
            return not failed and all(session.cleanup_complete for session in self.sessions)

    def finish(self) -> bool:
        """Reap live children normally when authority is valid; always clean owned CIDs."""
        with self._lock:
            self._closing = True
            for session in self.sessions:
                if not session.closed:
                    try:
                        session.close()
                    except Exception:
                        # Loss of authority/error must not block exact-CID cleanup.
                        # Missing raw waitpid still prevents D from accepting this run.
                        # The final sweep below attempts every session independently.
                        pass
            return self.force_close()

    def live_bindings(self) -> list[Mapping]:
        """Refresh host identities at telemetry boundaries; never retain stale PIDs."""
        with self._lock:
            return self._live_bindings()

    def _live_bindings(self) -> list[Mapping]:
        bindings = []
        for session in self.sessions:
            if session.closed:
                if not session.cleanup_complete:
                    raise TimingSessionError("closed session cleanup remains unconfirmed")
                continue
            session._guard()
            session._binding()
            bindings.append(session.identity_observations[-1])
        return bindings
