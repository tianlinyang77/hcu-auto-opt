from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol
from uuid import UUID, uuid4

from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ExecutionRequest,
    ExecutionResult,
    TargetSpec,
)
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import (
    ExecutionSafetyError,
    ImageIdentityError,
    StaleFencingToken,
)

MANAGED_LABEL = "io.hcuopt.managed"
REQUEST_LABEL = "io.hcuopt.request-id"
RESOURCE_LABEL = "io.hcuopt.resource-id"
FENCING_LABEL = "io.hcuopt.fencing-token"
SAFE_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_ENVIRONMENT_NAME = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE_KEY|ACCESS_KEY)(?:_|$)",
    re.IGNORECASE,
)
PHYSICAL_HCU_VISIBILITY_VARIABLE = "ROCR_VISIBLE_DEVICES"
LOGICAL_HCU_VISIBILITY_VARIABLE = "HIP_VISIBLE_DEVICES"


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class RunningCommand(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], timeout: float = 30.0) -> CommandResult: ...

    def popen(
        self,
        argv: Sequence[str],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> RunningCommand: ...


class LocalCommandRunner:
    """Run a structured argv locally without a shell."""

    def run(self, argv: Sequence[str], timeout: float = 30.0) -> CommandResult:
        completed = subprocess.run(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        return CommandResult(
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def popen(
        self,
        argv: Sequence[str],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            shell=False,
            start_new_session=True,
        )


class OpenSSHCommandRunner:
    """Use OpenSSH with host-key checking and a safely quoted remote argv."""

    def __init__(
        self,
        host: str,
        *,
        user: str,
        port: int = 22,
        identity_file: Path | None = None,
        connect_timeout_seconds: int = 10,
        ssh_binary: str = "ssh",
        local_runner: CommandRunner | None = None,
    ) -> None:
        if not host or not user:
            raise ValueError("SSH host and user are required")
        if port < 1 or port > 65_535:
            raise ValueError("SSH port is out of range")
        self.host = host
        self.user = user
        self.port = port
        self.identity_file = identity_file
        self.connect_timeout_seconds = connect_timeout_seconds
        self.ssh_binary = ssh_binary
        self.local_runner = local_runner or LocalCommandRunner()

    def wrapped_argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("remote argv must contain non-empty strings")
        options = [
            self.ssh_binary,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout_seconds}",
            "-p",
            str(self.port),
        ]
        if self.identity_file is not None:
            options.extend(("-i", str(self.identity_file)))
        return (*options, "--", f"{self.user}@{self.host}", shlex.join(tuple(argv)))

    def run(self, argv: Sequence[str], timeout: float = 30.0) -> CommandResult:
        wrapped = self.wrapped_argv(argv)
        completed = self.local_runner.run(wrapped, timeout)
        return CommandResult(
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def popen(
        self,
        argv: Sequence[str],
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> RunningCommand:
        return self.local_runner.popen(self.wrapped_argv(argv), stdout=stdout, stderr=stderr)


class FencingGuard:
    """Process-local high-water mark; the control plane remains the authoritative guard."""

    def __init__(self) -> None:
        self._tokens: dict[str, int] = {}
        self._lock = threading.Lock()

    def before_execute(self, resource_id: str, fencing_token: int) -> None:
        with self._lock:
            current = self._tokens.get(resource_id, 0)
            if fencing_token < current:
                raise StaleFencingToken(
                    f"resource {resource_id} token {fencing_token} is older than {current}"
                )
            self._tokens[resource_id] = fencing_token

    def before_result(self, resource_id: str, fencing_token: int) -> None:
        with self._lock:
            current = self._tokens.get(resource_id)
            if current != fencing_token:
                raise StaleFencingToken(
                    f"resource {resource_id} token {fencing_token} is no longer current"
                )


@dataclass(slots=True)
class _OwnedExecution:
    container_name: str
    resource_id: str
    fencing_token: int
    cancelled: threading.Event


class ContainerExecutionAdapter:
    """Execute one request in one digest-pinned, adapter-owned Docker container."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        profile: str = "nmz36-framework-smoke-v1",
        adapter_name: str = "ContainerExecutionAdapter",
        fencing_guard: FencingGuard | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        self.runner = runner or LocalCommandRunner()
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="executor",
            adapter_name=adapter_name,
            adapter_version="1.0.0",
            implementation_kind="real",
        )
        self.fencing_guard = fencing_guard or FencingGuard()
        self.poll_interval_seconds = poll_interval_seconds
        self._owned: dict[UUID, _OwnedExecution] = {}
        self._owned_lock = threading.Lock()

    def execute(
        self,
        request: ExecutionRequest,
        target: TargetSpec,
        output_dir: Path,
    ) -> ExecutionResult:
        self._validate_request(request, target)
        resource_id, fencing_token = self._lease_identity(request, target)
        if request.lease_scope is not LeaseScope.NONE:
            self.fencing_guard.before_execute(resource_id, fencing_token)

        image = self._inspect_locked_image(target)
        attempt_dir = self._attempt_directory(output_dir, request.request_id)
        stdout_path = attempt_dir / "stdout.log"
        stderr_path = attempt_dir / "stderr.log"
        container_name = f"hcuopt-{request.request_id.hex}"
        owned = _OwnedExecution(
            container_name=container_name,
            resource_id=resource_id,
            fencing_token=fencing_token,
            cancelled=threading.Event(),
        )
        with self._owned_lock:
            if request.request_id in self._owned:
                raise ExecutionSafetyError(
                    f"request {request.request_id} already has an active execution"
                )
            self._owned[request.request_id] = owned

        command = self._docker_command(
            request,
            target,
            container_name=container_name,
            resource_id=resource_id,
            fencing_token=fencing_token,
        )
        started_at = datetime.now(timezone.utc)
        status = "failed"
        exit_code: int | None = None
        termination: dict[str, object] = {
            "requested": False,
            "reason": None,
            "container_removed": False,
        }
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = self.runner.popen(command, stdout=stdout, stderr=stderr)
                deadline = time.monotonic() + request.timeout_seconds
                while process.poll() is None:
                    if owned.cancelled.is_set():
                        status = "cancelled"
                        termination = self._terminate_owned_container(owned, "cancelled")
                        break
                    if time.monotonic() >= deadline:
                        status = "timed_out"
                        termination = self._terminate_owned_container(owned, "timeout")
                        break
                    time.sleep(self.poll_interval_seconds)
                try:
                    exit_code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    exit_code = process.wait(timeout=5)
                    termination["client_process_killed"] = True
                if owned.cancelled.is_set() and status != "timed_out":
                    status = "cancelled"
                    termination["requested"] = True
                    termination["reason"] = "cancelled"
                elif status not in {"cancelled", "timed_out"}:
                    status = "succeeded" if exit_code == 0 else "failed"
        finally:
            finished_at = datetime.now(timezone.utc)
            with self._owned_lock:
                self._owned.pop(request.request_id, None)

        if request.lease_scope is not LeaseScope.NONE:
            self.fencing_guard.before_result(resource_id, fencing_token)

        stdout_hash, stdout_size = _hash_file(stdout_path)
        stderr_hash, stderr_size = _hash_file(stderr_path)
        metadata = {
            "target_id": target.target_id,
            "host": target.execution_host.name,
            "container_name": container_name,
            "container_id": None,
            "container_image": request.container_image,
            "image_id": image["Id"],
            "registry_digest_verified": True,
            "command_sha256": _hash_json(command),
            "stdout_sha256": stdout_hash,
            "stderr_sha256": stderr_hash,
            "stdout_bytes": stdout_size,
            "stderr_bytes": stderr_size,
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "device_index": target.execution_host.accelerator.device_index,
            "numa_node": target.execution_host.accelerator.numa_node,
            "cpu_affinity": target.execution_host.accelerator.cpu_affinity,
            "environment_keys": sorted(self._container_environment(request, target)),
            "termination": termination,
            "fencing_validated_before_execute": request.lease_scope is not LeaseScope.NONE,
            "fencing_validated_before_result": request.lease_scope is not LeaseScope.NONE,
        }
        return ExecutionResult(
            request_id=request.request_id,
            status=status,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            stdout_uri=stdout_path.resolve().as_uri(),
            stderr_uri=stderr_path.resolve().as_uri(),
            metadata=metadata,
            adapter_provenance=self.provenance,
            synthetic=False,
        )

    def cancel(self, request_id: UUID) -> Mapping[str, object]:
        with self._owned_lock:
            owned = self._owned.get(request_id)
        if owned is None:
            return {
                "request_id": str(request_id),
                "cancel_requested": True,
                "owned_execution_found": False,
            }
        owned.cancelled.set()
        termination = self._terminate_owned_container(owned, "cancelled")
        return {
            "request_id": str(request_id),
            "cancel_requested": True,
            "owned_execution_found": True,
            **termination,
        }

    def _validate_request(self, request: ExecutionRequest, target: TargetSpec) -> None:
        if request.target_id != target.target_id:
            raise ExecutionSafetyError(
                f"execution target mismatch: {request.target_id} != {target.target_id}"
            )
        if request.container_image != target.inference_image.immutable_reference:
            raise ImageIdentityError("execution must use the Target Lock immutable image")
        if "@sha256:" not in request.container_image:
            raise ImageIdentityError("container image must be referenced by registry digest")
        prohibited = PurePosixPath(target.execution_host.prohibited_work_root)
        for mount in request.mounts:
            source = PurePosixPath(mount.source)
            if source == prohibited or source.is_relative_to(prohibited):
                raise ExecutionSafetyError(
                    f"mount source uses prohibited work root: {mount.source}"
                )
            if "," in mount.source or "," in mount.target:
                raise ExecutionSafetyError("mount paths containing commas are unsupported")
        self._container_environment(request, target)

    @staticmethod
    def _lease_identity(request: ExecutionRequest, target: TargetSpec) -> tuple[str, int]:
        if request.lease_scope is LeaseScope.NONE:
            return f"unleased-{target.execution_host.name}", 0
        assert request.resource_id is not None
        assert request.fencing_token is not None
        return request.resource_id, request.fencing_token

    def _inspect_locked_image(self, target: TargetSpec) -> dict[str, object]:
        reference = target.inference_image.immutable_reference
        inspected = self.runner.run(("docker", "image", "inspect", reference), timeout=30)
        if inspected.returncode != 0:
            raise ImageIdentityError(
                "locked image is unavailable: " + _bounded_text(inspected.stderr)
            )
        try:
            values = json.loads(inspected.stdout)
            image = values[0]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
            raise ImageIdentityError("docker returned invalid image inspection data") from exc
        if not isinstance(image, dict) or image.get("Id") != target.inference_image.image_id:
            raise ImageIdentityError("locked image ID does not match Target Lock")
        repo_digests = image.get("RepoDigests") or []
        if reference not in repo_digests:
            raise ImageIdentityError("locked registry digest is absent from image RepoDigests")
        return image

    def _docker_command(
        self,
        request: ExecutionRequest,
        target: TargetSpec,
        *,
        container_name: str,
        resource_id: str,
        fencing_token: int,
    ) -> tuple[str, ...]:
        accelerator = target.execution_host.accelerator
        command = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--init",
            "--name",
            container_name,
            "--label",
            f"{MANAGED_LABEL}=true",
            "--label",
            f"{REQUEST_LABEL}={request.request_id}",
            "--label",
            f"{RESOURCE_LABEL}={resource_id}",
            "--label",
            f"{FENCING_LABEL}={fencing_token}",
            "--security-opt",
            "no-new-privileges",
            "--workdir",
            request.working_directory,
        ]
        if request.lease_scope is not LeaseScope.NONE:
            command.extend(
                (
                    "--cpuset-cpus",
                    accelerator.cpu_affinity,
                    "--cpuset-mems",
                    str(accelerator.numa_node),
                    "--device",
                    "/dev/kfd",
                    "--device",
                    "/dev/dri",
                )
            )
        for key, value in sorted(self._container_environment(request, target).items()):
            command.extend(("--env", f"{key}={value}"))
        for mount in request.mounts:
            specification = f"type=bind,src={mount.source},dst={mount.target}"
            if mount.read_only:
                specification += ",readonly"
            command.extend(("--mount", specification))
        command.extend(("--entrypoint", request.argv[0], request.container_image))
        command.extend(request.argv[1:])
        return tuple(command)

    @staticmethod
    def _container_environment(request: ExecutionRequest, target: TargetSpec) -> dict[str, str]:
        environment = dict(request.environment)
        for key in environment:
            if SAFE_ENVIRONMENT_NAME.fullmatch(key) is None:
                raise ExecutionSafetyError(f"invalid environment variable name: {key!r}")
            if SECRET_ENVIRONMENT_NAME.search(key):
                raise ExecutionSafetyError(
                    f"secrets cannot be embedded in ExecutionRequest.environment: {key}"
                )
        if request.lease_scope is not LeaseScope.NONE:
            visibility = {
                PHYSICAL_HCU_VISIBILITY_VARIABLE: str(
                    target.execution_host.accelerator.device_index
                ),
                # ROCR first selects the physical HCU. HIP then addresses that
                # filtered device set, where the sole leased device is index 0.
                LOGICAL_HCU_VISIBILITY_VARIABLE: "0",
            }
            for key, expected in visibility.items():
                declared = environment.get(key)
                if declared is not None and declared != expected:
                    raise ExecutionSafetyError(
                        f"{key}={declared} conflicts with locked HCU mapping; "
                        f"expected {expected}"
                    )
                environment[key] = expected
        return environment

    @staticmethod
    def _attempt_directory(output_dir: Path, request_id: UUID) -> Path:
        root = output_dir.resolve() / "executions" / str(request_id)
        root.mkdir(parents=True, exist_ok=True)
        attempt = root / f"attempt-{uuid4().hex}"
        attempt.mkdir()
        return attempt

    def _terminate_owned_container(self, owned: _OwnedExecution, reason: str) -> dict[str, object]:
        removed = self.runner.run(("docker", "rm", "--force", owned.container_name), timeout=30)
        stderr = _bounded_text(removed.stderr)
        not_found = "No such container" in stderr
        return {
            "requested": True,
            "reason": reason,
            "container_removed": removed.returncode == 0 or not_found,
            "remove_exit_code": removed.returncode,
            "remove_error": None if removed.returncode == 0 or not_found else stderr,
        }


class SSHExecutionAdapter(ContainerExecutionAdapter):
    """Container executor reached through an authenticated OpenSSH transport."""

    def __init__(
        self,
        host: str,
        *,
        user: str,
        port: int = 22,
        identity_file: Path | None = None,
        profile: str = "nmz36-framework-smoke-v1",
        fencing_guard: FencingGuard | None = None,
    ) -> None:
        super().__init__(
            OpenSSHCommandRunner(
                host,
                user=user,
                port=port,
                identity_file=identity_file,
            ),
            profile=profile,
            adapter_name="SSHExecutionAdapter",
            fencing_guard=fencing_guard,
        )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _bounded_text(value: bytes, limit: int = 4096) -> str:
    return value.decode("utf-8", errors="replace")[:limit]
