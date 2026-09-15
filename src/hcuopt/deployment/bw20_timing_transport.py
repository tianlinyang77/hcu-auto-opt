# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bounded JSON stdio and explicit BW20 SSH/Docker transport; no auto registration."""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from hcuopt.adapters.bw20_execution import IMAGE_ID
from hcuopt.adapters.execution import FENCING_LABEL, MANAGED_LABEL, RESOURCE_LABEL
from hcuopt.deployment.bw20_environment import IMAGE
from hcuopt.deployment.bw20_stage0_runtime import BW20TimingPlan, build_timing_plan

CPU_PROTOCOL = "hcuopt-stage0-cpu-rehearsal-v1"
CPU_PROGRAM = r'''
import json, os, signal, sys, time
protocol = "hcuopt-stage0-cpu-rehearsal-v1"
signal.alarm(90)
r, w = os.pipe()
pid = os.fork()
if pid == 0:
    signal.alarm(90)
    os.close(w)
    os.read(r, 1)
    os._exit(0)
os.close(r)
with open("/proc/%s/stat" % pid) as f:
    stat = f.read().strip()
def emit(value):
    print(json.dumps(dict(protocol=protocol, **value)), flush=True)
emit(dict(event="ready", process_id=pid, observer_process_id=os.getpid(), proc_stat_line=stat,
          hcu_devices_present=os.path.exists("/dev/kfd") or os.path.exists("/dev/dri")))
for line in sys.stdin:
    request = json.loads(line)
    if request["op"] == "close":
        os.close(w)
        waited, status = os.waitpid(pid, 0)
        emit(dict(event="closing", process_id=pid, observer_process_id=os.getpid(),
                  proc_stat_line=stat, waitpid_result_pid=waited, wait_status=status))
        break
    if request["op"] == "stall":
        time.sleep(60)
    emit(dict(event="echo", request=request))
'''


