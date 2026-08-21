#!/usr/bin/env python3
"""Run one SGLang framework-smoke variant and preserve lifecycle evidence.

This file intentionally uses only the Python 3.10 standard library.  It is
mounted read-only into the locked inference image and invoked by file path, so
it must not depend on the hcuopt package being installed in that image.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

PROTOCOL_VERSION = "sglang-smoke-v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
READY_BODY_LIMIT = 64 * 1024
SAFE_ENVIRONMENT_KEYS = (
    "HIP_VISIBLE_DEVICES",
    "HSA_VISIBLE_DEVICES",
    "OMP_NUM_THREADS",
    "ROCR_VISIBLE_DEVICES",
)
EXPECTED_SPEC_FIELDS = frozenset(
    {
        "protocol_version",
        "workload_id",
        "target_id",
        "model_path",
        "served_model_name",
        "host",
        "port",
        "tensor_parallel_size",
        "prompt",
        "temperature",
        "max_new_tokens",
        "sampling_seed",
        "stream",
        "ready_path",
        "generate_path",
        "ready_timeout_seconds",
        "ready_poll_interval_seconds",
        "request_timeout_seconds",
        "stop_grace_seconds",
        "execution_timeout_seconds",
        "runner_python_executable",
        "server_entrypoint",
        "server_subcommand",
        "trust_remote_code",
        "attention_backend",
        "page_size",
        "mem_fraction_static",
        "cookbook_repository",
        "cookbook_commit",
        "cookbook_paths",
    }
)


class SmokeRunnerError(RuntimeError):
    """A stable, user-actionable runner failure."""


class SpecValidationError(SmokeRunnerError):
    """The resolved workload spec is not the strict v1 schema."""


class GenerateResponseError(SmokeRunnerError):
    def __init__(self, message: str, envelope: dict[str, Any]) -> None:
        super().__init__(message)
        self.envelope = envelope


class WrapperSignal(SmokeRunnerError):
    def __init__(self, signum: int) -> None:
        super().__init__(f"wrapper received signal {signum}")
        self.signum = signum


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_spec(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SpecValidationError("spec must be a JSON object")
    fields = set(raw)
    missing = sorted(EXPECTED_SPEC_FIELDS - fields)
    unknown = sorted(fields - EXPECTED_SPEC_FIELDS)
    if missing:
        raise SpecValidationError(f"spec is missing fields: {', '.join(missing)}")
    if unknown:
        raise SpecValidationError(f"spec has unknown fields: {', '.join(unknown)}")

    spec = dict(raw)
    _require_exact(spec, "protocol_version", PROTOCOL_VERSION)
    _require_nonempty_string(spec, "workload_id")
    _require_nonempty_string(spec, "target_id")
    model_path = _require_nonempty_string(spec, "model_path")
    if not model_path.startswith("/"):
        raise SpecValidationError("model_path must be an absolute POSIX path")
    _require_nonempty_string(spec, "served_model_name")
    _require_exact(spec, "host", "127.0.0.1")
    _require_int(spec, "port", minimum=1, maximum=65_535)
    _require_int(spec, "tensor_parallel_size", minimum=1)
    _require_nonempty_string(spec, "prompt")

    temperature = _require_number(spec, "temperature", minimum=0.0)
    if temperature != 0.0:
        raise SpecValidationError("framework smoke requires temperature=0.0")
    _require_int(spec, "max_new_tokens", minimum=1, maximum=4_096)
    _require_int(spec, "sampling_seed", minimum=0)
    if not isinstance(spec["stream"], bool) or spec["stream"]:
        raise SpecValidationError("framework smoke requires stream=false")

    _require_exact(spec, "ready_path", "/health_generate")
    _require_exact(spec, "generate_path", "/generate")
    ready_timeout = _require_number(spec, "ready_timeout_seconds", minimum=0.001, maximum=1_800)
    _require_number(
        spec,
        "ready_poll_interval_seconds",
        minimum=0.001,
        maximum=30,
    )
    request_timeout = _require_number(spec, "request_timeout_seconds", minimum=0.001, maximum=600)
    stop_grace = _require_number(spec, "stop_grace_seconds", minimum=0.001, maximum=120)
    execution_timeout = _require_int(spec, "execution_timeout_seconds", minimum=1, maximum=86_400)
    if execution_timeout <= ready_timeout + request_timeout + stop_grace:
        raise SpecValidationError(
            "execution_timeout_seconds must exceed ready, request, and stop timeouts"
        )
    _require_exact(spec, "runner_python_executable", "python")
    _require_exact(spec, "server_entrypoint", "sglang")
    _require_exact(spec, "server_subcommand", "serve")
    _require_exact(spec, "trust_remote_code", True)
    _require_exact(spec, "attention_backend", "fa3")
    _require_exact(spec, "page_size", 64)
    mem_fraction_static = _require_number(spec, "mem_fraction_static", minimum=0.001, maximum=0.999)
    if mem_fraction_static != 0.85:
        raise SpecValidationError("mem_fraction_static must equal 0.85")
    _require_exact(
        spec,
        "cookbook_repository",
        "https://github.com/HYGON-AI/inference-cookbook-das",
    )
    cookbook_commit = _require_nonempty_string(spec, "cookbook_commit")
    if len(cookbook_commit) != 40 or any(
        character not in "0123456789abcdef" for character in cookbook_commit
    ):
        raise SpecValidationError("cookbook_commit must be a lowercase 40-character commit")
    cookbook_paths = spec["cookbook_paths"]
    if (
        not isinstance(cookbook_paths, list)
        or not cookbook_paths
        or any(not isinstance(path, str) or not path for path in cookbook_paths)
    ):
        raise SpecValidationError("cookbook_paths must be a non-empty list of strings")
    if any(path.startswith("/") or ".." in path.split("/") for path in cookbook_paths):
        raise SpecValidationError("cookbook_paths must contain clean relative paths")
    return spec


def server_argv(spec: Mapping[str, Any]) -> list[str]:
    return [
        str(spec["server_entrypoint"]),
        str(spec["server_subcommand"]),
        "--model-path",
        str(spec["model_path"]),
        "--served-model-name",
        str(spec["served_model_name"]),
        "--host",
        str(spec["host"]),
        "--port",
        str(spec["port"]),
        "--tp-size",
        str(spec["tensor_parallel_size"]),
        "--trust-remote-code",
        "--attention-backend",
        str(spec["attention_backend"]),
        "--page-size",
        str(spec["page_size"]),
        "--mem-fraction-static",
        str(spec["mem_fraction_static"]),
    ]


def request_payload(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": spec["prompt"],
        "sampling_params": {
            "temperature": spec["temperature"],
            "max_new_tokens": spec["max_new_tokens"],
            "sampling_seed": spec["sampling_seed"],
        },
        "stream": spec["stream"],
    }


def normalize_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise SmokeRunnerError("response must be a JSON object")
    text = response.get("text")
    meta = response.get("meta_info")
    if not isinstance(text, str):
        raise SmokeRunnerError("response.text must be a string")
    if not isinstance(meta, dict):
        raise SmokeRunnerError("response.meta_info must be an object")
    finish_reason = meta.get("finish_reason")
    if not isinstance(finish_reason, dict):
        raise SmokeRunnerError("response.meta_info.finish_reason must be an object")
    finish_type = finish_reason.get("type")
    if not isinstance(finish_type, str) or not finish_type:
        raise SmokeRunnerError("response finish_reason.type must be a non-empty string")
    prompt_tokens = _response_token_count(meta, "prompt_tokens")
    completion_tokens = _response_token_count(meta, "completion_tokens")
    return {
        "text": text,
        "finish_reason_type": finish_type,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def run_smoke(
    raw_spec: Mapping[str, Any],
    evidence_dir: Path,
    *,
    server_argv_override: Sequence[str] | None = None,
    enable_child_subreaper: bool = False,
    formal_lifecycle: bool = False,
    restart_ordinal: int | None = None,
) -> dict[str, Any]:
    """Run exactly one baseline or no-op variant in a fresh server process."""

    _prepare_evidence_dir(evidence_dir)
    paths = {name: evidence_dir / name for name in _evidence_file_names()}
    ready_temporary, ready_handle = _open_text_stream(paths["ready.jsonl"])
    try:
        log_temporary, log_handle = _open_binary_stream(paths["server.log"])
    except Exception:
        ready_handle.close()
        ready_temporary.unlink(missing_ok=True)
        raise

    process: subprocess.Popen[bytes] | None = None
    process_group_id: int | None = None
    child_subreaper_enabled = _set_child_subreaper(True) if enable_child_subreaper else False
    normalized: dict[str, Any] | None = None
    primary_error: dict[str, str] | None = None
    start_record: dict[str, Any] = {
        "status": "not_started",
        "started_at": None,
        "argv": None,
        "pid": None,
        "process_group_id": None,
        "child_subreaper_enabled": child_subreaper_enabled,
    }
    request_record: dict[str, Any] = {
        "attempted": False,
        "method": "POST",
        "url": None,
        "body": None,
    }
    response_record: dict[str, Any] = _empty_response_record("request was not attempted")
    stop_record: dict[str, Any] = {
        "status": "not_started",
        "term_sent": False,
        "kill_sent": False,
        "cleanup_succeeded": True,
        "exit_code": None,
        "finished_at": None,
        "error": None,
    }
    previous_signal_handlers, signal_state = _install_termination_signal_handlers()

    spec: dict[str, Any] | None = None
    try:
        _atomic_write_json(paths["spec.json"], dict(raw_spec))
        _atomic_write_json(paths["environment.json"], _environment_record())
        _atomic_write_json(paths["start.json"], start_record)
        _atomic_write_json(paths["request.json"], request_record)
        _atomic_write_json(paths["response.json"], response_record)
        _atomic_write_json(paths["stop.json"], stop_record)
        spec = validate_spec(raw_spec)
        _atomic_write_json(paths["spec.json"], spec)
        argv = _resolve_server_argv(spec, server_argv_override)
        launched_argv = argv
        if formal_lifecycle:
            if os.name != "posix" or not sys.platform.startswith("linux"):
                raise SmokeRunnerError("Formal process lifecycle capture requires Linux")
            if restart_ordinal is None or restart_ordinal < 0:
                raise SmokeRunnerError("Formal process lifecycle capture requires restart_ordinal")
            launched_argv = _lifecycle_wrapper_argv(
                argv,
                evidence_dir=evidence_dir,
                restart_ordinal=restart_ordinal,
            )
        request_record = {
            "attempted": False,
            "method": "POST",
            "url": _url(spec, str(spec["generate_path"])),
            "body": request_payload(spec),
        }
        _atomic_write_json(paths["request.json"], request_record)

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

        with _block_termination_signals():
            process = subprocess.Popen(**popen_options)
            if os.name == "posix":
                process_group_id = process.pid
            server_pid = process.pid
            if formal_lifecycle:
                lifecycle_start = _wait_for_lifecycle_start(
                    evidence_dir / "process-start.json",
                    process,
                )
                server_pid = int(lifecycle_start["process_id"])
                # The lifecycle child starts the real server in its own session.
                # Cleanup targets that server group while leaving the observer alive
                # long enough to persist the raw waitpid result.
                process_group_id = server_pid
            start_record = {
                "status": "started",
                "started_at": utc_now(),
                "argv": argv,
                "pid": server_pid,
                "process_group_id": process_group_id,
                "child_subreaper_enabled": child_subreaper_enabled,
            }
            _atomic_write_json(paths["start.json"], start_record)

        opener = build_opener(ProxyHandler({}))
        _wait_until_ready(spec, process, opener, ready_handle)
        request_record["attempted"] = True
        _atomic_write_json(paths["request.json"], request_record)
        response_record, normalized = _generate_once(spec, opener)
        _atomic_write_json(paths["response.json"], response_record)
    except Exception as exc:
        primary_error = _error_record(exc)
        if isinstance(exc, GenerateResponseError):
            response_record = exc.envelope
            _atomic_write_json(paths["response.json"], response_record)
        elif response_record["attempted"] is False:
            response_record = _empty_response_record(str(exc))
            response_record["attempted"] = request_record["attempted"]
            _atomic_write_json(paths["response.json"], response_record)
    finally:
        signal_state["cleanup"] = True
        finalization_errors: list[str] = []
        try:
            stop_record = _stop_process(process, process_group_id, spec)
        except Exception as exc:
            stop_record.update(
                status="failed",
                cleanup_succeeded=False,
                error=f"{type(exc).__name__}: {exc}",
                finished_at=utc_now(),
            )
            finalization_errors.append(f"stop process: {type(exc).__name__}: {exc}")
        try:
            _atomic_write_json(paths["stop.json"], stop_record)
        except Exception as exc:
            finalization_errors.append(f"write stop.json: {type(exc).__name__}: {exc}")
        for label, handle, temporary, final_path in (
            ("ready.jsonl", ready_handle, ready_temporary, paths["ready.jsonl"]),
            ("server.log", log_handle, log_temporary, paths["server.log"]),
        ):
            try:
                _finalize_stream(handle, temporary, final_path)
            except Exception as exc:
                try:
                    handle.close()
                except Exception:
                    pass
                finalization_errors.append(f"finalize {label}: {type(exc).__name__}: {exc}")
        if finalization_errors and primary_error is None:
            primary_error = {
                "type": "EvidenceFinalizationError",
                "message": "; ".join(finalization_errors),
            }
        elif finalization_errors:
            primary_error["message"] += "; evidence finalization: " + "; ".join(finalization_errors)
        if child_subreaper_enabled:
            _set_child_subreaper(False)
        _restore_signal_handlers(previous_signal_handlers)

    if not stop_record["cleanup_succeeded"]:
        cleanup_error = SmokeRunnerError(
            f"server cleanup failed: {stop_record.get('error') or 'process remains'}"
        )
        if primary_error is None:
            primary_error = _error_record(cleanup_error)

    succeeded = primary_error is None and normalized is not None
    result = {
        "protocol_version": PROTOCOL_VERSION,
        "status": "succeeded" if succeeded else "failed",
        "normalized_output": normalized,
        "error": primary_error,
        "cleanup_succeeded": bool(stop_record["cleanup_succeeded"]),
        "evidence_errors": finalization_errors,
        "evidence_files": [
            name
            for name in _evidence_file_names()
            if name == "result.json" or paths[name].is_file()
        ],
    }
    _atomic_write_json(paths["result.json"], result)
    return result


def _wait_until_ready(
    spec: Mapping[str, Any],
    process: subprocess.Popen[bytes],
    opener: OpenerDirector,
    ready_handle: TextIO,
) -> None:
    deadline = time.monotonic() + float(spec["ready_timeout_seconds"])
    attempt = 0
    url = _url(spec, str(spec["ready_path"]))
    while True:
        attempt += 1
        exit_code = process.poll()
        if exit_code is not None:
            record = {
                "attempt": attempt,
                "observed_at": utc_now(),
                "outcome": "server_exited",
                "exit_code": exit_code,
            }
            _append_jsonl(ready_handle, record)
            raise SmokeRunnerError(f"SGLang exited before ready with code {exit_code}")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _append_jsonl(
                ready_handle,
                {
                    "attempt": attempt,
                    "observed_at": utc_now(),
                    "outcome": "timeout",
                },
            )
            raise SmokeRunnerError("SGLang ready deadline exceeded")

        outcome: dict[str, Any]
        retry = False
        try:
            request = Request(url, method="GET", headers={"Accept": "application/json"})
            with opener.open(request, timeout=min(5.0, remaining)) as response:
                status = int(response.status)
                response.read(READY_BODY_LIMIT + 1)
            if status != 200:
                _append_jsonl(
                    ready_handle,
                    {
                        "attempt": attempt,
                        "observed_at": utc_now(),
                        "outcome": "failed",
                        "http_status": status,
                    },
                )
                raise SmokeRunnerError(f"ready probe returned unexpected HTTP {status}")
            outcome = {
                "attempt": attempt,
                "observed_at": utc_now(),
                "outcome": "ready",
                "http_status": status,
            }
            _append_jsonl(ready_handle, outcome)
            return
        except HTTPError as exc:
            status = int(exc.code)
            exc.read(READY_BODY_LIMIT + 1)
            exc.close()
            retry = status == 503
            outcome = {
                "attempt": attempt,
                "observed_at": utc_now(),
                "outcome": "retry" if retry else "failed",
                "http_status": status,
            }
        except (URLError, TimeoutError, ConnectionError) as exc:
            retry = True
            outcome = {
                "attempt": attempt,
                "observed_at": utc_now(),
                "outcome": "retry",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        _append_jsonl(ready_handle, outcome)
        if not retry:
            raise SmokeRunnerError(f"ready probe failed with HTTP {outcome.get('http_status')}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _append_jsonl(
                ready_handle,
                {
                    "attempt": attempt,
                    "observed_at": utc_now(),
                    "outcome": "timeout",
                },
            )
            raise SmokeRunnerError("SGLang ready deadline exceeded")
        time.sleep(min(float(spec["ready_poll_interval_seconds"]), remaining))


def _generate_once(
    spec: Mapping[str, Any], opener: OpenerDirector
) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded = json.dumps(
        request_payload(spec),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        _url(spec, str(spec["generate_path"])),
        data=encoded,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        response = opener.open(request, timeout=float(spec["request_timeout_seconds"]))
    except HTTPError as exc:
        envelope = _response_envelope(exc)
        exc.close()
        preview = envelope["body_text"][:1_024]
        raise GenerateResponseError(
            f"generate request returned HTTP {envelope['http_status']}: {preview}",
            envelope,
        ) from exc
    with response:
        envelope = _response_envelope(response)
    if envelope["http_status"] != 200:
        raise GenerateResponseError(
            f"generate request returned HTTP {envelope['http_status']}", envelope
        )
    if envelope["truncated"]:
        raise GenerateResponseError(
            f"generate response exceeds {MAX_RESPONSE_BYTES} bytes", envelope
        )
    if envelope["parse_error"] is not None:
        raise GenerateResponseError(
            f"generate response is not valid JSON: {envelope['parse_error']}",
            envelope,
        )
    try:
        normalized = normalize_response(envelope["body_json"])
    except SmokeRunnerError as exc:
        raise GenerateResponseError(str(exc), envelope) from exc
    return envelope, normalized


def _response_envelope(response: Any) -> dict[str, Any]:
    status = int(response.status)
    content_type = response.headers.get("Content-Type")
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    truncated = len(raw) > MAX_RESPONSE_BYTES
    bounded = raw[:MAX_RESPONSE_BYTES]
    parse_error: str | None = None
    body_json: Any = None
    try:
        body_text = bounded.decode("utf-8")
    except UnicodeDecodeError as exc:
        body_text = bounded.decode("utf-8", errors="replace")
        parse_error = f"invalid UTF-8: {exc}"
    if parse_error is None and not truncated:
        try:
            body_json = json.loads(body_text)
        except json.JSONDecodeError as exc:
            parse_error = f"invalid JSON: {exc}"
    if truncated:
        parse_error = f"response exceeds {MAX_RESPONSE_BYTES} bytes"
    return {
        "attempted": True,
        "http_status": status,
        "content_type": content_type,
        "body_text": body_text,
        "body_json": body_json,
        "parse_error": parse_error,
        "truncated": truncated,
    }


def _stop_process(
    process: subprocess.Popen[bytes] | None,
    process_group_id: int | None,
    spec: Mapping[str, Any] | None,
) -> dict[str, Any]:
    grace = float(spec["stop_grace_seconds"]) if spec is not None else 1.0
    record: dict[str, Any] = {
        "status": "not_started" if process is None else "stopping",
        "term_sent": False,
        "kill_sent": False,
        "cleanup_succeeded": process is None,
        "exit_code": None,
        "finished_at": utc_now(),
        "error": None,
    }
    if process is None:
        return record
    try:
        process.poll()
        if os.name == "posix" and process_group_id is not None:
            _stop_posix_group(process, process_group_id, grace, record)
        else:
            _stop_direct_process(process, grace, record)
    except Exception as exc:
        if os.name == "posix" and process_group_id is not None:
            try:
                if _send_posix_signal(process_group_id, process, signal.SIGKILL):
                    record["kill_sent"] = True
                    _wait_for_posix_group_exit(
                        process_group_id,
                        process,
                        max(1.0, min(5.0, grace)),
                    )
            except Exception:
                pass
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["cleanup_succeeded"] = False
    finally:
        try:
            process.poll()
            record["exit_code"] = process.returncode
        except Exception as exc:
            record["error"] = record["error"] or f"{type(exc).__name__}: {exc}"
            record["cleanup_succeeded"] = False
        record["finished_at"] = utc_now()
    return record


def _stop_posix_group(
    process: subprocess.Popen[bytes],
    process_group_id: int,
    grace: float,
    record: dict[str, Any],
) -> None:
    if not _posix_group_exists(process_group_id, process):
        process.wait(timeout=0)
        record.update(status="already_exited", cleanup_succeeded=True)
        return
    if not _send_posix_signal(process_group_id, process, signal.SIGTERM):
        record.update(status="already_exited", cleanup_succeeded=True)
        return
    record["term_sent"] = True
    if _wait_for_posix_group_exit(process_group_id, process, grace):
        record.update(status="terminated", cleanup_succeeded=True)
        return
    if _send_posix_signal(process_group_id, process, signal.SIGKILL):
        record["kill_sent"] = True
    kill_wait = max(1.0, min(5.0, grace))
    stopped = _wait_for_posix_group_exit(process_group_id, process, kill_wait)
    record.update(status="killed" if stopped else "failed", cleanup_succeeded=stopped)
    if not stopped:
        record["error"] = "process group remains after SIGKILL"


def _stop_direct_process(
    process: subprocess.Popen[bytes], grace: float, record: dict[str, Any]
) -> None:
    if process.poll() is not None:
        record.update(status="already_exited", cleanup_succeeded=True)
        return
    process.terminate()
    record["term_sent"] = True
    try:
        process.wait(timeout=grace)
        record.update(status="terminated", cleanup_succeeded=True)
        return
    except subprocess.TimeoutExpired:
        process.kill()
        record["kill_sent"] = True
    try:
        process.wait(timeout=max(1.0, min(5.0, grace)))
        record.update(status="killed", cleanup_succeeded=True)
    except subprocess.TimeoutExpired:
        record.update(
            status="failed",
            cleanup_succeeded=False,
            error="process remains after kill",
        )


def _wait_for_posix_group_exit(
    process_group_id: int, process: subprocess.Popen[bytes], timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        process.poll()
        if not _posix_group_exists(process_group_id, process):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                return False
            return process.returncode is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _posix_group_exists(process_group_id: int, process: subprocess.Popen[bytes]) -> bool:
    process.poll()
    _reap_adopted_children(process)
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _set_child_subreaper(enabled: bool) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        return prctl(36, int(enabled), 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (AttributeError, OSError):
        return False


def _reap_adopted_children(process: subprocess.Popen[bytes]) -> None:
    if os.name != "posix" or process.returncode is None:
        return
    while True:
        try:
            child_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if child_pid == 0:
            return


def _send_posix_signal(
    process_group_id: int,
    process: subprocess.Popen[bytes],
    signum: int,
) -> bool:
    process.poll()
    try:
        os.killpg(process_group_id, signum)
    except ProcessLookupError:
        process.poll()
        return False
    return True


def _install_termination_signal_handlers() -> tuple[dict[int, Any], dict[str, bool]]:
    previous: dict[int, Any] = {}
    state = {"received": False, "cleanup": False}
    if threading.current_thread() is not threading.main_thread():
        return previous, state

    def relay(signum: int, _frame: FrameType | None) -> None:
        if state["cleanup"] or state["received"]:
            return
        state["received"] = True
        raise WrapperSignal(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, relay)
    return previous, state


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


@contextmanager
def _block_termination_signals() -> Iterator[None]:
    if os.name != "posix" or not hasattr(signal, "pthread_sigmask"):
        yield
        return
    blocked = {signal.SIGTERM, signal.SIGINT}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _prepare_evidence_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise SmokeRunnerError(f"evidence path is not a directory: {path}")
        if any(path.iterdir()):
            raise SmokeRunnerError(f"refusing to overwrite non-empty evidence directory: {path}")
    else:
        path.mkdir(parents=True)


def _environment_record() -> dict[str, Any]:
    return {
        "captured_at": utc_now(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "working_directory": os.getcwd(),
        "wrapper_pid": os.getpid(),
        "allowlisted_environment": {
            name: os.environ[name] for name in SAFE_ENVIRONMENT_KEYS if name in os.environ
        },
    }


def _resolve_server_argv(spec: Mapping[str, Any], override: Sequence[str] | None) -> list[str]:
    values = list(override) if override is not None else server_argv(spec)
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise SmokeRunnerError("server argv must contain non-empty strings")
    return values


def _lifecycle_wrapper_argv(
    server_argv: Sequence[str],
    *,
    evidence_dir: Path,
    restart_ordinal: int,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--lifecycle-child",
        "--lifecycle-dir",
        str(evidence_dir),
        "--restart-ordinal",
        str(restart_ordinal),
        "--",
        *server_argv,
    ]


def _wait_for_lifecycle_start(
    path: Path,
    process: subprocess.Popen[bytes],
) -> dict[str, Any]:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(value, dict)
                and value.get("event") == "started"
                and isinstance(value.get("process_id"), int)
                and not isinstance(value.get("process_id"), bool)
            ):
                return value
            raise SmokeRunnerError("Formal process start record is invalid")
        if process.poll() is not None:
            raise SmokeRunnerError("lifecycle wrapper exited before publishing process identity")
        time.sleep(0.01)
    raise SmokeRunnerError("timed out waiting for Formal process identity")


def _lifecycle_child_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lifecycle-child", action="store_true")
    parser.add_argument("--lifecycle-dir", type=Path, required=True)
    parser.add_argument("--restart-ordinal", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not args.lifecycle_child or not command or args.restart_ordinal < 0:
        return 125
    lifecycle_dir = args.lifecycle_dir.resolve(strict=True)
    if lifecycle_dir.is_symlink() or not lifecycle_dir.is_dir():
        return 125
    start_path = lifecycle_dir / "process-start.json"
    exit_path = lifecycle_dir / "process-exit.json"
    if any(path.exists() or path.is_symlink() for path in (start_path, exit_path)):
        return 125

    child = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        shell=False,
        start_new_session=True,
    )
    proc_stat_line = Path(f"/proc/{child.pid}/stat").read_text(encoding="utf-8").strip()
    observer_pid = os.getpid()
    _atomic_write_canonical_json(
        start_path,
        {
            "schema_version": "process-lifecycle-v1",
            "event": "started",
            "restart_ordinal": args.restart_ordinal,
            "observer_process_id": observer_pid,
            "process_id": child.pid,
            "proc_stat_line": proc_stat_line,
            "captured_monotonic_ns": time.monotonic_ns(),
            "waitpid_result_pid": None,
            "wait_status": None,
        },
    )

    def relay(signum: int, _frame: FrameType | None) -> None:
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    previous = {signum: signal.signal(signum, relay) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        while True:
            try:
                waited_pid, wait_status = os.waitpid(child.pid, 0)
                break
            except InterruptedError:
                continue
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    child.returncode = os.waitstatus_to_exitcode(wait_status)
    _atomic_write_canonical_json(
        exit_path,
        {
            "schema_version": "process-lifecycle-v1",
            "event": "reaped",
            "restart_ordinal": args.restart_ordinal,
            "observer_process_id": observer_pid,
            "process_id": child.pid,
            "proc_stat_line": proc_stat_line,
            "captured_monotonic_ns": time.monotonic_ns(),
            "waitpid_result_pid": waited_pid,
            "wait_status": wait_status,
        },
    )
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


def _url(spec: Mapping[str, Any], path: str) -> str:
    return f"http://{spec['host']}:{spec['port']}{path}"


def _empty_response_record(error: str) -> dict[str, Any]:
    return {
        "attempted": False,
        "http_status": None,
        "content_type": None,
        "body_text": None,
        "body_json": None,
        "parse_error": error,
        "truncated": False,
    }


def _error_record(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _response_token_count(meta: Mapping[str, Any], name: str) -> int:
    value = meta.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SmokeRunnerError(f"response meta_info.{name} must be a non-negative int")
    return value


def _require_exact(spec: Mapping[str, Any], field: str, expected: Any) -> None:
    if spec[field] != expected or type(spec[field]) is not type(expected):
        raise SpecValidationError(f"{field} must equal {expected!r}")


def _require_nonempty_string(spec: Mapping[str, Any], field: str) -> str:
    value = spec[field]
    if not isinstance(value, str) or not value:
        raise SpecValidationError(f"{field} must be a non-empty string")
    return value


def _require_int(
    spec: Mapping[str, Any], field: str, *, minimum: int, maximum: int | None = None
) -> int:
    value = spec[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecValidationError(f"{field} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise SpecValidationError(f"{field} is outside the allowed range")
    return value


def _require_number(
    spec: Mapping[str, Any], field: str, *, minimum: float, maximum: float | None = None
) -> float:
    value = spec[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecValidationError(f"{field} must be a number")
    number = float(value)
    if not number == number or number in (float("inf"), float("-inf")):
        raise SpecValidationError(f"{field} must be finite")
    if number < minimum or (maximum is not None and number > maximum):
        raise SpecValidationError(f"{field} is outside the allowed range")
    return number


def _evidence_file_names() -> tuple[str, ...]:
    return (
        "spec.json",
        "environment.json",
        "start.json",
        "server.log",
        "ready.jsonl",
        "request.json",
        "response.json",
        "stop.json",
        "result.json",
    )


def _open_text_stream(final_path: Path) -> tuple[Path, TextIO]:
    temporary = _stream_temporary_path(final_path)
    return temporary, temporary.open("x", encoding="utf-8", newline="\n")


def _open_binary_stream(final_path: Path) -> tuple[Path, Any]:
    temporary = _stream_temporary_path(final_path)
    return temporary, temporary.open("xb")


def _stream_temporary_path(final_path: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{final_path.name}.",
        suffix=".tmp",
        dir=final_path.parent,
        delete=False,
    )
    path = Path(handle.name)
    handle.close()
    path.unlink()
    return path


def _append_jsonl(handle: TextIO, value: Any) -> None:
    handle.write(json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()


def _finalize_stream(handle: Any, temporary: Path, final_path: Path) -> None:
    if not handle.closed:
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
    os.replace(temporary, final_path)
    final_path.chmod(0o644)


def _atomic_write_json(path: Path, value: Any) -> None:
    text = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o644)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_write_canonical_json(path: Path, value: Any) -> None:
    encoded = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        path.chmod(0o444)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _load_spec(path: Path) -> Mapping[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"_spec_load_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(raw, dict):
        return {"_spec_load_error": "spec root must be a JSON object"}
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--lifecycle-child" in raw_argv:
        return _lifecycle_child_main(raw_argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--formal-lifecycle", action="store_true")
    parser.add_argument("--restart-ordinal", type=int)
    args = parser.parse_args(raw_argv)
    try:
        result = run_smoke(
            _load_spec(args.spec),
            args.evidence_dir,
            enable_child_subreaper=True,
            formal_lifecycle=args.formal_lifecycle,
            restart_ordinal=args.restart_ordinal,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": _error_record(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
