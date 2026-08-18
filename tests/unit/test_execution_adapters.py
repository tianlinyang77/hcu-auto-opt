from __future__ import annotations

import json
import shlex
import subprocess
import threading
from pathlib import Path
from typing import BinaryIO

import pytest

from hcuopt.adapters.execution import (
    FENCING_LABEL,
    RESOURCE_LABEL,
    CommandResult,
    ContainerExecutionAdapter,
    FencingGuard,
    OpenSSHCommandRunner,
)
from hcuopt.adapters.resource_cleaner import (
    ContainerResourceCleaner,
    cleanup_is_healthy,
)
from hcuopt.contracts.platform_v1 import ExecutionRequest
from hcuopt.domain.enums import LeaseScope
from hcuopt.domain.errors import ExecutionSafetyError, ImageIdentityError, StaleFencingToken
from hcuopt.targets import load_target

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")


class FakeProcess:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class ScriptedDockerRunner:
    def __init__(self, *, process_returncode: int | None = 0) -> None:
        self.process_returncode = process_returncode
        self.commands: list[tuple[str, ...]] = []
        self.process: FakeProcess | None = None
        self.started = threading.Event()
        self.containers: dict[str, dict[str, str]] = {}
        self.image_id = TARGET.inference_image.image_id
        self.repo_digests = [TARGET.inference_image.immutable_reference]

    def run(self, argv, timeout: float = 30.0) -> CommandResult:
        command = tuple(argv)
        self.commands.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            payload = json.dumps([{"Id": self.image_id, "RepoDigests": self.repo_digests}]).encode()
            return CommandResult(command, 0, payload, b"")
        if command[:3] == ("docker", "rm", "--force"):
            identifier = command[3]
            self.containers.pop(identifier, None)
            if self.process is not None:
                self.process.returncode = 137
            return CommandResult(command, 0, identifier.encode(), b"")
        if command[:3] == ("docker", "ps", "--all"):
            resource_filter = next(
                (
                    item.split("=", 2)[-1]
                    for item in command
                    if item.startswith(f"label={RESOURCE_LABEL}=")
                ),
                None,
            )
            identifiers = [
                identifier
                for identifier, labels in self.containers.items()
                if labels.get(RESOURCE_LABEL) == resource_filter
            ]
            return CommandResult(command, 0, "\n".join(identifiers).encode(), b"")
        if command[:3] == ("docker", "inspect", "--format"):
            labels = self.containers.get(command[-1], {})
            return CommandResult(command, 0, json.dumps(labels).encode(), b"")
        if command and command[0] == "hy-smi":
            return CommandResult(command, 0, b"HCU[7]: Healthy", b"")
        raise AssertionError(f"unexpected command: {command}")

    def popen(
        self,
        argv,
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> FakeProcess:
        command = tuple(argv)
        self.commands.append(command)
        stdout.write(b"Python 3.10.12\n")
        stderr.write(b"")
        self.process = FakeProcess(self.process_returncode)
        self.started.set()
        return self.process


def request(*, token: int = 1, timeout: int = 30) -> ExecutionRequest:
    return ExecutionRequest(
        target_id=TARGET.target_id,
        argv=["python", "--version"],
        working_directory="/work",
        timeout_seconds=timeout,
        lease_scope=LeaseScope.EXCLUSIVE,
        resource_id="hcu-7",
        fencing_token=token,
        container_image=TARGET.inference_image.immutable_reference,
    )


def test_ssh_runner_preserves_argv_boundaries() -> None:
    runner = OpenSSHCommandRunner("example.invalid", user="worker")
    remote = ("printf", "%s", "value; touch /tmp/not-created", "$(id)")
    wrapped = runner.wrapped_argv(remote)
    assert wrapped[-2] == "worker@example.invalid"
    assert shlex.split(wrapped[-1]) == list(remote)
    assert "BatchMode=yes" in wrapped
    assert "StrictHostKeyChecking=yes" in wrapped


def test_container_executor_uses_locked_identity_and_topology(tmp_path: Path) -> None:
    runner = ScriptedDockerRunner()
    adapter = ContainerExecutionAdapter(runner, poll_interval_seconds=0.001)
    result = adapter.execute(request(), TARGET, tmp_path)

    assert result.status == "succeeded"
    assert result.synthetic is False
    assert result.stdout_uri is not None and result.stdout_uri.startswith("file:")
    command = runner.commands[-1]
    assert command[:2] == ("docker", "run")
    assert "--pull=never" in command
    assert TARGET.inference_image.immutable_reference in command
    assert TARGET.execution_host.accelerator.cpu_affinity in command
    assert str(TARGET.execution_host.accelerator.numa_node) in command
    assert "HIP_VISIBLE_DEVICES=0" in command
    assert "ROCR_VISIBLE_DEVICES=7" in command
    assert result.metadata["image_id"] == TARGET.inference_image.image_id
    assert result.metadata["registry_digest_verified"] is True
    assert result.metadata["fencing_validated_before_result"] is True


def test_container_executor_rejects_wrong_image_identity(tmp_path: Path) -> None:
    runner = ScriptedDockerRunner()
    runner.image_id = "sha256:" + "0" * 64
    adapter = ContainerExecutionAdapter(runner)
    with pytest.raises(ImageIdentityError, match="image ID"):
        adapter.execute(request(), TARGET, tmp_path)


def test_container_executor_rejects_secret_environment(tmp_path: Path) -> None:
    runner = ScriptedDockerRunner()
    adapter = ContainerExecutionAdapter(runner)
    unsafe = request().model_copy(update={"environment": {"API_TOKEN": "secret"}})
    with pytest.raises(ExecutionSafetyError, match="secrets"):
        adapter.execute(unsafe, TARGET, tmp_path)
    assert runner.commands == []


def test_container_executor_rejects_physical_index_as_filtered_hip_index(
    tmp_path: Path,
) -> None:
    runner = ScriptedDockerRunner()
    adapter = ContainerExecutionAdapter(runner)
    unsafe = request().model_copy(
        update={"environment": {"HIP_VISIBLE_DEVICES": "7"}}
    )
    with pytest.raises(ExecutionSafetyError, match="expected 0"):
        adapter.execute(unsafe, TARGET, tmp_path)
    assert runner.commands == []


def test_timeout_removes_only_the_adapter_owned_container(tmp_path: Path) -> None:
    runner = ScriptedDockerRunner(process_returncode=None)
    adapter = ContainerExecutionAdapter(runner, poll_interval_seconds=0.001)
    result = adapter.execute(request(timeout=1), TARGET, tmp_path)
    assert result.status == "timed_out"
    assert result.metadata["termination"]["container_removed"] is True
    remove = [item for item in runner.commands if item[:3] == ("docker", "rm", "--force")]
    assert remove == [("docker", "rm", "--force", f"hcuopt-{result.request_id.hex}")]


def test_cancel_stops_the_owned_container_and_returns_cancelled(tmp_path: Path) -> None:
    runner = ScriptedDockerRunner(process_returncode=None)
    adapter = ContainerExecutionAdapter(runner, poll_interval_seconds=0.001)
    execution_request = request()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(adapter.execute(execution_request, TARGET, tmp_path))
    )
    thread.start()
    assert runner.started.wait(timeout=2)
    cancellation = adapter.cancel(execution_request.request_id)
    thread.join(timeout=5)

    assert cancellation["owned_execution_found"] is True
    assert results[0].status == "cancelled"
    assert not thread.is_alive()


