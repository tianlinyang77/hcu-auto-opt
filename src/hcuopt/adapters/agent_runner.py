# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import hmac
import json
import math
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
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Protocol, runtime_checkable
from uuid import UUID

from hcuopt.contracts.agent_runner_v1 import (
    RunnerExecutionRecord,
    RunnerExecutionStatus,
    RunnerProvenance,
    runner_provenance_identity_hash,
)
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
WINDOWS_CREATE_SUSPENDED = 0x00000004
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
DEPLOYMENT_CREDENTIAL_NAME_PATTERN = re.compile(
    r"^HCUOPT_DEPLOYMENT_[A-Z][A-Z0-9_]{2,80}_FILE$"
)
MAX_DEPLOYMENT_CREDENTIAL_BYTES = 64 * 1024

AgentRunStatus = RunnerExecutionStatus


class AgentRunnerSafetyError(ValueError):
    """The dev-only Agent runner refused to expand its execution authority."""


@dataclass(frozen=True, slots=True)
class AgentDeploymentCredential:
    """Deployment-owned secret staged as a private file for one attempt.

    This object is intentionally absent from ``AgentRunRequest`` so a caller cannot
    choose, replace, or observe provider credentials through the durable request.
    """

    environment_name: str
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not DEPLOYMENT_CREDENTIAL_NAME_PATTERN.fullmatch(self.environment_name):
            raise AgentRunnerSafetyError(
                "deployment credential environment name must use the reserved file namespace"
            )
        if not isinstance(self.content, bytes) or not self.content:
            raise AgentRunnerSafetyError("deployment credential content must be non-empty bytes")
        if len(self.content) > MAX_DEPLOYMENT_CREDENTIAL_BYTES:
            raise AgentRunnerSafetyError("deployment credential exceeds its size limit")
        if b"\x00" in self.content:
            raise AgentRunnerSafetyError("deployment credential contains NUL")


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _require_int(name: str, value: object, *, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int:
        raise AgentRunnerSafetyError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        upper = f", {maximum}" if maximum is not None else ""
        raise AgentRunnerSafetyError(f"{name} must be within [{minimum}{upper}]")
    return value


def _require_finite_float(
    name: str, value: object, *, minimum_exclusive: float, maximum: float
) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise AgentRunnerSafetyError(f"{name} must be a finite float")
    if value <= minimum_exclusive or value > maximum:
        raise AgentRunnerSafetyError(f"{name} must be within ({minimum_exclusive}, {maximum}]")
    return value


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
        _require_int("attempt_number", self.attempt_number, minimum=1, maximum=64)
        _require_finite_float(
            "timeout_seconds",
            self.timeout_seconds,
            minimum_exclusive=0.0,
            maximum=7_200.0,
        )
        for name in (
            "max_stdout_bytes",
            "max_stderr_bytes",
            "max_total_output_bytes",
            "max_tokens",
        ):
            _require_int(name, getattr(self, name), minimum=1)
        _require_finite_float(
            "termination_grace_seconds",
            self.termination_grace_seconds,
            minimum_exclusive=0.0,
            maximum=30.0,
        )


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    attempt_id: UUID
    generation_run_id: UUID
    request_id: UUID
    request_hash: str
    plan_id: UUID
    generator_id: str
    executable: Path
    generator_artifact: Path
    generator_artifact_hash: str
    argv: tuple[str, ...]
    limits: AgentRunLimits
    environment: tuple[tuple[str, str], ...] = ()
    input_files: tuple[AgentInputFile, ...] = ()

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, UUID)
            for value in (
                self.attempt_id,
                self.generation_run_id,
                self.request_id,
                self.plan_id,
            )
        ):
            raise AgentRunnerSafetyError("Agent authority identifiers must be UUIDs")
        if not re.fullmatch(r"^[a-z0-9][a-z0-9._-]{2,99}$", self.generator_id):
            raise AgentRunnerSafetyError("Agent generator identity is invalid")
        if not isinstance(self.request_hash, str) or not SHA256_PATTERN.fullmatch(
            self.request_hash
        ):
            raise AgentRunnerSafetyError("Agent request hash must be canonical SHA256")
        if not isinstance(self.executable, Path) or not isinstance(self.generator_artifact, Path):
            raise AgentRunnerSafetyError("Agent executable and artifact must be Paths")
        if not self.executable.is_absolute():
            raise AgentRunnerSafetyError("Agent executable must be absolute")
        if not self.generator_artifact.is_absolute():
            raise AgentRunnerSafetyError("Agent generator artifact must be absolute")
        if not isinstance(self.generator_artifact_hash, str) or not SHA256_PATTERN.fullmatch(
            self.generator_artifact_hash
        ):
            raise AgentRunnerSafetyError("Agent generator artifact hash must be canonical SHA256")
        if not isinstance(self.argv, tuple) or not isinstance(self.limits, AgentRunLimits):
            raise AgentRunnerSafetyError("Agent argv and limits must use their declared types")
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
class AgentRunResult:
    proposal_bytes: bytes | None
    evidence: RunnerExecutionRecord

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


