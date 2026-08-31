# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Protocol, runtime_checkable
from uuid import UUID

from hcuopt.contracts.platform_v1 import AdapterProvenance

SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FORBIDDEN_ENVIRONMENT_PATTERN = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE_KEY|ACCESS_KEY|"
    r"HIP_VISIBLE_DEVICES|ROCR_VISIBLE_DEVICES|CUDA_VISIBLE_DEVICES|"
    r"HOLDOUT|SSH_AUTH_SOCK|DOCKER_HOST|DOCKER_CONTEXT)(?:_|$)",
    re.IGNORECASE,
)
SENSITIVE_OUTPUT_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|private[_-]?key|access[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
USAGE_SCHEMA_VERSION = "hcuopt-agent-usage-v1"
SUMMARY_BYTES = 1_024
RESERVED_ENVIRONMENT_NAMES = frozenset(
    name.casefold()
    for name in (
        "HCUOPT_INPUT_ROOT",
        "HCUOPT_USAGE_PATH",
        "HCUOPT_REQUEST_HASH",
        "SystemRoot",
        "WINDIR",
    )
)

AgentRunStatus = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "output_limit_exceeded",
    "token_limit_exceeded",
    "invalid_output",
    "cleanup_failed",
]


class AgentRunnerSafetyError(ValueError):
    """The dev-only Agent runner refused to expand its execution authority."""


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_input_path(value: str) -> str:
    parts = value.split("/")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise AgentRunnerSafetyError("Agent input path must be a normalized relative path")
    return value


def _redacted_summary(value: bytes, *, sensitive_values: Iterable[str] = ()) -> str:
    decoded = value[:SUMMARY_BYTES].decode("utf-8", errors="replace")
    redacted = SENSITIVE_OUTPUT_PATTERN.sub(r"\1\2<redacted>", decoded)
    for sensitive in sorted((item for item in sensitive_values if item), key=len, reverse=True):
        redacted = redacted.replace(sensitive, "<redacted-env>")
    return redacted


def _remove_attempt_tree(path: Path) -> None:
    def make_writable_and_retry(
        function: Callable[[str], object], blocked_path: str, _error: object
    ) -> None:
        os.chmod(blocked_path, stat.S_IWRITE)
        function(blocked_path)

    shutil.rmtree(path, onerror=make_writable_and_retry)


@dataclass(frozen=True, slots=True)
class AgentInputFile:
    path: str
    content: bytes

    def __post_init__(self) -> None:
        _safe_input_path(self.path)
        if not isinstance(self.content, bytes):
            raise AgentRunnerSafetyError("Agent input content must be bytes")


@dataclass(frozen=True, slots=True)
class AgentRunLimits:
    attempt_number: int
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_total_output_bytes: int
    max_tokens: int
    termination_grace_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.attempt_number < 1 or self.attempt_number > 64:
            raise AgentRunnerSafetyError("Agent attempt number is out of range")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 7_200:
            raise AgentRunnerSafetyError("Agent timeout must be within (0, 7200]")
        for name in (
            "max_stdout_bytes",
            "max_stderr_bytes",
            "max_total_output_bytes",
            "max_tokens",
        ):
            if getattr(self, name) < 1:
                raise AgentRunnerSafetyError(f"{name} must be positive")
        if self.termination_grace_seconds <= 0 or self.termination_grace_seconds > 30:
            raise AgentRunnerSafetyError("Agent termination grace must be within (0, 30]")


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    attempt_id: UUID
    generation_run_id: UUID
    request_hash: str
    executable: Path
    argv: tuple[str, ...]
    limits: AgentRunLimits
    environment: tuple[tuple[str, str], ...] = ()
    input_files: tuple[AgentInputFile, ...] = ()

    def __post_init__(self) -> None:
        if not SHA256_PATTERN.fullmatch(self.request_hash):
            raise AgentRunnerSafetyError("Agent request hash must be canonical SHA256")
        if not self.executable.is_absolute():
            raise AgentRunnerSafetyError("Agent executable must be absolute")
        if not self.argv or len(self.argv) > 128:
            raise AgentRunnerSafetyError("Agent argv must contain between 1 and 128 entries")
        if any(
            not isinstance(item, str) or not item or "\x00" in item or len(item) > 8_192
            for item in self.argv
        ):
            raise AgentRunnerSafetyError("Agent argv contains an invalid entry")
        if any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in self.environment
        ):
            raise AgentRunnerSafetyError("Agent environment names and values must be strings")
        environment_names = [name.casefold() for name, _value in self.environment]
        if len(environment_names) != len(set(environment_names)):
            raise AgentRunnerSafetyError("Agent environment contains a duplicate name")
        input_paths = [item.path for item in self.input_files]
        if len(input_paths) != len(set(input_paths)):
            raise AgentRunnerSafetyError("Agent input contains a duplicate path")


