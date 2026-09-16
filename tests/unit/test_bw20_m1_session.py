# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from hcuopt.deployment.bw20_m1_runtime import M1_WORKER_PROTOCOL, build_m1_container_plan
from hcuopt.deployment.bw20_m1_session import (
    BW20M1PairedWorkload,
    BW20M1ProcessSession,
    BW20M1SessionError,
)
from hcuopt.measurement.models import RawEvidenceFileV2
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = UUID("00000000-0000-0000-0000-000000000126")
CID = "c" * 64
MODULE_HASH = "sha256:" + "d" * 64
FILE_HASH = "sha256:" + "e" * 64


def _target():
    return load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")


def _stat(pid=2, ticks=778899):
    return f"{pid} (python worker) S " + " ".join(["1"] * 18 + [str(ticks)])


def _ready():
    return {
        "protocol": M1_WORKER_PROTOCOL,
        "event": "ready",
        "observer_process_id": 1,
        "process_id": 2,
        "process_start_token": "linux-proc-startticks:778899",
        "proc_stat_line": _stat(),
        "module_hash": MODULE_HASH,
        "device_identity": {
            "pci": "0000:b1:00.0",
            "architecture": "gfx936",
            "logical_device_index": 0,
        },
        "controller_pid_namespace": "pid:[4026532999]",
        "process_pid_namespace": "pid:[4026532999]",
        "namespace_hash": FILE_HASH,
        "cache_sha256": FILE_HASH,
        "import_sha256": FILE_HASH,
    }


class _Channel:
    def __init__(self, ready):
        self.ready = ready
        self.requests = []
        self.aborted = False

    def receive(self, timeout):
        assert timeout > 0
        return self.ready

    def request(self, payload, timeout):
        assert timeout > 0
        self.requests.append(dict(payload))
        operation = payload["op"]
        if operation == "close":
            return {
                "protocol": M1_WORKER_PROTOCOL,
                "event": "closing",
                "observer_process_id": 1,
                "process_id": 2,
                "proc_stat_line": _stat(),
                "waitpid_result_pid": 2,
                "wait_status": 0,
            }
        if operation == "measure":
            return {
                "protocol": M1_WORKER_PROTOCOL,
                "event": "measured",
                "sample_ordinal": 0,
                "event_sha256": FILE_HASH,
            }
        return {
            "protocol": M1_WORKER_PROTOCOL,
            "event": "synchronized" if operation == "synchronize" else "warmed",
        }

    def wait(self, timeout):
        assert timeout > 0
        return 0

    def poll(self):
        return None if not self.aborted else 0

    def abort(self):
        self.aborted = True


class _Transport:
    def __init__(self, channel):
        self.channel = channel
        self.removed = False
        self.timeouts = []

    def create(self, plan, timeout):
        assert timeout > 0
        self.timeouts.append(("create", timeout))
        self.plan = plan
        return CID

    def start(self, cid, timeout):
        assert cid == CID and timeout > 0
        self.timeouts.append(("start", timeout))
        return self.channel

    def inspect(self, cid, timeout):
        assert cid == CID and timeout > 0
        return None if self.removed else {"Id": CID}

    def remove(self, cid, timeout):
        assert cid == CID and timeout > 0
        self.removed = True


def _binding(plan, ready):
    return {
        "container_id": CID,
        "resource_id": plan.resource_id,
        "fencing_token": plan.fencing_token,
        "worker_protocol": M1_WORKER_PROTOCOL,
        "container_ready": ready,
        "host_measured": {"host_pid": 1002, "start_token": "linux-proc-startticks:778899"},
    }


def _session(ready=None):
    plan = build_m1_container_plan(
        _target(),
        run_id=RUN_ID,
        arm="baseline",
        acquisition_ordinal=0,
        fencing_token=9,
    )
    ready = _ready() if ready is None else ready
    channel = _Channel(ready)
    transport = _Transport(channel)
    session = BW20M1ProcessSession(
        plan=plan,
        target=_target(),
        transport=transport,
        assert_staging=lambda value: None,
        assert_lease=lambda: None,
        bind_process=lambda cid, observed: _binding(plan, observed),
        cancelled=lambda: False,
        expected_module_hash=MODULE_HASH,
    )
    return session, channel, transport


def test_session_keeps_container_pid_evidence_and_cleans_exact_cid() -> None:
    session, channel, transport = _session()
    session.open()
    assert all(timeout <= 90 for _, timeout in transport.timeouts)
    assert session.process_id == 2
    assert session.identity_observations[-1]["host_measured"]["host_pid"] == 1002
    assert session.request({"op": "synchronize"})["event"] == "synchronized"
    session.close()
    assert session.exit_observation["waitpid_result_pid"] == 2
    assert transport.removed and channel.aborted and session.cleanup_complete
    assert not session.is_alive()


def test_session_rejects_wrong_device_before_accepting_activation() -> None:
    ready = _ready()
    ready["device_identity"]["pci"] = "0000:b2:00.0"
    session, _, transport = _session(ready)
    with pytest.raises(BW20M1SessionError, match="activation"):
        session.open()
    assert transport.removed and session.cleanup_complete


def test_session_rejects_mismatched_container_pid_namespace() -> None:
    ready = _ready()
    ready["process_pid_namespace"] = "pid:[4026533000]"
    session, _, transport = _session(ready)
    with pytest.raises(BW20M1SessionError, match="activation"):
        session.open()
    assert transport.removed and session.cleanup_complete


def test_paired_workload_projects_only_hash_verified_worker_evidence(tmp_path) -> None:
    session, _, _ = _session()
    session.open()

    class Mirror:
        def __init__(self):
            self.names = []

        def fetch(self, name, expected_hash):
            self.names.append((name, expected_hash))
            path = tmp_path / name
            path.write_text("{}\n")
            return RawEvidenceFileV2(uri=path.as_uri(), sha256=expected_hash)

    mirror = Mirror()
    workload = BW20M1PairedWorkload(
        process=session,
        target=_target(),
        artifact=SimpleNamespace(content_hash="sha256:" + "f" * 64),
        mirror=mirror,
    )
    assert workload.process_identity().pid == 2
    assert workload.activation_evidence().arm == "baseline"
    assert workload.measure_batch(1).sha256 == FILE_HASH
    assert mirror.names == [
        ("cache-namespace.json", FILE_HASH),
        ("device-event-0000.json", FILE_HASH),
    ]
    workload.close()
