"""Concrete nmz36 deployment adapters for Formal Stage 0 measurement.

These classes run the measured process in the digest-locked image while the harness
and cleanup controller remain on the host.  The measured child owns its HIP Events;
the host only transports and binds the raw observations to procfs lifecycle evidence.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from hcuopt.adapters.execution import (
    FENCING_LABEL,
    MANAGED_LABEL,
    RESOURCE_LABEL,
)
from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.harness import FormalStage0Workload
from hcuopt.measurement.models import (
    FormalWorkloadTimingV2,
    ProcessIdentity,
    ProcessLifecycleRecordV2,
    Stage0Segment,
)

WORKER_PROTOCOL = "hcuopt-stage0-torch-worker-v1"
SHA256_PREFIX = "sha256:"
_NUMBER = r"([0-9]+(?:\.[0-9]+)?)"


class Nmz36RuntimeError(RuntimeError):
    pass


class ManagedProcessRegistry:
    """Tracks measured child PIDs so telemetry cannot misclassify owned HCU work."""

    def __init__(self) -> None:
        self._process_ids: set[int] = set()
        self._lock = threading.Lock()

    def add(self, process_id: int) -> None:
        with self._lock:
            self._process_ids.add(process_id)

    def discard(self, process_id: int) -> None:
        with self._lock:
            self._process_ids.discard(process_id)

    def contains(self, process_id: int) -> bool:
        with self._lock:
            return process_id in self._process_ids


class _JsonContainerProcess:
    def __init__(
        self,
        *,
        target: TargetSpec,
        source_root: Path,
        output_dir: Path,
        role: str,
        resource_id: str,
        fencing_token: int,
        registry: ManagedProcessRegistry,
        startup_timeout_seconds: float = 90.0,
    ) -> None:
        self.target = target
        self.resource_id = resource_id
        self.fencing_token = fencing_token
        self.registry = registry
        self.container_name = f"hcuopt-stage0-{role}-{uuid4().hex}"
        self.stderr_path = output_dir.resolve() / "container-logs" / f"{self.container_name}.log"
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr = self.stderr_path.open("wb")
        command = self._command(source_root.resolve())
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                text=True,
                encoding="utf-8",
                bufsize=1,
                shell=False,
                start_new_session=True,
            )
            self.start_observation = self._read_response(startup_timeout_seconds)
            self._validate_start(self.start_observation)
            self.process_id = int(self.start_observation["process_id"])
            self.process_start_token = _proc_start_token(
                str(self.start_observation["proc_stat_line"]),
                self.process_id,
            )
            self.registry.add(self.process_id)
        except BaseException:
            self._force_remove()
            self._stderr.close()
            raise
        self.exit_observation: dict[str, Any] | None = None
        self._closed = False

    def _command(self, source_root: Path) -> tuple[str, ...]:
        accelerator = self.target.execution_host.accelerator
        image = self.target.inference_image.immutable_reference
        return (
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--pull=never",
            "--name",
            self.container_name,
            "--label",
            f"{MANAGED_LABEL}=true",
            "--label",
            f"{RESOURCE_LABEL}={self.resource_id}",
            "--label",
            f"{FENCING_LABEL}={self.fencing_token}",
            "--security-opt",
            "no-new-privileges",
            "--pid=host",
            "--cpuset-cpus",
            accelerator.cpu_affinity,
            "--cpuset-mems",
            str(accelerator.numa_node),
            "--device",
            "/dev/kfd",
            "--device",
            "/dev/dri",
            "--group-add",
            "video",
            "--env",
            f"ROCR_VISIBLE_DEVICES={accelerator.device_index}",
            "--env",
            "PYTHONPATH=/workspace/src",
            "--mount",
            f"type=bind,src={source_root},dst=/workspace,readonly",
            "--mount",
            "type=bind,src=/opt/hyhal,dst=/opt/hyhal,readonly",
            "--workdir",
            "/workspace",
            "--entrypoint",
            "python",
            image,
            "-m",
            "hcuopt.measurement.torch_worker",
            "--controller",
        )

    def request(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        if self._closed:
            raise Nmz36RuntimeError("Stage 0 worker is already closed")
        assert self._process.stdin is not None
        self._process.stdin.write(
            json.dumps(
                dict(payload),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        self._process.stdin.flush()
        response = self._read_response(timeout_seconds)
        if response.get("event") == "error":
            raise Nmz36RuntimeError(str(response.get("error", "Stage 0 worker failed")))
        return response

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.exit_observation = self.request({"op": "close"}, timeout_seconds=30.0)
            returncode = self._process.wait(timeout=30)
            if returncode != 0:
                raise Nmz36RuntimeError(
                    f"Stage 0 worker exited with {returncode}; log={self.stderr_path}"
                )
        except BaseException:
            self._force_remove()
            raise
        finally:
            self._closed = True
            if hasattr(self, "process_id"):
                self.registry.discard(self.process_id)
            self._stderr.close()

    def is_alive(self) -> bool:
        return not self._closed and self._process.poll() is None

    def force_close(self) -> None:
        if self._closed:
            return
        self._force_remove()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        self._closed = True
        if hasattr(self, "process_id"):
            self.registry.discard(self.process_id)
        self._stderr.close()

    def _read_response(self, timeout_seconds: float) -> dict[str, Any]:
        assert self._process.stdout is not None
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout_seconds):
                raise Nmz36RuntimeError(
                    f"Stage 0 worker response timed out; log={self.stderr_path}"
                )
            line = self._process.stdout.readline()
        finally:
            selector.close()
        if not line:
            raise Nmz36RuntimeError(
                f"Stage 0 worker closed stdout; exit={self._process.poll()}; "
                f"log={self.stderr_path}"
            )
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Nmz36RuntimeError("Stage 0 worker returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("protocol") != WORKER_PROTOCOL:
            raise Nmz36RuntimeError("Stage 0 worker returned an invalid protocol envelope")
        return response

    @staticmethod
    def _validate_start(response: Mapping[str, Any]) -> None:
        if response.get("event") != "ready":
            raise Nmz36RuntimeError(f"Stage 0 worker did not become ready: {response!r}")
        process_id = response.get("process_id")
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id < 1:
            raise Nmz36RuntimeError("Stage 0 worker has no valid measured PID")
        _proc_start_token(str(response.get("proc_stat_line", "")), process_id)

    def _force_remove(self) -> None:
        subprocess.run(
            ("docker", "rm", "--force", self.container_name),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


class DockerTorchEventTimer:
    """Device clock hosted in the locked image and physical HCU binding."""

    def __init__(self, process: _JsonContainerProcess) -> None:
        self.process = process

    def read_ticks(self) -> int:
        value = self.process.request({"op": "ticks"})["device_ticks"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise Nmz36RuntimeError("Stage 0 timer returned invalid device ticks")
        return value

    def measure_resolution_ns(self, sample_count: int) -> float:
        return float(min(self.sample_resolution_ticks(sample_count)))

    def sample_resolution_ticks(self, sample_count: int) -> Sequence[int]:
        values = self.process.request(
            {"op": "resolution", "sample_count": sample_count}
        )["resolution_tick_deltas"]
        if not isinstance(values, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise Nmz36RuntimeError("Stage 0 timer returned invalid resolution samples")
        return tuple(values)

    def synchronize(self) -> None:
        self.process.request({"op": "synchronize"})

    def close(self) -> None:
        self.process.force_close()


class DockerFormalStage0Workload(FormalStage0Workload):
    def __init__(
        self,
        process: _JsonContainerProcess,
        *,
        probe_type: Stage0ProbeType,
    ) -> None:
        self.process = process
        self.probe_type = probe_type
        self.identity = ProcessIdentity(
            pid=process.process_id,
            start_token=process.process_start_token,
        )

    def process_identity(self) -> ProcessIdentity:
        return self.identity

    def synchronize(self) -> None:
        self.process.request({"op": "synchronize"})

    def warmup_segment(self, segment: Stage0Segment) -> None:
        self.process.request(
            {
                "op": "warmup",
                "probe_type": self.probe_type.value,
                "segment": segment,
            }
        )

    def measure_segment_batch(
        self,
        segment: Stage0Segment,
        iterations: int,
    ) -> FormalWorkloadTimingV2:
        response = self.process.request(
            {
                "op": "measure",
                "probe_type": self.probe_type.value,
                "segment": segment,
                "iterations": iterations,
            }
        )
        if response.get("process_id") != self.identity.pid:
            raise Nmz36RuntimeError("measured response came from a different process")
        return FormalWorkloadTimingV2(
            process_id=self.identity.pid,
            process_start_token=self.identity.start_token,
            segment=response["segment"],
            batch_iterations=response["batch_iterations"],
            started_monotonic_ns=response["started_monotonic_ns"],
            finished_monotonic_ns=response["finished_monotonic_ns"],
            started_device_ticks=response["started_device_ticks"],
            finished_device_ticks=response["finished_device_ticks"],
        )

    def close(self) -> None:
        self.process.close()

    def is_alive(self) -> bool:
        return self.process.is_alive()


class DockerProcessLifecycleRecorder:
    def record_started(
        self,
        workload: DockerFormalStage0Workload,
        *,
        restart_ordinal: int,
        captured_monotonic_ns: int,
    ) -> ProcessLifecycleRecordV2:
        value = workload.process.start_observation
        return ProcessLifecycleRecordV2(
            event="started",
            restart_ordinal=restart_ordinal,
            observer_process_id=value["observer_process_id"],
            process_id=workload.identity.pid,
            proc_stat_line=value["proc_stat_line"],
            captured_monotonic_ns=captured_monotonic_ns,
        )

    def record_reaped(
        self,
        workload: DockerFormalStage0Workload,
        *,
        restart_ordinal: int,
        captured_monotonic_ns: int,
    ) -> ProcessLifecycleRecordV2:
        value = workload.process.exit_observation
        if value is None:
            raise Nmz36RuntimeError("measured process has no raw waitpid evidence")
        return ProcessLifecycleRecordV2(
            event="reaped",
            restart_ordinal=restart_ordinal,
            observer_process_id=value["observer_process_id"],
            process_id=workload.identity.pid,
            proc_stat_line=value["proc_stat_line"],
            captured_monotonic_ns=captured_monotonic_ns,
            waitpid_result_pid=value["waitpid_result_pid"],
            wait_status=value["wait_status"],
        )


class Nmz36WorkloadFactory:
    def __init__(
        self,
        *,
        target: TargetSpec,
        source_root: Path,
        output_dir: Path,
        resource_id: str,
        fencing_token: int,
        registry: ManagedProcessRegistry,
    ) -> None:
        self.target = target
        self.source_root = source_root
        self.output_dir = output_dir
        self.resource_id = resource_id
        self.fencing_token = fencing_token
        self.registry = registry

    def timer(self) -> DockerTorchEventTimer:
        return DockerTorchEventTimer(
            self._process("timer", restart_ordinal=None)
        )

    def workload(
        self,
        probe_type: Stage0ProbeType,
        restart_ordinal: int,
    ) -> DockerFormalStage0Workload:
        return DockerFormalStage0Workload(
            self._process(probe_type.value, restart_ordinal=restart_ordinal),
            probe_type=probe_type,
        )

    def _process(
        self,
        role: str,
        *,
        restart_ordinal: int | None,
    ) -> _JsonContainerProcess:
        suffix = role if restart_ordinal is None else f"{role}-r{restart_ordinal}"
        return _JsonContainerProcess(
            target=self.target,
            source_root=self.source_root,
            output_dir=self.output_dir,
            role=suffix,
            resource_id=self.resource_id,
            fencing_token=self.fencing_token,
            registry=self.registry,
        )


class HySmiTelemetryCollector:
    """Collect typed HCU 7 telemetry and retain unmanaged KFD processes as evidence."""

    def __init__(
        self,
        target: TargetSpec,
        registry: ManagedProcessRegistry,
    ) -> None:
        self.target = target
        self.registry = registry

    def collect(self) -> Mapping[str, Any]:
        device_index = self.target.execution_host.accelerator.device_index
        device_output = _run_text(
            (
                "hy-smi",
                "-d",
                str(device_index),
                "--showtemp",
                "--showclocks",
                "--showperflevel",
                "--showpower",
            )
        )
        process_output = _run_text(("hy-smi", "--showpids"))
        return {
            "device": {
                "device_index": device_index,
                "temperature_c": _extract(device_output, rf"Sensor edge\) \(C\): {_NUMBER}"),
                "hotspot_temperature_c": _extract(
                    device_output,
                    rf"Sensor junction\) \(C\): {_NUMBER}",
                ),
                "sclk_mhz": _extract(device_output, rf"sclk clock level: .*\({_NUMBER}Mhz\)"),
                "mclk_mhz": _extract(device_output, rf"mclk clock level: .*\({_NUMBER}Mhz\)"),
                "performance_level": _extract_text(
                    device_output,
                    r"Performance Level: ([A-Za-z0-9_-]+)",
                ),
                "power_w": _extract(
                    device_output,
                    rf"Average Graphics Package Power \(W\): {_NUMBER}",
                ),
            },
            "cache": {
                "state": "flushed",
                "cleared_before_sample": True,
                "identity_hash": None,
            },
            "background_processes": self._processes(process_output, device_index),
        }

    def _processes(self, output: str, device_index: int) -> list[dict[str, Any]]:
        processes: list[dict[str, Any]] = []
        for block in re.split(r"\n(?=PID: )", output):
            pid_match = re.search(r"^PID: ([0-9]+)", block.strip())
            if pid_match is None:
                continue
            index_line = re.search(r"HCU Index: ([^\n]+)", block)
            indices = (
                {int(value) for value in re.findall(r"\['([0-9]+)'\]", index_line.group(1))}
                if index_line is not None
                else set()
            )
            if device_index not in indices:
                continue
            process_id = int(pid_match.group(1))
            memory_match = re.search(r"VRAM USED\(MiB\): ([0-9]+)", block)
            executable = _read_link(Path(f"/proc/{process_id}/exe"))
            command_line = _read_text(Path(f"/proc/{process_id}/comm"))
            processes.append(
                {
                    "process_id": process_id,
                    "executable": executable or "unavailable",
                    "command_line": command_line or "unavailable",
                    "uses_accelerator": True,
                    "device_memory_bytes": (
                        int(memory_match.group(1)) * 1024 * 1024 if memory_match else 0
                    ),
                    "managed_by_stage0": self.registry.contains(process_id),
                }
            )
        return processes


def _proc_start_token(proc_stat_line: str, process_id: int) -> str:
    prefix = f"{process_id} ("
    if not proc_stat_line.startswith(prefix):
        raise Nmz36RuntimeError("procfs stat does not identify the measured process")
    close = proc_stat_line.rfind(") ")
    if close < len(prefix):
        raise Nmz36RuntimeError("procfs stat has no parseable command field")
    remaining = proc_stat_line[close + 2 :].split()
    if len(remaining) < 20 or not remaining[19].isdigit():
        raise Nmz36RuntimeError("procfs stat has no parseable starttime")
    return f"linux-proc-startticks:{remaining[19]}"


def _run_text(argv: Sequence[str]) -> str:
    completed = subprocess.run(
        tuple(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise Nmz36RuntimeError(
            f"telemetry command failed ({completed.returncode}): {' '.join(argv)}; "
            f"stderr={completed.stderr[:1000]}"
        )
    return completed.stdout


def _extract(value: str, pattern: str) -> float:
    match = re.search(pattern, value)
    if match is None:
        raise Nmz36RuntimeError(f"hy-smi output is missing field: {pattern}")
    return float(match.group(1))


def _extract_text(value: str, pattern: str) -> str:
    match = re.search(pattern, value)
    if match is None:
        raise Nmz36RuntimeError(f"hy-smi output is missing field: {pattern}")
    return match.group(1)


def _read_link(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[:1000]
    except OSError:
        return ""
