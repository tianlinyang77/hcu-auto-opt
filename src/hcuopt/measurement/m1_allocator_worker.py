"""Locked-image worker for the single M1 paged-allocator business Candidate.

The controller forks and reaps the measured process so correctness and performance
evidence carry a real procfs identity.  The child imports the installed SGLang allocator
or the read-only startup Overlay selected by the deployment-owned Docker mount.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from hcuopt.evaluation.m1_protocol import M1HotspotCorrectnessSpec
from hcuopt.evaluation.m1_verifier import (
    M1CacheNamespaceV1,
    M1NormalizedOutputV1,
    M1OutputRecord,
    M1TensorOutput,
)
from hcuopt.measurement.evidence import write_evidence
from hcuopt.measurement.m1_allocator_reference import (
    INITIAL_FREE_PAGE_COUNT,
    PAGE_SIZE,
    build_case,
    input_tensor_record,
)
from hcuopt.measurement.m1_models import (
    M1CacheNamespaceRecord,
    M1DeviceEventRecord,
    M1OverlayImportRecord,
)
from hcuopt.measurement.models import Stage0AdapterProvenance

PROTOCOL = "hcuopt-m1-allocator-worker-v1"
TARGET_CASE_ID = "target-4091-direct-nosort"


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
        raise ValueError("M1 allocator worker commands must be JSON objects")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _proc_stat(process_id: int) -> str:
    return Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8").strip()


def _proc_start_token(stat_line: str, process_id: int) -> str:
    prefix = f"{process_id} ("
    if not stat_line.startswith(prefix):
        raise RuntimeError("procfs identity does not match the measured process")
    closing = stat_line.rfind(")")
    fields = stat_line[closing + 2 :].split()
    if len(fields) < 20:
        raise RuntimeError("procfs identity is incomplete")
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise RuntimeError("procfs starttime is not an integer") from exc
    if start_ticks < 1:
        raise RuntimeError("procfs starttime must be positive")
    return f"linux-proc-startticks:{start_ticks}"


def _load_allocator(expected_hash: str | None):
    import torch
    from sglang.srt.mem_cache.allocator import PagedTokenToKVPoolAllocator

    module_path = Path(sys.modules[PagedTokenToKVPoolAllocator.__module__].__file__).resolve(
        strict=True
    )
    module_hash = _sha256(module_path)
    if expected_hash is not None and module_hash != expected_hash:
        raise RuntimeError("loaded allocator module does not match the frozen Artifact")
    return torch, PagedTokenToKVPoolAllocator, module_path, module_hash


def _new_allocator(torch, allocator_type, *, need_sort: bool):
    allocator = object.__new__(allocator_type)
    allocator.page_size = PAGE_SIZE
    allocator.need_sort = need_sort
    allocator.is_not_in_free_group = True
    allocator.free_group = []
    allocator.debug_mode = False
    allocator.free_pages = torch.arange(
        10_000,
        10_000 + INITIAL_FREE_PAGE_COUNT,
        dtype=torch.int64,
        device="cuda",
    )
    allocator.release_pages = torch.empty((0,), dtype=torch.int64, device="cuda")
    return allocator


def _run_allocator(torch, allocator_type, case) -> tuple[list[int], int, int]:
    free_index = torch.tensor(case.values, dtype=torch.int64, device="cuda")
    allocator = _new_allocator(torch, allocator_type, need_sort=case.need_sort)
    if case.group_splits:
        allocator.free_group_begin()
        start = 0
        for stop in (*case.group_splits, len(case.values)):
            allocator.free(free_index[start:stop])
            start = stop
        allocator.free_group_end()
    else:
        allocator.free(free_index)
    freed = (
        allocator.release_pages
        if case.need_sort
        else allocator.free_pages[: -INITIAL_FREE_PAGE_COUNT]
    )
    sorted_pages = torch.sort(freed).values
    unique_pages = torch.unique(sorted_pages)
    result = [int(value) for value in unique_pages.cpu().tolist()]
    return result, int(freed.numel()), int(allocator.available_size())


def _normalized_correctness(
    torch,
    allocator_type,
    spec: M1HotspotCorrectnessSpec,
) -> M1NormalizedOutputV1:
    records: list[M1OutputRecord] = []
    for case_spec in spec.cases:
        for seed in case_spec.seeds:
            for special in case_spec.special_values:
                case = build_case(case_spec.case_id, seed, special)
                for repeat in range(case_spec.repeats):
                    pages, raw_count, available_size = _run_allocator(
                        torch, allocator_type, case
                    )
                    records.append(
                        M1OutputRecord(
                            case_id=case_spec.case_id,
                            seed=seed,
                            special_value=special,
                            repeat_ordinal=repeat,
                            inputs=(M1TensorOutput.model_validate(input_tensor_record(case)),),
                            outputs=(
                                M1TensorOutput(
                                    name="freed_pages",
                                    shape=(len(pages),),
                                    dtype="int64",
                                    values=tuple(pages),
                                ),
                                M1TensorOutput(
                                    name="raw_freed_page_count",
                                    shape=(1,),
                                    dtype="int64",
                                    values=(raw_count,),
                                ),
                                M1TensorOutput(
                                    name="available_size",
                                    shape=(1,),
                                    dtype="int64",
                                    values=(available_size,),
                                ),
                            ),
                        )
                    )
    torch.cuda.synchronize()
    return M1NormalizedOutputV1(
        schema_version="m1-normalized-kernel-output-v1",
        hotspot_id=spec.hotspot_id,
        records=tuple(records),
    )


def _require_one_hcu(torch) -> None:
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("M1 allocator worker requires exactly one visible HCU")
    torch.cuda.set_device(0)


def _validate_expected_device(torch, args: argparse.Namespace) -> dict[str, object] | None:
    expected = (args.expected_device_pci, args.expected_device_architecture)
    if expected == (None, None):
        return None
    if None in expected:
        raise RuntimeError("M1 device attestation requires both PCI and architecture")
    properties = torch.cuda.get_device_properties(0)
    pci = (
        properties.pci_domain_id,
        properties.pci_bus_id,
        properties.pci_device_id,
    )
    actual_pci = f"{pci[0]:04x}:{pci[1]:02x}:{pci[2]:02x}.0"
    architecture = properties.gcnArchName.split(":")[0]
    if actual_pci != expected[0] or architecture != expected[1]:
        raise RuntimeError("M1 physical PCI or architecture differs from deployment pin")
    return {
        "pci": actual_pci,
        "architecture": architecture,
        "logical_device_index": 0,
    }


def _correctness_child(args: argparse.Namespace) -> int:
    torch, allocator_type, module_path, module_hash = _load_allocator(
        args.expected_artifact_hash
    )
    _require_one_hcu(torch)
    device_identity = _validate_expected_device(torch, args)
    spec = M1HotspotCorrectnessSpec.model_validate_json(args.spec.read_bytes())
    if any(case.inputs[0].name != "free_index" for case in spec.cases):
        raise RuntimeError("allocator correctness spec has an unexpected input contract")
    cache = M1CacheNamespaceV1(
        schema_version="m1-cache-namespace-v1",
        variant=args.variant,
        namespace=args.cache_namespace_id,
        empty_before_execution=not any(args.cache_namespace.iterdir()),
    )
    write_evidence(args.evidence_dir / "cache-namespace.json", cache)
    output = _normalized_correctness(torch, allocator_type, spec)
    write_evidence(args.evidence_dir / "normalized-output.json", output)
    _write_json(
        sys.stdout,
        {
            "protocol": PROTOCOL,
            "event": "correctness_complete",
            "process_id": os.getpid(),
            "module_path": str(module_path),
            "module_hash": module_hash,
            **({"device_identity": device_identity} if device_identity is not None else {}),
        },
    )
    return 0


def _performance_child(args: argparse.Namespace, command_fd: int, response_fd: int) -> int:
    command_stream = os.fdopen(command_fd, "r", encoding="utf-8", buffering=1)
    response_stream = os.fdopen(response_fd, "w", encoding="utf-8", buffering=1)
    try:
        torch, allocator_type, module_path, module_hash = _load_allocator(
            args.expected_artifact_hash
        )
        _require_one_hcu(torch)
        device_identity = _validate_expected_device(torch, args)
        if any(args.cache_namespace.iterdir()):
            raise RuntimeError("M1 acquisition cache namespace is not empty")
        identity_stat = _proc_stat(os.getpid())
        start_token = _proc_start_token(identity_stat, os.getpid())
        namespace_hash = "sha256:" + hashlib.sha256(
            args.cache_namespace_id.encode("utf-8")
        ).hexdigest()
        cache_record = write_evidence(
            args.evidence_dir / "cache-namespace.json",
            M1CacheNamespaceRecord(
                acquisition_ordinal=args.acquisition_ordinal,
                arm=args.arm,
                process_id=os.getpid(),
                process_start_token=start_token,
                namespace_hash=namespace_hash,
                empty_before_execution=True,
            ),
        )
        import_record = None
        if args.arm == "candidate":
            import_record = write_evidence(
                args.evidence_dir / "import-attestation.json",
                M1OverlayImportRecord(
                    acquisition_ordinal=args.acquisition_ordinal,
                    process_id=os.getpid(),
                    process_start_token=start_token,
                    image_digest=args.image_digest,
                    artifact_content_hash=args.expected_artifact_hash,
                ),
            )
        case = build_case(TARGET_CASE_ID, args.workload_seed, "ordinary")
        free_index = torch.tensor(case.values, dtype=torch.int64, device="cuda")
        origin = torch.cuda.Event(enable_timing=True)
        origin.record()
        torch.cuda.synchronize()
        _write_json(
            response_stream,
            {
                "protocol": PROTOCOL,
                "event": "ready",
                "process_id": os.getpid(),
                "process_start_token": start_token,
                "module_path": str(module_path),
                "module_hash": module_hash,
                "namespace_hash": namespace_hash,
                "cache_uri": cache_record.uri,
                "cache_sha256": cache_record.sha256,
                "import_uri": None if import_record is None else import_record.uri,
                "import_sha256": None if import_record is None else import_record.sha256,
                **(
                    {"device_identity": device_identity}
                    if device_identity is not None
                    else {}
                ),
            },
        )
        sample_ordinal = 0
        while True:
            command = _read_json(command_stream)
            if command is None:
                _write_json(
                    sys.stderr,
                    {"protocol": PROTOCOL, "event": "performance_child_command_eof"},
                )
                return 1
            operation = command.get("op")
            if operation == "close":
                _write_json(response_stream, {"protocol": PROTOCOL, "event": "closing"})
                return 0
            if operation == "synchronize":
                torch.cuda.synchronize()
                _write_json(response_stream, {"protocol": PROTOCOL, "event": "synchronized"})
                continue
            if operation == "warmup":
                _run_free_batch(
                    torch,
                    allocator_type,
                    free_index,
                    case.need_sort,
                    int(command.get("iterations", 1)),
                )
                torch.cuda.synchronize()
                _write_json(response_stream, {"protocol": PROTOCOL, "event": "warmed"})
                continue
            if operation != "measure":
                raise ValueError(f"unsupported M1 worker operation: {operation!r}")
            iterations = int(command["iterations"])
            if iterations < 1:
                raise ValueError("M1 batch iterations must be positive")
            free_index.add_(0)
            started_event = torch.cuda.Event(enable_timing=True)
            finished_event = torch.cuda.Event(enable_timing=True)
            started_monotonic_ns = time.monotonic_ns()
            started_event.record()
            _run_free_batch(
                torch,
                allocator_type,
                free_index,
                case.need_sort,
                iterations,
            )
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
                raise RuntimeError("M1 HCU timing interval is not positive")
            event = write_evidence(
                args.evidence_dir / f"device-event-{sample_ordinal:04d}.json",
                M1DeviceEventRecord(
                    arm=args.arm,
                    acquisition_ordinal=args.acquisition_ordinal,
                    sample_ordinal=sample_ordinal,
                    process_id=os.getpid(),
                    process_start_token=start_token,
                    batch_iterations=iterations,
                    started_monotonic_ns=started_monotonic_ns,
                    finished_monotonic_ns=finished_monotonic_ns,
                    started_device_ticks=started_ticks,
                    finished_device_ticks=finished_ticks,
                    timer_provenance=Stage0AdapterProvenance(
                        profile=args.profile,
                        capability="measurement_harness",
                        adapter_name=args.harness_name,
                        adapter_version=args.harness_version,
                        implementation_kind="real",
                    ),
                ),
            )
            _write_json(
                response_stream,
                {
                    "protocol": PROTOCOL,
                    "event": "measured",
                    "sample_ordinal": sample_ordinal,
                    "event_uri": event.uri,
                    "event_sha256": event.sha256,
                },
            )
            sample_ordinal += 1
    except BaseException as exc:
        error = {
            "protocol": PROTOCOL,
            "event": "performance_child_error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        }
        _write_json(sys.stderr, error)
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


def _run_free_batch(torch, allocator_type, free_index, need_sort: bool, iterations: int) -> None:
    initial_free = torch.arange(
        10_000,
        10_000 + INITIAL_FREE_PAGE_COUNT,
        dtype=torch.int64,
        device="cuda",
    )
    empty = torch.empty((0,), dtype=torch.int64, device="cuda")
    allocator = _new_allocator(torch, allocator_type, need_sort=need_sort)
    for _ in range(iterations):
        allocator.free_pages = initial_free
        allocator.release_pages = empty
        allocator.is_not_in_free_group = True
        allocator.free_group = []
        allocator.free(free_index)


def _controller(args: argparse.Namespace) -> int:
    parent_read, child_write = os.pipe()
    child_read, parent_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(parent_read)
        os.close(parent_write)
        code = _performance_child(args, child_read, child_write)
        os._exit(code)
    os.close(child_read)
    os.close(child_write)
    commands = os.fdopen(parent_write, "w", encoding="utf-8", buffering=1)
    responses = os.fdopen(parent_read, "r", encoding="utf-8", buffering=1)
    wait_status: int | None = None
    stat_line = _proc_stat(child_pid)
    try:
        ready = _read_json(responses)
        if ready is None or ready.get("event") != "ready":
            raise RuntimeError(f"M1 measured child failed to become ready: {ready!r}")
        controller_pid_namespace = os.readlink("/proc/self/ns/pid")
        process_pid_namespace = os.readlink(f"/proc/{child_pid}/ns/pid")
        if controller_pid_namespace != process_pid_namespace:
            raise RuntimeError("M1 controller and measured child PID namespaces differ")
        _write_json(
            sys.stdout,
            {
                **ready,
                "observer_process_id": os.getpid(),
                "proc_stat_line": stat_line,
                "controller_pid_namespace": controller_pid_namespace,
                "process_pid_namespace": process_pid_namespace,
            },
        )
        while True:
            command = _read_json(sys.stdin)
            if command is None:
                _write_json(
                    sys.stderr,
                    {"protocol": PROTOCOL, "event": "performance_controller_stdin_eof"},
                )
                return 1
            _write_json(commands, command)
            response = _read_json(responses)
            if response is None:
                raise RuntimeError("M1 measured child closed its evidence pipe")
            if response.get("event") == "error":
                raise RuntimeError(str(response.get("error", "M1 measured child failed")))
            if command.get("op") == "close":
                waited_pid, wait_status = os.waitpid(child_pid, 0)
                _write_json(
                    sys.stdout,
                    {
                        **response,
                        "observer_process_id": os.getpid(),
                        "process_id": child_pid,
                        "proc_stat_line": stat_line,
                        "waitpid_result_pid": waited_pid,
                        "wait_status": wait_status,
                    },
                )
                return 0 if os.waitstatus_to_exitcode(wait_status) == 0 else 1
            _write_json(sys.stdout, response)
    except BaseException as exc:
        _write_json(
            sys.stderr,
            {
                "protocol": PROTOCOL,
                "event": "performance_controller_error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            },
        )
        raise
    finally:
        commands.close()
        responses.close()
        if wait_status is None:
            try:
                os.kill(child_pid, 15)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass
def _correctness_controller(args: argparse.Namespace) -> int:
    child_pid = os.fork()
    if child_pid == 0:
        try:
            code = _correctness_child(args)
        except BaseException as exc:
            _write_json(
                sys.stdout,
                {
                    "protocol": PROTOCOL,
                    "event": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                },
            )
            code = 1
        os._exit(code)
    stat_line = _proc_stat(child_pid)
    start_ns = time.monotonic_ns()
    _write_json(
        sys.stdout,
        {
            "protocol": PROTOCOL,
            "event": "started",
            "observer_process_id": os.getpid(),
            "process_id": child_pid,
            "proc_stat_line": stat_line,
            "captured_monotonic_ns": start_ns,
        },
    )
    waited_pid, wait_status = os.waitpid(child_pid, 0)
    _write_json(
        sys.stdout,
        {
            "protocol": PROTOCOL,
            "event": "reaped",
            "observer_process_id": os.getpid(),
            "process_id": child_pid,
            "proc_stat_line": stat_line,
            "captured_monotonic_ns": time.monotonic_ns(),
            "waitpid_result_pid": waited_pid,
            "wait_status": wait_status,
        },
    )
    return 0 if os.waitstatus_to_exitcode(wait_status) == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--performance-controller", action="store_true")
    mode.add_argument("--correctness-controller", action="store_true")
    parser.add_argument("--arm", choices=("baseline", "candidate"))
    parser.add_argument("--variant", choices=("reference", "candidate"))
    parser.add_argument("--acquisition-ordinal", type=int, default=0)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--harness-name", default="M1TrustedMeasurementHarness")
    parser.add_argument("--harness-version", default="1")
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--expected-artifact-hash")
    parser.add_argument("--expected-device-pci")
    parser.add_argument("--expected-device-architecture")
    parser.add_argument("--cache-namespace", required=True, type=Path)
    parser.add_argument("--cache-namespace-id", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--workload-seed", type=int, default=20260825)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.name != "posix" or not hasattr(os, "fork"):
        raise SystemExit("M1 allocator worker requires POSIX fork/waitpid")
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    args.cache_namespace.mkdir(parents=True, exist_ok=True)
    if args.performance_controller:
        if args.arm is None or args.variant is not None or args.spec is not None:
            raise SystemExit("performance mode requires only an arm")
        if (args.arm == "candidate") != bool(args.expected_artifact_hash):
            raise SystemExit("Candidate performance mode requires the Artifact hash")
        return _controller(args)
    if args.variant is None or args.arm is not None or args.spec is None:
        raise SystemExit("correctness mode requires a variant and correctness spec")
    if (args.variant == "candidate") != bool(args.expected_artifact_hash):
        raise SystemExit("Candidate correctness mode requires the Artifact hash")
    return _correctness_controller(args)


if __name__ == "__main__":
    raise SystemExit(main())
