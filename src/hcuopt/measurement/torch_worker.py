"""Locked-image HCU timing worker used by the nmz36 Stage 0 deployment.

The controller remains PID 1 in its container, forks one measured child, captures the
child's real procfs identity, and reaps it with ``waitpid``.  Device Events and host
timestamps are both captured by that measured child; no parent-context Event is used
to time child-context work.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

PROTOCOL = "hcuopt-stage0-torch-worker-v1"


def _write_json(stream: TextIO, value: Mapping[str, Any]) -> None:
    stream.write(
        json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    stream.flush()


def _read_json(stream: TextIO) -> dict[str, Any] | None:
    line = stream.readline()
    if not line:
        return None
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("worker commands must be JSON objects")
    return value


def _proc_stat(process_id: int) -> str:
    return Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8").strip()


def _operation_multiplier(probe_type: str, segment: str) -> int:
    if probe_type == "known_signal" and segment in {"B1", "B2"}:
        return 2
    return 1


def _allocator_cache_receipt(torch, sequence: int) -> dict:
    """Record allocator maintenance, NOT an HBM/L2/CPU cache flush claim."""
    started = time.monotonic_ns()
    torch.cuda.synchronize()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    finished = time.monotonic_ns()
    if finished <= started:
        raise RuntimeError("allocator maintenance clock did not advance")
    return {"schema_version": "stage0-allocator-cache-receipt-v1",
            "process_id": os.getpid(), "sequence": sequence,
            "method": "torch.cuda.synchronize/empty_cache/synchronize",
            "scope": "pytorch_unused_allocator_blocks_only",
            "hardware_cache_flushed": False,
            "started_monotonic_ns": started, "finished_monotonic_ns": finished}


def _child_loop(command_fd: int, response_fd: int,
                device_validator: Callable[[Any], dict] | None = None,
                cache_receipts: bool = False) -> int:
    command_stream = os.fdopen(command_fd, "r", encoding="utf-8", buffering=1)
    response_stream = os.fdopen(response_fd, "w", encoding="utf-8", buffering=1)
    try:
        import torch

        # Deployment checks run only in the forked child, before test tensors/Events.
        device_identity = device_validator(torch) if device_validator is not None else None
        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise RuntimeError("locked worker requires exactly one visible HCU")
        torch.cuda.set_device(0)
        tensor = torch.ones(1 << 18, device="cuda", dtype=torch.float32)
        fixture_locked = False
        origin = torch.cuda.Event(enable_timing=True)
        origin.record()
        torch.cuda.synchronize()
        _write_json(
            response_stream,
            {
                "protocol": PROTOCOL,
                "event": "ready",
                "process_id": os.getpid(),
                **({"device_identity": device_identity} if device_identity is not None else {}),
            },
        )
        cache_sequence = 0
        while command := _read_json(command_stream):
            operation = command.get("op")
            if operation == "close":
                _write_json(response_stream, {"protocol": PROTOCOL, "event": "closing"})
                return 0
            if operation == "synchronize":
                torch.cuda.synchronize()
                _write_json(response_stream, {"protocol": PROTOCOL, "event": "synchronized"})
                continue
            if operation == "ticks":
                snapshot = torch.cuda.Event(enable_timing=True)
                snapshot.record()
                torch.cuda.synchronize()
                ticks = round(float(origin.elapsed_time(snapshot)) * 1_000_000)
                _write_json(response_stream, {"protocol": PROTOCOL, "device_ticks": ticks})
                continue
            if operation in {"calibration", "calibration_spaced"}:
                sample_count = int(command["sample_count"])
                resolution_sample_count = int(command["resolution_sample_count"])
                if sample_count < 3 or resolution_sample_count < 3:
                    raise ValueError("calibration requires at least three raw samples")
                points = []
                last_ticks = -1
                for point_ordinal in range(sample_count):
                    # Event.elapsed_time() already exposes a time axis.  The original
                    # back-to-back points were only tens of microseconds apart, so
                    # host call latency dominated the regression and produced a
                    # different scale on repeated runs.  V2 spaces points on both
                    # clocks without mutating the device policy.
                    if operation == "calibration_spaced" and point_ordinal:
                        time.sleep(0.01)
                    started_ns = time.monotonic_ns()
                    snapshot = torch.cuda.Event(enable_timing=True)
                    snapshot.record()
                    torch.cuda.synchronize()
                    finished_ns = time.monotonic_ns()
                    ticks = round(float(origin.elapsed_time(snapshot)) * 1_000_000)
                    if finished_ns <= started_ns or ticks <= last_ticks:
                        raise RuntimeError("atomic calibration axes did not strictly advance")
                    points.append(
                        {
                            "point_ordinal": point_ordinal,
                            "device_ticks": ticks,
                            "host_started_monotonic_ns": started_ns,
                            "host_finished_monotonic_ns": finished_ns,
                        }
                    )
                    last_ticks = ticks
                pairs = []
                for _ in range(resolution_sample_count):
                    started = torch.cuda.Event(enable_timing=True)
                    finished = torch.cuda.Event(enable_timing=True)
                    started.record()
                    finished.record()
                    pairs.append((started, finished))
                torch.cuda.synchronize()
                deltas = [
                    round(float(started.elapsed_time(finished)) * 1_000_000)
                    for started, finished in pairs
                ]
                deltas = [value for value in deltas if value > 0]
                if len(deltas) < 3:
                    raise RuntimeError("HCU Event timer returned fewer than three positive deltas")
                _write_json(
                    response_stream,
                    {
                        "protocol": PROTOCOL,
                        "points": points,
                        "resolution_tick_deltas": deltas,
                    },
                )
                continue
            if operation == "configure_fixture":
                workload_elements = int(command["workload_elements"])
                if fixture_locked:
                    raise RuntimeError("workload fixture is already in use")
                if not 1 <= workload_elements <= 1 << 26:
                    raise ValueError("workload fixture size is outside the supported range")
                tensor = torch.ones(
                    workload_elements,
                    device="cuda",
                    dtype=torch.float32,
                )
                torch.cuda.synchronize()
                _write_json(
                    response_stream,
                    {
                        "protocol": PROTOCOL,
                        "event": "fixture_configured",
                        "workload_elements": workload_elements,
                    },
                )
                continue
            if operation == "resolution":
                sample_count = int(command["sample_count"])
                if sample_count < 3:
                    raise ValueError("resolution requires at least three samples")
                pairs = []
                for _ in range(sample_count):
                    started = torch.cuda.Event(enable_timing=True)
                    finished = torch.cuda.Event(enable_timing=True)
                    started.record()
                    finished.record()
                    pairs.append((started, finished))
                torch.cuda.synchronize()
                deltas = [
                    round(float(started.elapsed_time(finished)) * 1_000_000)
                    for started, finished in pairs
                ]
                deltas = [value for value in deltas if value > 0]
                if len(deltas) < 3:
                    raise RuntimeError("HCU Event timer returned fewer than three positive deltas")
                _write_json(
                    response_stream,
                    {"protocol": PROTOCOL, "resolution_tick_deltas": deltas},
                )
                continue
            if operation == "warmup":
                fixture_locked = True
                probe_type = str(command["probe_type"])
                segment = str(command["segment"])
                iterations = int(command["iterations"])
                if iterations < 1:
                    raise ValueError("warmup iterations must be positive")
                for _ in range(iterations):
                    for _ in range(_operation_multiplier(probe_type, segment)):
                        tensor.add_(1.0)
                torch.cuda.synchronize()
                _write_json(
                    response_stream,
                    {
                        "protocol": PROTOCOL,
                        "event": "warmed",
                        "segment": segment,
                        "batch_iterations": iterations,
                    },
                )
                continue
            if operation != "measure":
                raise ValueError(f"unsupported worker operation: {operation!r}")

            fixture_locked = True
            probe_type = str(command["probe_type"])
            segment = str(command["segment"])
            iterations = int(command["iterations"])
            if iterations < 1:
                raise ValueError("measurement iterations must be positive")
            cache_receipt = None
            if cache_receipts:
                cache_sequence += 1
                cache_receipt = _allocator_cache_receipt(torch, cache_sequence)
            else:
                torch.cuda.empty_cache()
            # Advance the stream before the measured start Event so successive absolute
            # Event ticks remain strictly ordered even on coarse timestamp hardware.
            tensor.add_(0.0)
            started_event = torch.cuda.Event(enable_timing=True)
            finished_event = torch.cuda.Event(enable_timing=True)
            started_monotonic_ns = time.monotonic_ns()
            started_event.record()
            for _ in range(iterations):
                for _ in range(_operation_multiplier(probe_type, segment)):
                    tensor.add_(1.0)
            finished_event.record()
            torch.cuda.synchronize()
            finished_monotonic_ns = time.monotonic_ns()
            started_ticks = round(float(origin.elapsed_time(started_event)) * 1_000_000)
            finished_ticks = round(float(origin.elapsed_time(finished_event)) * 1_000_000)
            if (
                finished_monotonic_ns <= started_monotonic_ns
                or finished_ticks <= started_ticks
                or not math.isfinite(float(finished_ticks - started_ticks))
            ):
                raise RuntimeError("HCU timing interval is not positive")
            _write_json(
                response_stream,
                {
                    "protocol": PROTOCOL,
                    "process_id": os.getpid(),
                    "segment": segment,
                    "batch_iterations": iterations,
                    "started_monotonic_ns": started_monotonic_ns,
                    "finished_monotonic_ns": finished_monotonic_ns,
                    "started_device_ticks": started_ticks,
                    "finished_device_ticks": finished_ticks,
                    **({"cache_receipt": cache_receipt} if cache_receipts else {}),
                },
            )
        return 0
    except BaseException as exc:
        _write_json(
            response_stream,
            {
                "protocol": PROTOCOL,
                "event": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            },
        )
        return 1
    finally:
        command_stream.close()
        response_stream.close()


def _controller_loop(device_validator: Callable[[Any], dict] | None = None,
                     cache_receipts: bool = False) -> int:
    parent_read, child_write = os.pipe()
    child_read, parent_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(parent_read)
        os.close(parent_write)
        exit_code = _child_loop(child_read, child_write, device_validator, cache_receipts)
        os._exit(exit_code)

    os.close(child_read)
    os.close(child_write)
    child_commands = os.fdopen(parent_write, "w", encoding="utf-8", buffering=1)
    child_responses = os.fdopen(parent_read, "r", encoding="utf-8", buffering=1)
    wait_status: int | None = None
    try:
        ready = _read_json(child_responses)
        if ready is None or ready.get("event") != "ready":
            raise RuntimeError(f"measured child failed to become ready: {ready!r}")
        child_proc_stat = _proc_stat(child_pid)
        _write_json(
            sys.stdout,
            {
                **ready,
                "observer_process_id": os.getpid(),
                "proc_stat_line": child_proc_stat,
            },
        )
        while command := _read_json(sys.stdin):
            _write_json(child_commands, command)
            response = _read_json(child_responses)
            if response is None:
                raise RuntimeError("measured child closed its evidence pipe")
            if response.get("event") == "error":
                raise RuntimeError(str(response.get("error", "measured child failed")))
            if command.get("op") == "close":
                waited_pid, wait_status = os.waitpid(child_pid, 0)
                _write_json(
                    sys.stdout,
                    {
                        **response,
                        "observer_process_id": os.getpid(),
                        "process_id": child_pid,
                        "proc_stat_line": child_proc_stat,
                        "waitpid_result_pid": waited_pid,
                        "wait_status": wait_status,
                    },
                )
                return 0 if os.waitstatus_to_exitcode(wait_status) == 0 else 1
            _write_json(sys.stdout, response)
    finally:
        child_commands.close()
        child_responses.close()
        if wait_status is None:
            try:
                os.kill(child_pid, 15)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", action="store_true")
    return parser


def main(argv: list[str] | None = None, *,
         device_validator: Callable[[Any], dict] | None = None,
         cache_receipts: bool = False) -> int:
    args = build_parser().parse_args(argv)
    if not args.controller:
        raise SystemExit("the Stage 0 torch worker must run through its lifecycle controller")
    if os.name != "posix" or not hasattr(os, "fork"):
        raise SystemExit("the Stage 0 torch worker requires POSIX fork/waitpid")
    return _controller_loop(device_validator, cache_receipts)


if __name__ == "__main__":
    raise SystemExit(main())