@dataclass(frozen=True, slots=True)
class AgentRunEvidence:
    status: AgentRunStatus
    synthetic: bool
    attempts_consumed: int
    wall_seconds_consumed: float
    stdout_bytes_consumed: int
    stderr_bytes_consumed: int
    total_output_bytes_consumed: int
    tokens_consumed: int | None
    exit_code: int | None
    executable_name: str
    argv_hash: str
    input_manifest_hash: str
    stdout_hash: str
    stderr_hash: str
    stdout_summary: str
    stderr_summary: str
    environment_names: tuple[str, ...]
    termination_reason: str | None
    process_tree_cleanup: Literal["not_needed", "terminated", "killed", "failed"]
    cleanup_status: Literal["verified", "failed"]
    cleanup_summary: str
    performance_conclusion: Literal["not_measured"] = "not_measured"
    hcu_access_allowed: Literal[False] = False
    holdout_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    proposal_bytes: bytes | None
    evidence: AgentRunEvidence

    @property
    def status(self) -> AgentRunStatus:
        return self.evidence.status


@runtime_checkable
class AgentRunnerAdapter(Protocol):
    """Run one bounded proposal-generation attempt without Candidate authority."""

    provenance: AdapterProvenance

    def run(self, request: AgentRunRequest, output_dir: Path) -> AgentRunResult: ...


def _request_identity(request: AgentRunRequest) -> tuple[str, str, tuple[str, ...]]:
    executable = str(request.executable.resolve())
    argv_hash = _sha256_bytes(_canonical_json_bytes([executable, *request.argv]))
    manifest = [
        {"path": item.path, "content_hash": _sha256_bytes(item.content)}
        for item in sorted(request.input_files, key=lambda item: item.path)
    ]
    return executable, argv_hash, tuple(item["path"] for item in manifest)


def _input_manifest_hash(request: AgentRunRequest) -> str:
    manifest = [
        {"path": item.path, "content_hash": _sha256_bytes(item.content)}
        for item in sorted(request.input_files, key=lambda item: item.path)
    ]
    return _sha256_bytes(_canonical_json_bytes(manifest))


def _evidence(
    *,
    request: AgentRunRequest,
    status: AgentRunStatus,
    synthetic: bool,
    wall_seconds: float,
    stdout: bytes,
    stderr: bytes,
    stdout_bytes: int,
    stderr_bytes: int,
    tokens: int | None,
    exit_code: int | None,
    termination_reason: str | None,
    process_tree_cleanup: Literal["not_needed", "terminated", "killed", "failed"],
    cleanup_status: Literal["verified", "failed"] = "verified",
    cleanup_summary: str = "adapter-owned attempt directory removed",
) -> AgentRunEvidence:
    _executable, argv_hash, _paths = _request_identity(request)
    return AgentRunEvidence(
        status=status,
        synthetic=synthetic,
        attempts_consumed=1,
        wall_seconds_consumed=max(0.0, wall_seconds),
        stdout_bytes_consumed=stdout_bytes,
        stderr_bytes_consumed=stderr_bytes,
        total_output_bytes_consumed=stdout_bytes + stderr_bytes,
        tokens_consumed=tokens,
        exit_code=exit_code,
        executable_name=request.executable.name,
        argv_hash=argv_hash,
        input_manifest_hash=_input_manifest_hash(request),
        stdout_hash=_sha256_bytes(stdout),
        stderr_hash=_sha256_bytes(stderr),
        stdout_summary=_redacted_summary(
            stdout, sensitive_values=(value for _name, value in request.environment)
        ),
        stderr_summary=_redacted_summary(
            stderr, sensitive_values=(value for _name, value in request.environment)
        ),
        environment_names=tuple(sorted(name for name, _value in request.environment)),
        termination_reason=termination_reason,
        process_tree_cleanup=process_tree_cleanup,
        cleanup_status=cleanup_status,
        cleanup_summary=cleanup_summary,
    )