def test_fencing_guard_rejects_old_token_before_launch(tmp_path: Path) -> None:
    runner = ScriptedDockerRunner()
    guard = FencingGuard()
    guard.before_execute("hcu-7", 2)
    adapter = ContainerExecutionAdapter(runner, fencing_guard=guard)
    with pytest.raises(StaleFencingToken, match="older"):
        adapter.execute(request(token=1), TARGET, tmp_path)
    assert runner.commands == []


def test_cleaner_preserves_newer_fencing_generation() -> None:
    runner = ScriptedDockerRunner()
    runner.containers = {
        "old": {RESOURCE_LABEL: "hcu-7", FENCING_LABEL: "1"},
        "current": {RESOURCE_LABEL: "hcu-7", FENCING_LABEL: "2"},
        "newer": {RESOURCE_LABEL: "hcu-7", FENCING_LABEL: "3"},
    }
    cleaner = ContainerResourceCleaner(TARGET, runner)
    fence = cleaner.fence("hcu-7", 2)
    assert set(fence["owned_containers_removed"]) == {"old", "current"}
    assert fence["newer_containers_preserved"] == ["newer"]
    assert fence["fenced"] is True
    health = cleaner.health_check("hcu-7")
    assert health["healthy"] is False
    assert health["quarantined"] is True
    assert cleanup_is_healthy({"fence": fence, "health": health}) is False
