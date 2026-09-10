# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import UUID

import pytest

from hcuopt.deployment.bw20_timing_transport import (
    BW20DockerTransport,
    DockerCreationUnconfirmed,
    JsonLineChannel,
    _timeout,
)
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]
TARGET = load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")
CID = "a" * 64


class Runner:
    host, user, port = "10.17.1.20", "github", 22

    def __init__(self, code=0, stdout="", stderr=""):
        self.response = SimpleNamespace(returncode=code, stdout=stdout.encode(),
                                        stderr=stderr.encode())
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append(tuple(argv))
        return self.response


def test_real_local_stdio_roundtrip_and_close():
    code = "import json,sys; print('{}',flush=True); " \
           "print(json.dumps(json.loads(sys.stdin.readline())),flush=True)"
    channel = JsonLineChannel((sys.executable, "-u", "-c", code))
    try:
        assert channel.receive(5) == {}
        assert channel.request({"op": "echo"}, 5) == {"op": "echo"}
        assert channel.wait(5) == 0
    finally:
        channel.abort()


def test_stdio_timeout_terminates_only_retained_process():
    channel = JsonLineChannel((sys.executable, "-u", "-c",
                              "import time; print('{}',flush=True); time.sleep(30)"))
    try:
        channel.receive(5)
        with pytest.raises(TimeoutError):
            channel.request({"op": "stall"}, 0.2)
        assert channel.poll() is not None
    finally:
        channel.abort()


@pytest.mark.parametrize("source", ["print('[]')", "print('x'*70000)", "print('not-json')"])
def test_invalid_wire_response_fails(source):
    channel = JsonLineChannel((sys.executable, "-u", "-c", source))
    try:
        with pytest.raises((ValueError, RuntimeError)):
            channel.receive(5)
    finally:
        channel.abort()


@pytest.mark.parametrize("value", [0, -1, True, float('nan'), float('inf'), 91])
def test_timeout_validation(value):
    with pytest.raises(ValueError):
        _timeout(value)


@pytest.mark.parametrize("code,stdout,stderr,absent", [
    (1, "[]", f"Error: No such container: {CID}", True),
    (1, "[]", f"Error: No such object: {CID}", True),
    (1, "[]", f"Error response from daemon: No such container: {CID}", True),
    (255, "", "Connection lost", False),
    (1, "", "Cannot connect to Docker daemon", False),
    (1, "[]", "Error: No such container: " + "b" * 64, False),
])
def test_missing_container_distinct_from_unknown(code, stdout, stderr, absent):
    transport = BW20DockerTransport(runner=Runner(code, stdout, stderr), target=TARGET)
    transport.owned[CID] = None
    if absent:
        assert transport.inspect(CID, 1) is None
    else:
        with pytest.raises(RuntimeError):
            transport.inspect(CID, 1)


def test_unknown_container_cannot_be_inspected_or_removed():
    runner = Runner()
    transport = BW20DockerTransport(runner=runner, target=TARGET)
    with pytest.raises(ValueError):
        transport.remove(CID, 1)
    assert not runner.calls


@pytest.mark.parametrize("path", ["/proc/1/environ", "/etc/passwd", "/proc/1/../2/stat"])
def test_procfs_allowlist_rejects_unrelated_data(path):
    runner = Runner()
    transport = BW20DockerTransport(runner=runner, target=TARGET)
    with pytest.raises(ValueError):
        transport.read_proc(PurePosixPath(path))
    assert not runner.calls


def test_inspect_identity_mismatch_rejected():
    runner = Runner(stdout=json.dumps([{"Id": "b" * 64}]))
    transport = BW20DockerTransport(runner=runner, target=TARGET)
    transport.owned[CID] = None
    with pytest.raises(RuntimeError):
        transport.inspect(CID, 1)


def test_cpu_create_decodes_real_command_bytes_and_keeps_isolation():
    runner = Runner(stdout=CID + "\n")
    transport = BW20DockerTransport(runner=runner, target=TARGET)
    plan, cid = transport.create_cpu_rehearsal(UUID(int=1))
    assert cid == CID and transport.owned[cid] == plan
    args = runner.calls[0]
    assert args[:2] == ("docker", "create")
    assert not any(arg.startswith(("--pid=", "--device=", "--mount")) for arg in args)
    assert "--network=none" in args and "--read-only" in args
    assert "--user=1002:1002" in args and "--cap-drop=ALL" in args
    assert plan.resource_id == "bw20:cpu-rehearsal"


def test_creation_failure_retains_attempt_for_reconciliation():
    runner = Runner(code=125, stderr="docker: --pid: invalid PID mode.\n")
    transport = BW20DockerTransport(runner=runner, target=TARGET)
    with pytest.raises(DockerCreationUnconfirmed) as error:
        transport.create_cpu_rehearsal(UUID(int=1))
    assert error.value.result.returncode == 125
    assert "invalid PID mode" in error.value.result.stderr
    assert error.value.plan.container_name == f"hcuopt-bw20-cpu-{UUID(int=1)}"
    assert not transport.owned


@pytest.mark.parametrize("stdout", ["", "a" * 12, "A" * 64, CID + "\nextra"])
def test_creation_requires_full_cid(stdout):
    transport = BW20DockerTransport(runner=Runner(stdout=stdout), target=TARGET)
    with pytest.raises(RuntimeError, match="full CID"):
        transport.create_cpu_rehearsal(UUID(int=1))
    assert not transport.owned