class DeterministicAgentRunner:
    """Return fixed synthetic proposal bytes for scheduler and CI tests."""

    def __init__(self, *, proposal_bytes: bytes, reported_tokens: int) -> None:
        if reported_tokens < 0:
            raise AgentRunnerSafetyError("reported token consumption cannot be negative")
        self.proposal_bytes = bytes(proposal_bytes)
        self.reported_tokens = reported_tokens
        self.provenance = AdapterProvenance(
            profile="m2b-agent-runner-deterministic-v1",
            capability="agent_runner",
            adapter_name="DeterministicAgentRunner",
            adapter_version="1.0.0",
            implementation_kind="fake",
        )

    def run(self, request: AgentRunRequest, output_dir: Path) -> AgentRunResult:
        del output_dir
        status: AgentRunStatus = "succeeded"
        reason = None
        stdout_bytes = len(self.proposal_bytes)
        if (
            stdout_bytes > request.limits.max_stdout_bytes
            or stdout_bytes > request.limits.max_total_output_bytes
        ):
            status = "output_limit_exceeded"
            reason = "output_limit_exceeded"
        elif self.reported_tokens > request.limits.max_tokens:
            status = "token_limit_exceeded"
            reason = "token_limit_exceeded"
        evidence = _evidence(
            request=request,
            status=status,
            synthetic=True,
            wall_seconds=0.0,
            stdout=self.proposal_bytes,
            stderr=b"",
            stdout_bytes=stdout_bytes,
            stderr_bytes=0,
            tokens=self.reported_tokens,
            exit_code=0,
            termination_reason=reason,
            process_tree_cleanup="not_needed",
        )
        return AgentRunResult(
            proposal_bytes=self.proposal_bytes if status == "succeeded" else None,
            evidence=evidence,
        )


@dataclass(slots=True)
class _OutputState:
    lock: threading.Lock
    overflow: threading.Event
    total_bytes: int = 0
    overflow_reason: str | None = None


@dataclass(slots=True)
class _CapturedStream:
    content: bytearray
    bytes_read: int = 0


def _capture_stream(
    stream: BinaryIO,
    captured: _CapturedStream,
    *,
    stream_name: str,
    stream_limit: int,
    total_limit: int,
    state: _OutputState,
) -> None:
    try:
        while True:
            chunk = stream.read(4_096)
            if not chunk:
                return
            with state.lock:
                captured.bytes_read += len(chunk)
                state.total_bytes += len(chunk)
                remaining = max(0, stream_limit - len(captured.content))
                captured.content.extend(chunk[:remaining])
                if captured.bytes_read > stream_limit and state.overflow_reason is None:
                    state.overflow_reason = f"{stream_name}_limit_exceeded"
                    state.overflow.set()
                if state.total_bytes > total_limit and state.overflow_reason is None:
                    state.overflow_reason = "total_output_limit_exceeded"
                    state.overflow.set()
    finally:
        stream.close()