def _timeout(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 90:
        raise ValueError("transport timeout must be within 90 seconds")
    return float(value)


class DockerCreationUnconfirmed(RuntimeError):
    """Retain exact attempt identity and diagnostics for operator reconciliation."""

    def __init__(self, plan, result):
        super().__init__("Docker creation unconfirmed; retain for reconciliation")
        self.plan = plan
        self.result = result


@dataclass(frozen=True)
class DockerTextResult:
    returncode: int
    stdout: str
    stderr: str


def validate_cpu_scope(raw, plan):
    host, config = raw["HostConfig"], raw["Config"]
    if (raw["Image"] != IMAGE_ID or config["Image"] != IMAGE
            or config["User"] != "1002:1002" or config["Entrypoint"] != ["python"]
            or config["Cmd"] != ["-I", "-S", "-u", "-c", CPU_PROGRAM]
            or host["NetworkMode"] != "none" or host["PidMode"] not in ("", "private")
            or host["IpcMode"] != "private" or host["ReadonlyRootfs"] is not True
            or host["Privileged"] is not False or host["CapDrop"] != ["ALL"]
            or host.get("CapAdd") or host.get("Devices") or host.get("DeviceRequests")
            or host.get("DeviceCgroupRules") or host.get("VolumesFrom") or raw.get("Mounts")
            or host["Memory"] != 256 * 1024**2 or host["MemorySwap"] != 256 * 1024**2
            or host["PidsLimit"] != 32 or host["ShmSize"] != 16 * 1024**2
            or host["NanoCpus"] != 1000000000 or host["AutoRemove"] is not True
            or host["RestartPolicy"]["Name"] != "no"
            or host["SecurityOpt"] not in (["no-new-privileges"], ["no-new-privileges:true"])
            or host["Tmpfs"] != {"/tmp": "rw,nosuid,nodev,noexec,size=16m"}
            or raw["Name"] != "/" + plan.container_name):
        raise RuntimeError("CPU rehearsal scope mismatch")


class JsonLineChannel:
    def __init__(self, argv):
        self.process = subprocess.Popen(tuple(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.lines = queue.Queue(maxsize=2)
        self.failure = None
        self.stderr = bytearray()
        self.lock = threading.Lock()
        self.threads = [threading.Thread(target=self._stdout, daemon=True),
                        threading.Thread(target=self._stderr, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _stdout(self):
        try:
            while line := self.process.stdout.readline(65537):
                if len(line) > 65536 or not line.endswith(b"\n"):
                    raise ValueError("oversized or unterminated JSON line")
                self.lines.put_nowait(line)
            self.lines.put_nowait(None)
        except Exception as exc:
            self.failure = exc

    def _stderr(self):
        try:
            while data := self.process.stderr.read(4096):
                if len(self.stderr) + len(data) > 65536:
                    self.failure = ValueError("stderr budget exceeded")
                    self.process.kill()  # Only the retained local transport process.
                    return
                self.stderr.extend(data)
        except (OSError, ValueError):
            return

    def receive(self, timeout):
        deadline = time.monotonic() + _timeout(timeout)
        while True:
            if self.failure is not None:
                raise RuntimeError("invalid transport stream") from self.failure
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("JSON response timed out")
            try:
                line = self.lines.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if line is None:
                raise EOFError("transport closed before a JSON response")
            value = json.loads(line.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("worker response is not an object")
            return value

    def request(self, payload, timeout):
        timeout = _timeout(timeout)
        encoded = (json.dumps(dict(payload), allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > 8192:
            raise ValueError("request exceeds wire budget")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("concurrent stdio requests are forbidden")
        try:
            started, done, errors = time.monotonic(), threading.Event(), []
            def write():
                try:
                    self.process.stdin.write(encoded)
                    self.process.stdin.flush()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    done.set()
            threading.Thread(target=write, daemon=True).start()
            if not done.wait(timeout):
                raise TimeoutError("JSON request write timed out")
            if errors:
                raise RuntimeError("JSON request write failed") from errors[0]
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("JSON request budget exhausted")
            return self.receive(remaining)
        except BaseException:
            self.abort()
            raise
        finally:
            self.lock.release()

    def poll(self):
        return self.process.poll()

    def wait(self, timeout):
        return self.process.wait(timeout=_timeout(timeout))

    def abort(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()
        for thread in self.threads:
            thread.join(timeout=1)


class BW20DockerTransport:
    def __init__(self, *, runner, target):
        if (runner.host, runner.user, runner.port) != ("10.17.1.20", "github", 22):
            raise ValueError("transport requires the explicit BW20 SSH endpoint")
        self.runner, self.target = runner, target
        self.owned = {}

    def _run(self, argv, timeout):
        result = self.runner.run(tuple(argv), timeout=_timeout(timeout))
        if len(result.stdout) > 262144 or len(result.stderr) > 65536:
            raise RuntimeError("Docker response exceeds budget")
        return DockerTextResult(result.returncode, result.stdout.decode("utf-8"),
                                result.stderr.decode("utf-8"))

    def _create(self, plan, timeout):
        args = list(plan.argv)
        if args[:2] != ["docker", "run"]:
            raise ValueError("invalid create plan")
        args[1] = "create"
        result = self._run(args, timeout)
        if result.returncode:
            raise DockerCreationUnconfirmed(plan, result)
        cid = result.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{64}", cid) is None:
            raise RuntimeError("Docker creation did not return a full CID")
        self.owned[cid] = plan
        return cid

    def create(self, plan, timeout):
        run = UUID(plan.container_name.removeprefix("hcuopt-bw20-stage0-"))
        if plan != build_timing_plan(self.target, run_id=run, fencing_token=plan.fencing_token):
            raise ValueError("noncanonical BW20 timing plan")
        return self._create(plan, timeout)

    def create_cpu_rehearsal(self, run_id: UUID, timeout=15):
        # Deliberately distinct resource and protocol: never a Formal timing run.
        name, resource = f"hcuopt-bw20-cpu-{run_id}", "bw20:cpu-rehearsal"
        argv = ("docker", "run", "--rm", "-i", "--pull=never", "--name", name,
            "--label", f"{MANAGED_LABEL}=true", "--label", f"{RESOURCE_LABEL}={resource}",
            "--label", f"{FENCING_LABEL}=1", "--user=1002:1002", "--network=none",
            "--ipc=private", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--cpus=1", "--memory=256m",
            "--memory-swap=256m", "--pids-limit=32", "--shm-size=16m",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m", "--entrypoint=python",
            IMAGE, "-I", "-S", "-u", "-c", CPU_PROGRAM)
        plan = BW20TimingPlan(argv, name, resource, 1, "")
        return plan, self._create(plan, timeout)

    def inspect(self, cid, timeout):
        if cid not in self.owned:
            raise ValueError("container not created by this transport")
        result = self._run(("docker", "container", "inspect", cid), timeout)
        if result.returncode:
            # Only an exact daemon not-found response proves absence, never SSH failure.
            if (result.returncode == 1 and result.stdout.strip() == "[]"
                    and result.stderr.strip() in (
                        f"Error: No such container: {cid}", f"Error: No such object: {cid}",
                        f"Error response from daemon: No such container: {cid}")):
                return None
            raise RuntimeError("container state unknown")
        values = json.loads(result.stdout)
        if not isinstance(values, list) or len(values) != 1 or values[0].get("Id") != cid:
            raise RuntimeError("unexpected Docker inspect identity")
        return values[0]

    def _ownership(self, raw, cid):
        plan = self.owned[cid]
        labels = raw.get("Config", {}).get("Labels", {})
        if (raw.get("Name") != "/" + plan.container_name or labels.get(MANAGED_LABEL) != "true"
                or labels.get(RESOURCE_LABEL) != plan.resource_id
                or labels.get(FENCING_LABEL) != str(plan.fencing_token)):
            raise RuntimeError("container ownership changed")

    def start(self, cid, timeout):
        raw = self.inspect(cid, timeout)
        if raw is None:
            raise RuntimeError("created container disappeared")
        self._ownership(raw, cid)
        if raw["State"]["Running"] is not False:
            raise RuntimeError("container already running")
        plan = self.owned[cid]
        if plan.resource_id == "bw20:cpu-rehearsal":
            validate_cpu_scope(raw, plan)
        else:
            from hcuopt.deployment.bw20_timing_session import validate_container
            validate_container(raw, cid, plan)
        command = self.runner.wrapped_argv(("docker", "start", "--attach", "--interactive", cid))
        return JsonLineChannel(command)

    def remove(self, cid, timeout):
        raw = self.inspect(cid, timeout)
        if raw is None:
            return
        self._ownership(raw, cid)
        result = self._run(("docker", "rm", "--force", cid), timeout)
        if result.returncode:
            raise RuntimeError("container removal unconfirmed")

    def read_proc(self, path):
        value = str(path)
        pattern = r"/proc/[1-9][0-9]*/(?:stat|status|cgroup|task/[1-9][0-9]*/children)"
        if re.fullmatch(pattern, value) is None:
            raise ValueError("procfs path outside allowlist")
        result = self._run(("head", "-c", "65537", value), 10)
        if result.returncode or len(result.stdout) > 65536:
            raise RuntimeError("host procfs unavailable")
        return result.stdout

    def read_namespace(self, path):
        if re.fullmatch(r"/proc/(?:self|[1-9][0-9]*)", str(path)) is None:
            raise ValueError("namespace path outside allowlist")
        result = self._run(("readlink", str(PurePosixPath(path) / "ns/pid")), 10)
        if result.returncode:
            raise RuntimeError("host namespace unavailable")
        return result.stdout.strip()