def _freeze_provenance(provenance: AdapterProvenance) -> RunnerProvenance:
    return RunnerProvenance(
        profile=provenance.profile,
        capability=provenance.capability,
        adapter_name=provenance.adapter_name,
        adapter_version=provenance.adapter_version,
        implementation_kind=provenance.implementation_kind,
        source_commit=provenance.source_commit,
        identity_hash=runner_provenance_identity_hash(
            profile=provenance.profile,
            capability=provenance.capability,
            adapter_name=provenance.adapter_name,
            adapter_version=provenance.adapter_version,
            implementation_kind=provenance.implementation_kind,
            source_commit=provenance.source_commit,
        ),
    )


def _evidence(
    *,
    request: AgentRunRequest,
    runner_provenance: AdapterProvenance,
    executable_hash: str | None,
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
    sensitive_values: Iterable[str] = (),
) -> RunnerExecutionRecord:
    _executable, argv_hash, _paths = _request_identity(request)
    return RunnerExecutionRecord(
        attempt_id=request.attempt_id,
        generation_run_id=request.generation_run_id,
        request_id=request.request_id,
        request_hash=request.request_hash,
        plan_id=request.plan_id,
        generator_id=request.generator_id,
        attempt_number=request.limits.attempt_number,
        runner_provenance=_freeze_provenance(runner_provenance),
        generator_artifact_hash=request.generator_artifact_hash,
        executable_hash=executable_hash,
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
            stdout,
            sensitive_values=(
                *(value for _name, value in request.environment),
                *sensitive_values,
            ),
        ),
        stderr_summary=_redacted_summary(
            stderr,
            sensitive_values=(
                *(value for _name, value in request.environment),
                *sensitive_values,
            ),
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
        if type(reported_tokens) is not int or reported_tokens < 0:
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
            runner_provenance=self.provenance,
            executable_hash=None,
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


class _WindowsJobDomain:
    """Own one suspended process tree in a kill-on-close Windows Job Object."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise AgentRunnerSafetyError("Windows Job Objects are only available on Windows")
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise AgentRunnerSafetyError("failed to create Windows Agent Job Object")
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            kernel32.CloseHandle(handle)
            raise AgentRunnerSafetyError("failed to configure Windows Agent Job Object")
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = kernel32
        self._handle: int | None = int(handle)

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32
        if self._handle is None:
            raise AgentRunnerSafetyError("Windows Agent Job Object is closed")
        process_handle = getattr(process, "_handle", None)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        if process_handle is None or not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle), wintypes.HANDLE(int(process_handle))
        ):
            raise AgentRunnerSafetyError("failed to assign Agent process to Windows Job Object")

        class _ThreadEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel32.ResumeThread.restype = wintypes.DWORD

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if not snapshot or int(snapshot) == invalid_handle:
            raise AgentRunnerSafetyError("failed to enumerate suspended Agent threads")
        resumed = 0
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while has_entry:
                if entry.th32OwnerProcessID == process.pid:
                    thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise AgentRunnerSafetyError("failed to open suspended Agent thread")
                    try:
                        if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise AgentRunnerSafetyError("failed to resume Agent process")
                        resumed += 1
                    finally:
                        kernel32.CloseHandle(thread)
                has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snapshot)
        if resumed != 1:
            raise AgentRunnerSafetyError(
                "Agent process did not expose exactly one suspended thread"
            )

    def _active_processes(self) -> int:
        ctypes = self._ctypes
        wintypes = self._wintypes
        kernel32 = self._kernel32
        if self._handle is None:
            raise AgentRunnerSafetyError("Windows Agent Job Object is closed")

        class _BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        information = _BasicAccountingInformation()
        if not kernel32.QueryInformationJobObject(
            wintypes.HANDLE(self._handle),
            1,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            raise AgentRunnerSafetyError("failed to query Windows Agent Job Object")
        return int(information.ActiveProcesses)

    def terminate_and_verify(
        self, grace_seconds: float
    ) -> Literal["terminated", "killed", "failed"]:
        try:
            if self._active_processes() == 0:
                return "terminated"
            kernel32 = self._kernel32
            wintypes = self._wintypes
            kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            if self._handle is None or not kernel32.TerminateJobObject(
                wintypes.HANDLE(self._handle), 1
            ):
                return "failed"
            deadline = time.monotonic() + max(1.0, grace_seconds)
            while time.monotonic() < deadline:
                if self._active_processes() == 0:
                    return "killed"
                time.sleep(0.01)
            return "failed"
        except (OSError, AgentRunnerSafetyError):
            return "failed"
        finally:
            self.close()

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._wintypes.HANDLE(self._handle))
            self._handle = None


class LocalCommandAgentRunner:
    """Execute one allowlisted local generator in an adapter-owned directory."""

    def __init__(
        self,
        *,
        allowed_executables: Iterable[Path],
        allowed_argv_prefixes: Iterable[tuple[str, ...]],
        allowed_environment_names: frozenset[str] = frozenset(),
        deployment_credentials: Iterable[AgentDeploymentCredential] = (),
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
        _require_int("max_input_bytes", max_input_bytes, minimum=1)
        _require_finite_float(
            "poll_interval_seconds",
            poll_interval_seconds,
            minimum_exclusive=0.0,
            maximum=1.0,
        )
        self.allowed_executables = frozenset(resolved)
        self.allowed_argv_prefixes = prefixes
        self.allowed_environment_names = frozenset(allowed_environment_names)
        self.deployment_credentials = tuple(deployment_credentials)
        credential_names = [
            credential.environment_name.casefold()
            for credential in self.deployment_credentials
        ]
        if len(credential_names) != len(set(credential_names)):
            raise AgentRunnerSafetyError("deployment credentials contain a duplicate name")
        self.reserved_environment_names = RESERVED_ENVIRONMENT_NAMES | frozenset(
            credential_names
        )
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
        executable_hash = self._preflight(request)
        output_dir.mkdir(parents=True, exist_ok=True)
        attempt_dir = Path(tempfile.mkdtemp(prefix="agent-run-", dir=output_dir))
        started = time.monotonic()
        result: AgentRunResult | None = None
        try:
            (
                input_root,
                work_dir,
                usage_path,
                credential_environment,
                sensitive_values,
            ) = self._prepare_attempt(attempt_dir, request)
            result = self._execute(
                request,
                input_root,
                work_dir,
                usage_path,
                started,
                executable_hash,
                credential_environment,
                sensitive_values,
            )
        finally:
            try:
                self.remove_tree(attempt_dir)
            except Exception as exc:
                if result is None:
                    raise AgentRunnerSafetyError(
                        "Agent attempt setup failed and temporary cleanup also failed"
                    ) from exc
                summary = _redacted_summary(str(exc).encode("utf-8", errors="replace"))
                evidence = RunnerExecutionRecord.model_validate(
                    {
                        **result.evidence.model_dump(mode="json"),
                        "status": "cleanup_failed",
                        "cleanup_status": "failed",
                        "cleanup_summary": summary,
                    }
                )
                result = AgentRunResult(proposal_bytes=None, evidence=evidence)
        if result is None:
            raise AgentRunnerSafetyError("Agent attempt ended without a result")
        return result

    def _preflight(self, request: AgentRunRequest) -> str:
        executable = request.executable.resolve()
        if executable not in self.allowed_executables:
            raise AgentRunnerSafetyError("Agent executable is not allowlisted")
        if not executable.is_file():
            raise AgentRunnerSafetyError("allowlisted Agent executable is not a file")
        artifact = request.generator_artifact.resolve()
        if not artifact.is_file():
            raise AgentRunnerSafetyError("Agent generator artifact is not a file")
        if artifact != executable and str(artifact) not in request.argv:
            raise AgentRunnerSafetyError(
                "Agent generator artifact must be the executable or an exact argv entry"
            )
        if not hmac.compare_digest(_sha256_file(artifact), request.generator_artifact_hash):
            raise AgentRunnerSafetyError("Agent generator artifact hash does not match content")
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
            if name.casefold() in self.reserved_environment_names:
                raise AgentRunnerSafetyError("Agent request contains a forbidden environment name")
            if name not in self.allowed_environment_names:
                raise AgentRunnerSafetyError("Agent environment name is not allowlisted")
            if FORBIDDEN_ENVIRONMENT_PATTERN.search(name):
                raise AgentRunnerSafetyError("Agent request contains a forbidden environment name")
            if "\x00" in value:
                raise AgentRunnerSafetyError("Agent environment value contains NUL")
        return _sha256_file(executable)

    def _prepare_attempt(
        self, attempt_dir: Path, request: AgentRunRequest
    ) -> tuple[Path, Path, Path, dict[str, str], tuple[str, ...]]:
        input_root = attempt_dir / "inputs"
        work_dir = attempt_dir / "work"
        input_root.mkdir()
        work_dir.mkdir()
        for item in request.input_files:
            path = input_root.joinpath(*PurePosixPath(item.path).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.content)
            path.chmod(stat.S_IREAD)
        credential_environment: dict[str, str] = {}
        sensitive_values: list[str] = []
        if self.deployment_credentials:
            credential_root = attempt_dir / "credentials"
            credential_root.mkdir()
            try:
                credential_root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                pass
            for ordinal, credential in enumerate(self.deployment_credentials):
                path = credential_root / f"credential-{ordinal:02d}"
                path.write_bytes(credential.content)
                try:
                    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
                credential_environment[credential.environment_name] = str(path)
                decoded = credential.content.decode("utf-8", errors="ignore")
                sensitive_values.extend((decoded, decoded.strip()))
        usage_path = work_dir / "usage.json"
        return (
            input_root,
            work_dir,
            usage_path,
            credential_environment,
            tuple(value for value in sensitive_values if value),
        )

    def _execute(
        self,
        request: AgentRunRequest,
        input_root: Path,
        work_dir: Path,
        usage_path: Path,
        started: float,
        executable_hash: str,
        credential_environment: dict[str, str],
        sensitive_values: tuple[str, ...],
    ) -> AgentRunResult:
        environment = self._environment(
            request,
            input_root,
            usage_path,
            credential_environment,
        )
        command = (str(request.executable.resolve()), *request.argv)
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": work_dir,
            "env": environment,
            "shell": False,
        }
        windows_job: _WindowsJobDomain | None = None
        if os.name == "nt":
            windows_job = _WindowsJobDomain()
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | WINDOWS_CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
            if windows_job is not None:
                windows_job.assign_and_resume(process)
        except Exception:
            if windows_job is not None:
                if "process" in locals():
                    windows_job.terminate_and_verify(request.limits.termination_grace_seconds)
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=max(1.0, request.limits.termination_grace_seconds))
                else:
                    windows_job.close()
            raise
        if process.stdout is None or process.stderr is None:
            self._terminate_tree(
                process,
                request.limits.termination_grace_seconds,
                windows_job=windows_job,
            )
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
                break
            if time.monotonic() >= deadline:
                termination_reason = "timeout"
                break
            time.sleep(self.poll_interval_seconds)

        process_tree_cleanup = self._terminate_tree(
            process,
            request.limits.termination_grace_seconds,
            windows_job=windows_job,
        )

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
            runner_provenance=self.provenance,
            executable_hash=executable_hash,
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
            sensitive_values=sensitive_values,
        )
        return AgentRunResult(
            proposal_bytes=stdout_bytes if status == "succeeded" else None,
            evidence=evidence,
        )

    @staticmethod
    def _environment(
        request: AgentRunRequest,
        input_root: Path,
        usage_path: Path,
        credential_environment: dict[str, str],
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
        environment.update(credential_environment)
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
        process: subprocess.Popen[bytes],
        grace_seconds: float,
        *,
        windows_job: _WindowsJobDomain | None = None,
    ) -> Literal["terminated", "killed", "failed"]:
        if os.name == "nt":
            if windows_job is None:
                return "failed"
            return windows_job.terminate_and_verify(grace_seconds)

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return "terminated"
        except PermissionError:
            return "failed"

        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            process.poll()
            if not LocalCommandAgentRunner._posix_group_exists(process.pid):
                return "terminated"
            time.sleep(0.01)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return "terminated"
        except PermissionError:
            return "failed"
        deadline = time.monotonic() + max(1.0, grace_seconds)
        while time.monotonic() < deadline:
            process.poll()
            if not LocalCommandAgentRunner._posix_group_exists(process.pid):
                return "killed"
            time.sleep(0.01)
        return "failed"

    @staticmethod
    def _posix_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