class LocalCommandAgentRunner:
    """Execute one allowlisted local generator in an adapter-owned directory."""

    def __init__(
        self,
        *,
        allowed_executables: Iterable[Path],
        allowed_argv_prefixes: Iterable[tuple[str, ...]],
        allowed_environment_names: frozenset[str] = frozenset(),
        forbidden_host_paths: tuple[Path, ...] | None = None,
        path_exists: Callable[[Path], bool] = Path.exists,
        remove_tree: Callable[[Path], None] = _remove_attempt_tree,
        max_input_bytes: int = 10_000_000,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        resolved = tuple(Path(item).resolve() for item in allowed_executables)
        prefixes = tuple(tuple(item) for item in allowed_argv_prefixes)
        if not resolved:
            raise AgentRunnerSafetyError("at least one Agent executable must be allowlisted")
        if not prefixes or any(
            not prefix
            or any(not isinstance(item, str) or not item or "\x00" in item for item in prefix)
            for prefix in prefixes
        ):
            raise AgentRunnerSafetyError("at least one valid Agent argv prefix must be allowlisted")
        if max_input_bytes < 1:
            raise AgentRunnerSafetyError("Agent input byte limit must be positive")
        if poll_interval_seconds <= 0 or poll_interval_seconds > 1:
            raise AgentRunnerSafetyError("Agent poll interval must be within (0, 1]")
        self.allowed_executables = frozenset(resolved)
        self.allowed_argv_prefixes = prefixes
        self.allowed_environment_names = frozenset(allowed_environment_names)
        self.forbidden_host_paths = (
            forbidden_host_paths
            if forbidden_host_paths is not None
            else ((Path("/dev/kfd"),) if os.name != "nt" else ())
        )
        self.path_exists = path_exists
        self.remove_tree = remove_tree
        self.max_input_bytes = max_input_bytes
        self.poll_interval_seconds = poll_interval_seconds
        self.provenance = AdapterProvenance(
            profile="m2b-agent-runner-local-command-v1",
            capability="agent_runner",
            adapter_name="LocalCommandAgentRunner",
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def run(self, request: AgentRunRequest, output_dir: Path) -> AgentRunResult:
        self._preflight(request)
        output_dir.mkdir(parents=True, exist_ok=True)
        attempt_dir = Path(tempfile.mkdtemp(prefix="agent-run-", dir=output_dir))
        started = time.monotonic()
        result: AgentRunResult | None = None
        try:
            input_root, work_dir, usage_path = self._prepare_attempt(attempt_dir, request)
            result = self._execute(request, input_root, work_dir, usage_path, started)
        finally:
            try:
                self.remove_tree(attempt_dir)
            except Exception as exc:
                if result is None:
                    raise AgentRunnerSafetyError(
                        "Agent attempt setup failed and temporary cleanup also failed"
                    ) from exc
                summary = _redacted_summary(str(exc).encode("utf-8", errors="replace"))
                evidence = replace(
                    result.evidence,
                    status="cleanup_failed",
                    cleanup_status="failed",
                    cleanup_summary=summary,
                )
                result = AgentRunResult(proposal_bytes=None, evidence=evidence)
        if result is None:
            raise AgentRunnerSafetyError("Agent attempt ended without a result")
        return result

    def _preflight(self, request: AgentRunRequest) -> None:
        executable = request.executable.resolve()
        if executable not in self.allowed_executables:
            raise AgentRunnerSafetyError("Agent executable is not allowlisted")
        if not executable.is_file():
            raise AgentRunnerSafetyError("allowlisted Agent executable is not a file")
        if not any(request.argv[: len(prefix)] == prefix for prefix in self.allowed_argv_prefixes):
            raise AgentRunnerSafetyError("Agent argv prefix is not allowlisted")
        if sum(len(item.content) for item in request.input_files) > self.max_input_bytes:
            raise AgentRunnerSafetyError("Agent input byte limit is exceeded")
        for path in self.forbidden_host_paths:
            if self.path_exists(path):
                raise AgentRunnerSafetyError(f"forbidden host path is visible: {path.name}")
        for name, value in request.environment:
            if not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
                raise AgentRunnerSafetyError("Agent environment name is invalid")
            if name.casefold() in RESERVED_ENVIRONMENT_NAMES:
                raise AgentRunnerSafetyError("Agent request contains a forbidden environment name")
            if name not in self.allowed_environment_names:
                raise AgentRunnerSafetyError("Agent environment name is not allowlisted")
            if FORBIDDEN_ENVIRONMENT_PATTERN.search(name):
                raise AgentRunnerSafetyError("Agent request contains a forbidden environment name")
            if "\x00" in value:
                raise AgentRunnerSafetyError("Agent environment value contains NUL")

    @staticmethod
    def _prepare_attempt(attempt_dir: Path, request: AgentRunRequest) -> tuple[Path, Path, Path]:
        input_root = attempt_dir / "inputs"
        work_dir = attempt_dir / "work"
        input_root.mkdir()
        work_dir.mkdir()
        for item in request.input_files:
            path = input_root.joinpath(*PurePosixPath(item.path).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.content)
            path.chmod(stat.S_IREAD)
        usage_path = work_dir / "usage.json"
        return input_root, work_dir, usage_path

    def _execute(
        self,
        request: AgentRunRequest,
        input_root: Path,
        work_dir: Path,
        usage_path: Path,
        started: float,
    ) -> AgentRunResult:
        environment = self._environment(request, input_root, usage_path)
        command = (str(request.executable.resolve()), *request.argv)
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": work_dir,
            "env": environment,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
        if process.stdout is None or process.stderr is None:
            raise AgentRunnerSafetyError("Agent runner failed to capture process output")

        state = _OutputState(lock=threading.Lock(), overflow=threading.Event())
        stdout = _CapturedStream(bytearray())
        stderr = _CapturedStream(bytearray())
        stdout_thread = threading.Thread(
            target=_capture_stream,
            args=(process.stdout, stdout),
            kwargs={
                "stream_name": "stdout",
                "stream_limit": request.limits.max_stdout_bytes,
                "total_limit": request.limits.max_total_output_bytes,
                "state": state,
            },
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_capture_stream,
            args=(process.stderr, stderr),
            kwargs={
                "stream_name": "stderr",
                "stream_limit": request.limits.max_stderr_bytes,
                "total_limit": request.limits.max_total_output_bytes,
                "state": state,
            },
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        deadline = started + request.limits.timeout_seconds
        termination_reason: str | None = None
        process_tree_cleanup: Literal["not_needed", "terminated", "killed", "failed"] = "not_needed"
        while process.poll() is None:
            if state.overflow.is_set():
                termination_reason = "output_limit_exceeded"
                process_tree_cleanup = self._terminate_tree(
                    process, request.limits.termination_grace_seconds
                )
                break
            if time.monotonic() >= deadline:
                termination_reason = "timeout"
                process_tree_cleanup = self._terminate_tree(
                    process, request.limits.termination_grace_seconds
                )
                break
            time.sleep(self.poll_interval_seconds)

        try:
            exit_code = process.wait(timeout=request.limits.termination_grace_seconds + 1)
        except subprocess.TimeoutExpired:
            exit_code = None
            process_tree_cleanup = "failed"
        stdout_thread.join(timeout=request.limits.termination_grace_seconds + 1)
        stderr_thread.join(timeout=request.limits.termination_grace_seconds + 1)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            process_tree_cleanup = "failed"

        stdout_bytes = bytes(stdout.content)
        stderr_bytes = bytes(stderr.content)
        tokens, usage_valid = self._read_usage(usage_path)
        status: AgentRunStatus
        if process_tree_cleanup == "failed":
            status = "cleanup_failed"
        elif termination_reason == "timeout":
            status = "timed_out"
        elif termination_reason == "output_limit_exceeded" or state.overflow_reason:
            status = "output_limit_exceeded"
            termination_reason = "output_limit_exceeded"
        elif exit_code != 0:
            status = "failed"
        elif not usage_valid:
            status = "invalid_output"
        elif tokens is not None and tokens > request.limits.max_tokens:
            status = "token_limit_exceeded"
            termination_reason = "token_limit_exceeded"
        else:
            status = "succeeded"

        evidence = _evidence(
            request=request,
            status=status,
            synthetic=False,
            wall_seconds=time.monotonic() - started,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            stdout_bytes=stdout.bytes_read,
            stderr_bytes=stderr.bytes_read,
            tokens=tokens,
            exit_code=exit_code,
            termination_reason=termination_reason,
            process_tree_cleanup=process_tree_cleanup,
            cleanup_status="verified" if process_tree_cleanup != "failed" else "failed",
            cleanup_summary=(
                "adapter-owned process tree and attempt directory removed"
                if process_tree_cleanup != "failed"
                else "adapter-owned process tree cleanup could not be verified"
            ),
        )
        return AgentRunResult(
            proposal_bytes=stdout_bytes if status == "succeeded" else None,
            evidence=evidence,
        )

    @staticmethod
    def _environment(
        request: AgentRunRequest, input_root: Path, usage_path: Path
    ) -> dict[str, str]:
        environment: dict[str, str] = {
            "HCUOPT_INPUT_ROOT": str(input_root),
            "HCUOPT_USAGE_PATH": str(usage_path),
            "HCUOPT_REQUEST_HASH": request.request_hash,
        }
        if os.name == "nt":
            for name in ("SystemRoot", "WINDIR"):
                if name in os.environ:
                    environment[name] = os.environ[name]
        environment.update(request.environment)
        return environment

    @staticmethod
    def _read_usage(path: Path) -> tuple[int | None, bool]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None, False
        if not isinstance(value, dict) or set(value) != {"schema_version", "total_tokens"}:
            return None, False
        tokens = value.get("total_tokens")
        if value.get("schema_version") != USAGE_SCHEMA_VERSION:
            return None, False
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            return None, False
        return tokens, True

    @staticmethod
    def _terminate_tree(
        process: subprocess.Popen[bytes], grace_seconds: float
    ) -> Literal["terminated", "killed", "failed"]:
        if os.name == "nt":
            completed = subprocess.run(
                ("taskkill.exe", "/PID", str(process.pid), "/T", "/F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, grace_seconds),
                check=False,
                shell=False,
            )
            try:
                process.wait(timeout=max(1.0, grace_seconds))
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=max(1.0, grace_seconds))
                except subprocess.TimeoutExpired:
                    return "failed"
                return "killed"
            return "killed" if completed.returncode == 0 else "terminated"

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return "terminated"
        try:
            process.wait(timeout=grace_seconds)
            return "terminated"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return "terminated"
            try:
                process.wait(timeout=max(1.0, grace_seconds))
            except subprocess.TimeoutExpired:
                return "failed"
            return "killed"
