#!/usr/bin/env python3
"""Run one non-streaming SGLang endpoint acquisition with raw request evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from hcuopt.evaluation import sglang_smoke_runner as smoke

PROTOCOL_VERSION = "sglang-endpoint-acquisition-v1"
EXPECTED_FIELDS = frozenset(
    {
        "protocol_version",
        "arm",
        "acquisition_ordinal",
        "warmup_requests",
        "measured_requests",
        "expected_prompt_tokens",
        "expected_completion_tokens",
        "ignore_eos",
        "activation_attestation",
        "smoke_spec",
    }
)


class EndpointRunnerError(RuntimeError):
    """Stable fail-closed endpoint acquisition error."""


def validate_spec(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise EndpointRunnerError("endpoint acquisition spec must be an object")
    fields = set(raw)
    missing = sorted(EXPECTED_FIELDS - fields)
    unknown = sorted(fields - EXPECTED_FIELDS)
    if missing:
        raise EndpointRunnerError(f"endpoint spec is missing fields: {', '.join(missing)}")
    if unknown:
        raise EndpointRunnerError(f"endpoint spec has unknown fields: {', '.join(unknown)}")
    spec = dict(raw)
    if spec["protocol_version"] != PROTOCOL_VERSION:
        raise EndpointRunnerError("unsupported endpoint acquisition protocol")
    if spec["arm"] not in {"baseline", "candidate"}:
        raise EndpointRunnerError("endpoint arm must be baseline or candidate")
    for name, minimum, maximum in (
        ("acquisition_ordinal", 0, 1_000_000),
        ("warmup_requests", 1, 10_000),
        ("measured_requests", 1, 100_000),
        ("expected_prompt_tokens", 1, 1_000_000),
        ("expected_completion_tokens", 1, 1_000_000),
    ):
        value = spec[name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise EndpointRunnerError(f"endpoint {name} is outside the allowed range")
    if spec["ignore_eos"] is not True:
        raise EndpointRunnerError("endpoint acquisition v1 requires ignore_eos=true")
    activation = spec["activation_attestation"]
    if activation is not None:
        if not isinstance(activation, Mapping) or set(activation) != {
            "module_name",
            "module_path",
            "expected_sha256",
        }:
            raise EndpointRunnerError("endpoint activation attestation is malformed")
        if (
            not isinstance(activation["module_name"], str)
            or not activation["module_name"]
            or not isinstance(activation["module_path"], str)
            or not os.path.isabs(activation["module_path"])
            or not isinstance(activation["expected_sha256"], str)
            or len(activation["expected_sha256"]) != 71
            or not activation["expected_sha256"].startswith("sha256:")
        ):
            raise EndpointRunnerError("endpoint activation attestation is malformed")
        spec["activation_attestation"] = dict(activation)
    smoke_spec = smoke.validate_spec(spec["smoke_spec"])
    if smoke_spec["stream"] is not False:
        raise EndpointRunnerError("endpoint acquisition v1 requires non-streaming requests")
    spec["smoke_spec"] = smoke_spec
    return spec


def run_acquisition(
    raw_spec: Mapping[str, Any],
    evidence_dir: Path,
    *,
    server_argv_override: Sequence[str] | None = None,
    formal_lifecycle: bool = False,
    cache_parent: Path = Path("/tmp"),
) -> dict[str, Any]:
    smoke._prepare_evidence_dir(evidence_dir)
    spec_path = evidence_dir / "spec.json"
    environment_path = evidence_dir / "environment.json"
    start_path = evidence_dir / "start.json"
    ready_path = evidence_dir / "ready.jsonl"
    stop_path = evidence_dir / "stop.json"
    result_path = evidence_dir / "result.json"
    server_log_path = evidence_dir / "server.log"
    smoke._atomic_write_json(spec_path, dict(raw_spec))
    smoke._atomic_write_json(environment_path, smoke._environment_record())

    ready_temporary, ready_handle = smoke._open_text_stream(ready_path)
    try:
        log_temporary, log_handle = smoke._open_binary_stream(server_log_path)
    except Exception:
        ready_handle.close()
        ready_temporary.unlink(missing_ok=True)
        raise

    process: subprocess.Popen[bytes] | None = None
    process_group_id: int | None = None
    primary_error: dict[str, str] | None = None
    stop_record: dict[str, Any] = {
        "status": "not_started",
        "term_sent": False,
        "kill_sent": False,
        "cleanup_succeeded": True,
        "exit_code": None,
        "finished_at": None,
        "error": None,
    }
    start_record: dict[str, Any] = {
        "status": "not_started",
        "pid": None,
        "process_group_id": None,
        "argv": None,
    }
    smoke._atomic_write_json(start_path, start_record)
    smoke._atomic_write_json(stop_path, stop_record)
    completed_requests = 0
    cache_root: Path | None = None
    cache_cleanup_succeeded = False
    spec: dict[str, Any] | None = None
    finalization_errors: list[str] = []
    try:
        spec = validate_spec(raw_spec)
        smoke._atomic_write_json(spec_path, spec)
        cache_root = _prepare_cache_namespace(
            evidence_dir,
            cache_parent=cache_parent,
            acquisition_ordinal=spec["acquisition_ordinal"],
        )
        smoke_spec = spec["smoke_spec"]
        argv = list(server_argv_override or smoke.server_argv(smoke_spec))
        launched_argv = argv
        if formal_lifecycle:
            if os.name != "posix" or not sys.platform.startswith("linux"):
                raise EndpointRunnerError("formal endpoint lifecycle requires Linux")
            launched_argv = smoke._lifecycle_wrapper_argv(
                argv,
                evidence_dir=evidence_dir,
                restart_ordinal=spec["acquisition_ordinal"],
            )
        popen_options: dict[str, Any] = {
            "args": launched_argv,
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "shell": False,
        }
        if os.name == "posix":
            popen_options["start_new_session"] = True
        elif os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(**popen_options)
        process_group_id = process.pid if os.name == "posix" else None
        server_pid = process.pid
        if formal_lifecycle:
            lifecycle = smoke._wait_for_lifecycle_start(
                evidence_dir / "process-start.json", process
            )
            server_pid = int(lifecycle["process_id"])
            process_group_id = server_pid
        start_record = {
            "status": "started",
            "pid": server_pid,
            "process_group_id": process_group_id,
            "argv": argv,
        }
        smoke._atomic_write_json(start_path, start_record)

        opener = build_opener(ProxyHandler({}))
        smoke._wait_until_ready(smoke_spec, process, opener, ready_handle)
        _require_activation_attestation(spec, evidence_dir)
        for ordinal in range(spec["warmup_requests"]):
            _run_request(
                smoke_spec,
                opener,
                evidence_dir / "warmup" / f"{ordinal:04d}",
                ordinal=ordinal,
                measured=False,
                expected_prompt_tokens=spec["expected_prompt_tokens"],
                expected_completion_tokens=spec["expected_completion_tokens"],
                ignore_eos=spec["ignore_eos"],
            )
            _require_server_alive(process)
        for ordinal in range(spec["measured_requests"]):
            _run_request(
                smoke_spec,
                opener,
                evidence_dir / "requests" / f"{ordinal:04d}",
                ordinal=ordinal,
                measured=True,
                expected_prompt_tokens=spec["expected_prompt_tokens"],
                expected_completion_tokens=spec["expected_completion_tokens"],
                ignore_eos=spec["ignore_eos"],
            )
            completed_requests += 1
            _require_server_alive(process)
    except Exception as exc:
        primary_error = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            stop_record = smoke._stop_process(
                process,
                process_group_id,
                spec["smoke_spec"] if spec is not None else None,
            )
        except Exception as exc:
            stop_record.update(
                status="failed",
                cleanup_succeeded=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            finalization_errors.append(f"stop process: {type(exc).__name__}: {exc}")
        try:
            smoke._atomic_write_json(stop_path, stop_record)
        except Exception as exc:
            finalization_errors.append(f"write stop.json: {type(exc).__name__}: {exc}")
        for label, handle, temporary, final_path in (
            ("ready.jsonl", ready_handle, ready_temporary, ready_path),
            ("server.log", log_handle, log_temporary, server_log_path),
        ):
            try:
                smoke._finalize_stream(handle, temporary, final_path)
            except Exception as exc:
                finalization_errors.append(f"finalize {label}: {type(exc).__name__}: {exc}")
        try:
            cache_cleanup_succeeded = _cleanup_cache_namespace(cache_root, cache_parent)
            smoke._atomic_write_json(
                evidence_dir / "cache-cleanup.json",
                {
                    "cache_root": str(cache_root) if cache_root is not None else None,
                    "removed": cache_cleanup_succeeded,
                },
            )
        except Exception as exc:
            cache_cleanup_succeeded = False
            finalization_errors.append(f"cleanup cache: {type(exc).__name__}: {exc}")

    if not stop_record.get("cleanup_succeeded") and primary_error is None:
        primary_error = {
            "type": "EndpointCleanupError",
            "message": "endpoint server cleanup was not confirmed",
        }
    if not cache_cleanup_succeeded and primary_error is None:
        primary_error = {
            "type": "EndpointCacheCleanupError",
            "message": "endpoint cache namespace cleanup was not confirmed",
        }
    if finalization_errors and primary_error is None:
        primary_error = {
            "type": "EvidenceFinalizationError",
            "message": "; ".join(finalization_errors),
        }
    elif finalization_errors:
        primary_error["message"] += "; evidence finalization: " + "; ".join(
            finalization_errors
        )
    expected = spec["measured_requests"] if spec is not None else 0
    succeeded = primary_error is None and completed_requests == expected
    result = {
        "protocol_version": PROTOCOL_VERSION,
        "status": "succeeded" if succeeded else "failed",
        "run_mode": "provisional",
        "completed_requests": completed_requests,
        "expected_requests": expected,
        "cleanup_succeeded": bool(stop_record.get("cleanup_succeeded")),
        "cache_cleanup_succeeded": cache_cleanup_succeeded,
        "error": primary_error,
        "producer_verdict": None,
        "automatic_release_allowed": False,
    }
    smoke._atomic_write_json(result_path, result)
    return result


def _prepare_cache_namespace(
    evidence_dir: Path,
    *,
    cache_parent: Path,
    acquisition_ordinal: int,
) -> Path:
    parent = cache_parent.resolve(strict=True)
    if not parent.is_dir():
        raise EndpointRunnerError("endpoint cache parent is not a directory")
    identifier = uuid4()
    root = parent / f"hcuopt-endpoint-cache-{identifier}"
    if root.exists() or root.is_symlink() or root.parent != parent:
        raise EndpointRunnerError("endpoint cache namespace is not fresh")
    root.mkdir(mode=0o700)
    paths = {
        "xdg": root / "xdg",
        "huggingface": root / "huggingface",
        "triton": root / "triton",
    }
    for path in paths.values():
        path.mkdir(mode=0o700)
        if any(path.iterdir()):
            raise EndpointRunnerError("endpoint cache namespace is not empty")
    os.environ["XDG_CACHE_HOME"] = str(paths["xdg"])
    os.environ["HF_HOME"] = str(paths["huggingface"])
    os.environ["TRITON_CACHE_DIR"] = str(paths["triton"])
    identity = {
        "schema_version": "sglang-endpoint-cache-v1",
        "acquisition_ordinal": acquisition_ordinal,
        "namespace_id": str(identifier),
        "cache_root": str(root),
        "paths": {name: str(path) for name, path in sorted(paths.items())},
        "empty_before_start": True,
        "device": root.stat().st_dev,
        "inode": root.stat().st_ino,
    }
    encoded = json.dumps(
        identity, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    identity["namespace_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    smoke._atomic_write_json(evidence_dir / "cache-namespace.json", identity)
    return root


def _cleanup_cache_namespace(cache_root: Path | None, cache_parent: Path) -> bool:
    if cache_root is None:
        return True
    parent = cache_parent.resolve(strict=True)
    if (
        cache_root.parent != parent
        or not cache_root.name.startswith("hcuopt-endpoint-cache-")
        or cache_root.is_symlink()
    ):
        raise EndpointRunnerError("refusing to clean an unowned endpoint cache")
    if cache_root.exists():
        shutil.rmtree(cache_root)
    return not cache_root.exists()


def _require_activation_attestation(spec: Mapping[str, Any], evidence_dir: Path) -> None:
    expected = spec["activation_attestation"]
    if expected is None:
        return
    path = evidence_dir / "activation.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise EndpointRunnerError("endpoint activation attestation is absent or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EndpointRunnerError("endpoint activation attestation is invalid") from exc
    if (
        value.get("schema_version") != "sglang-endpoint-import-attestation-v1"
        or value.get("module_name") != expected["module_name"]
        or value.get("module_path") != expected["module_path"]
        or value.get("module_sha256") != expected["expected_sha256"]
        or type(value.get("process_id")) is not int
        or value["process_id"] < 1
        or type(value.get("captured_monotonic_ns")) is not int
    ):
        raise EndpointRunnerError("endpoint activation attestation differs from the frozen spec")


def _run_request(
    smoke_spec: Mapping[str, Any],
    opener: Any,
    directory: Path,
    *,
    ordinal: int,
    measured: bool,
    expected_prompt_tokens: int,
    expected_completion_tokens: int,
    ignore_eos: bool,
) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    request_record = {
        "request_ordinal": ordinal,
        "measured": measured,
        "method": "POST",
        "url": smoke._url(smoke_spec, str(smoke_spec["generate_path"])),
        "body": _request_payload(smoke_spec, ignore_eos=ignore_eos),
    }
    smoke._atomic_write_json(directory / "request.json", request_record)
    started = time.monotonic_ns()
    envelope: dict[str, Any] | None = None
    try:
        envelope, normalized = _generate_once(
            smoke_spec,
            opener,
            ignore_eos=ignore_eos,
        )
        finished = time.monotonic_ns()
        smoke._atomic_write_json(directory / "response.json", envelope)
        sample = {
            "request_ordinal": ordinal,
            "measured": measured,
            "succeeded": True,
            "started_monotonic_ns": started,
            "finished_monotonic_ns": finished,
            "e2e_latency_ns": finished - started,
            "http_status": envelope["http_status"],
            "prompt_tokens": normalized["prompt_tokens"],
            "completion_tokens": normalized["completion_tokens"],
            "finish_reason": normalized["finish_reason_type"],
            "error": None,
        }
        smoke._atomic_write_json(directory / "sample.json", sample)
        if (
            normalized["prompt_tokens"] != expected_prompt_tokens
            or normalized["completion_tokens"] != expected_completion_tokens
        ):
            raise EndpointRunnerError("endpoint response token counts differ from the frozen spec")
    except Exception as exc:
        finished = time.monotonic_ns()
        if isinstance(exc, smoke.GenerateResponseError):
            envelope = exc.envelope
        if envelope is None:
            envelope = {
                "attempted": True,
                "http_status": None,
                "body_text": "",
                "body_json": None,
                "parse_error": str(exc),
                "truncated": False,
            }
        smoke._atomic_write_json(directory / "response.json", envelope)
        smoke._atomic_write_json(
            directory / "sample.json",
            {
                "request_ordinal": ordinal,
                "measured": measured,
                "succeeded": False,
                "started_monotonic_ns": started,
                "finished_monotonic_ns": finished,
                "e2e_latency_ns": finished - started,
                "http_status": envelope.get("http_status"),
                "prompt_tokens": None,
                "completion_tokens": None,
                "finish_reason": None,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            },
        )
        raise


def _request_payload(smoke_spec: Mapping[str, Any], *, ignore_eos: bool) -> dict[str, Any]:
    payload = smoke.request_payload(smoke_spec)
    payload["sampling_params"]["ignore_eos"] = ignore_eos
    return payload


def _generate_once(
    smoke_spec: Mapping[str, Any], opener: Any, *, ignore_eos: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded = json.dumps(
        _request_payload(smoke_spec, ignore_eos=ignore_eos),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        smoke._url(smoke_spec, str(smoke_spec["generate_path"])),
        data=encoded,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        response = opener.open(request, timeout=float(smoke_spec["request_timeout_seconds"]))
    except HTTPError as exc:
        envelope = smoke._response_envelope(exc)
        exc.close()
        raise smoke.GenerateResponseError(
            f"generate request returned HTTP {envelope['http_status']}", envelope
        ) from exc
    with response:
        envelope = smoke._response_envelope(response)
    if envelope["http_status"] != 200:
        raise smoke.GenerateResponseError(
            f"generate request returned HTTP {envelope['http_status']}", envelope
        )
    if envelope["truncated"] or envelope["parse_error"] is not None:
        raise smoke.GenerateResponseError(
            envelope["parse_error"] or "generate response is invalid", envelope
        )
    try:
        normalized = smoke.normalize_response(envelope["body_json"])
    except smoke.SmokeRunnerError as exc:
        raise smoke.GenerateResponseError(str(exc), envelope) from exc
    return envelope, normalized


def _require_server_alive(process: subprocess.Popen[bytes]) -> None:
    exit_code = process.poll()
    if exit_code is not None:
        raise EndpointRunnerError(
            f"SGLang exited during endpoint acquisition with code {exit_code}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        raw = json.loads(arguments.spec.read_text(encoding="utf-8"))
        result = run_acquisition(raw, arguments.evidence_dir, formal_lifecycle=True)
    except Exception as exc:
        result = {
            "protocol_version": PROTOCOL_VERSION,
            "status": "failed",
            "run_mode": "provisional",
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "producer_verdict": None,
            "automatic_release_allowed": False,
        }
    print(json.dumps(result, allow_nan=False, sort_keys=True))
    return 0 if result.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
