from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.adapters.agent_runner import (
    AgentDeploymentCredential,
    AgentInputFile,
    AgentRunLimits,
    AgentRunnerAdapter,
    AgentRunnerSafetyError,
    AgentRunRequest,
    AgentRunResult,
    DeterministicAgentRunner,
    LocalCommandAgentRunner,
)
from hcuopt.adapters.agent_runner_receipt import RunnerExecutionReceiptStore
from hcuopt.contracts.agent_runner_v1 import RunnerExecutionRecord
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.source_hash import file_uri_to_path

ROOT = Path(__file__).parents[2]
STUB = ROOT / "tests" / "fixtures" / "agent_runner_stub.py"
REQUEST_HASH = "sha256:" + "a" * 64


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _limits(**updates: object) -> AgentRunLimits:
    values: dict[str, object] = {
        "attempt_number": 1,
        "timeout_seconds": 5.0,
        "max_stdout_bytes": 8_192,
        "max_stderr_bytes": 8_192,
        "max_total_output_bytes": 12_288,
        "max_tokens": 100,
        "termination_grace_seconds": 0.5,
    }
    values.update(updates)
    return AgentRunLimits(**values)


def _request(
    mode: str = "success",
    *,
    values: tuple[str, ...] = (),
    options: tuple[str, ...] = (),
    environment: tuple[tuple[str, str], ...] = (),
    inputs: tuple[AgentInputFile, ...] = (),
    limits: AgentRunLimits | None = None,
    executable: Path | None = None,
    generator_artifact: Path | None = None,
    generator_artifact_hash: str | None = None,
    argv_artifact: Path | None = None,
) -> AgentRunRequest:
    artifact = generator_artifact or STUB
    return AgentRunRequest(
        attempt_id=UUID("00000000-0000-0000-0000-000000000114"),
        generation_run_id=UUID("00000000-0000-0000-0000-000000000112"),
        request_id=UUID("00000000-0000-0000-0000-000000000111"),
        request_hash=REQUEST_HASH,
        plan_id=UUID("00000000-0000-0000-0000-000000000113"),
        generator_id="agent-runner-test",
        executable=executable or Path(sys.executable),
        generator_artifact=artifact,
        generator_artifact_hash=generator_artifact_hash or _file_hash(artifact),
        argv=(str(argv_artifact or artifact), mode, *values, *options),
        environment=environment,
        input_files=inputs,
        limits=limits or _limits(),
    )


def _runner(**updates: object) -> LocalCommandAgentRunner:
    values: dict[str, object] = {
        "allowed_executables": (Path(sys.executable),),
        "allowed_argv_prefixes": ((str(STUB),),),
        "allowed_environment_names": frozenset({"SAFE_FLAG"}),
        "path_exists": lambda _path: False,
        "poll_interval_seconds": 0.01,
    }
    values.update(updates)
    return LocalCommandAgentRunner(**values)


def _pid_is_active(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
        process_query_limited_information, False, pid
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
            handle, ctypes.byref(exit_code)
        ):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def test_deterministic_runner_uses_the_runtime_protocol_and_budget_evidence(
    tmp_path: Path,
) -> None:
    runner = DeterministicAgentRunner(
        proposal_bytes=b'{"proposal":"fixture"}',
        reported_tokens=9,
    )
    request = _request()

    assert isinstance(runner, AgentRunnerAdapter)
    result = runner.run(request, tmp_path)

    assert result.status == "succeeded"
    assert result.proposal_bytes == b'{"proposal":"fixture"}'
    assert result.evidence.synthetic is True
    assert result.evidence.attempts_consumed == 1
    assert result.evidence.tokens_consumed == 9
    assert result.evidence.attempt_id == request.attempt_id
    assert result.evidence.generation_run_id == request.generation_run_id
    assert result.evidence.request_hash == request.request_hash
    assert result.evidence.request_id == request.request_id
    assert result.evidence.plan_id == request.plan_id
    assert result.evidence.generator_id == request.generator_id
    assert result.evidence.attempt_number == request.limits.attempt_number
    assert result.evidence.generator_artifact_hash == request.generator_artifact_hash
    assert result.evidence.runner_provenance.profile == runner.provenance.profile
    assert result.evidence.runner_provenance.identity_hash.startswith("sha256:")
    assert result.evidence.performance_conclusion == "not_measured"
    assert result.evidence.hcu_access_allowed is False
    assert result.evidence.measurement_access_allowed is False


def test_runner_receipt_store_publishes_and_rereads_exact_output(tmp_path: Path) -> None:
    runner = DeterministicAgentRunner(
        proposal_bytes=b'{"proposal":"fixture"}',
        reported_tokens=9,
    )
    result = runner.run(_request(), tmp_path / "runner")
    store = RunnerExecutionReceiptStore(tmp_path / "receipts")

    first = store.publish(result)
    second = store.publish(result)
    receipt = store.load(first)

    assert first == second
    assert receipt.execution == result.evidence
    assert receipt.raw_output_hash == result.evidence.stdout_hash
    assert receipt.raw_output_bytes == len(result.proposal_bytes or b"")
    assert receipt.raw_output_uri is not None
    assert receipt.automatic_release_allowed is False


def test_runner_receipt_store_rejects_tampered_output(tmp_path: Path) -> None:
    runner = DeterministicAgentRunner(proposal_bytes=b"proposal", reported_tokens=1)
    result = runner.run(_request(), tmp_path / "runner")
    store = RunnerExecutionReceiptStore(tmp_path / "receipts")
    reference = store.publish(result)
    receipt = store.load(reference)
    assert receipt.raw_output_uri is not None
    file_uri_to_path(receipt.raw_output_uri).write_bytes(b"tampered")

    with pytest.raises(SourceArtifactError, match="raw output changed"):
        store.load(reference)


@pytest.mark.parametrize("exit_code", [None, 17])
def test_successful_runner_contract_requires_zero_exit_code(
    tmp_path: Path, exit_code: int | None
) -> None:
    result = DeterministicAgentRunner(
        proposal_bytes=b'{"proposal":"fixture"}', reported_tokens=9
    ).run(_request(), tmp_path / "runner")
    payload = result.evidence.model_dump(mode="json")
    payload["exit_code"] = exit_code

    with pytest.raises(ValidationError, match="exit_code 0"):
        RunnerExecutionRecord.model_validate(payload)


def test_runner_receipt_store_revalidates_execution_contract(tmp_path: Path) -> None:
    result = DeterministicAgentRunner(
        proposal_bytes=b'{"proposal":"fixture"}', reported_tokens=9
    ).run(_request(), tmp_path / "runner")
    forged = AgentRunResult(
        proposal_bytes=result.proposal_bytes,
        evidence=result.evidence.model_copy(update={"exit_code": 17}),
    )

    with pytest.raises(SourceArtifactError, match="invalid Execution Record"):
        RunnerExecutionReceiptStore(tmp_path / "receipts").publish(forged)


def test_local_runner_preserves_literal_argv_without_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    literal_values = ("value; touch", str(marker), "$(id)", "a b")

    result = _runner().run(_request("argv", values=literal_values), tmp_path)

    assert result.status == "succeeded"
    assert json.loads(result.proposal_bytes or b"null") == list(literal_values)
    assert not marker.exists()
    assert result.evidence.executable_name == Path(sys.executable).name
    assert result.evidence.executable_hash == _file_hash(Path(sys.executable))
    assert result.evidence.argv_hash.startswith("sha256:")


def test_local_runner_constructs_environment_and_never_records_values(
    tmp_path: Path,
) -> None:
    result = _runner().run(
        _request(
            "environment",
            options=("--environment-name", "SAFE_FLAG"),
            environment=(("SAFE_FLAG", "visible-only-to-child"),),
        ),
        tmp_path,
    )

    assert result.status == "succeeded"
    assert result.proposal_bytes == b"visible-only-to-child"
    assert result.evidence.environment_names == ("SAFE_FLAG",)
    assert "visible-only-to-child" not in repr(result.evidence)


def test_deployment_credential_is_private_request_invisible_and_redacted(
    tmp_path: Path,
) -> None:
    secret = "deployment-owned-secret"
    name = "HCUOPT_DEPLOYMENT_PROVIDER_API_KEY_FILE"
    runner = _runner(
        deployment_credentials=(
            AgentDeploymentCredential(environment_name=name, content=secret.encode()),
        )
    )
    result = runner.run(
        _request("credential", options=("--environment-name", name)),
        tmp_path,
    )

    assert result.status == "succeeded"
    assert result.proposal_bytes == b'{"proposal":"credential was readable"}'
    assert secret not in repr(result.evidence)
    assert secret not in result.evidence.stderr_summary
    assert "<redacted-env>" in result.evidence.stderr_summary
    assert name not in result.evidence.environment_names
    assert not list(tmp_path.glob("agent-run-*"))


def test_request_cannot_override_deployment_credential_binding(tmp_path: Path) -> None:
    name = "HCUOPT_DEPLOYMENT_PROVIDER_API_KEY_FILE"
    runner = _runner(
        allowed_environment_names=frozenset({name}),
        deployment_credentials=(
            AgentDeploymentCredential(environment_name=name, content=b"secret"),
        ),
    )

    with pytest.raises(AgentRunnerSafetyError, match="forbidden environment"):
        runner.run(_request(environment=((name, "caller-path"),)), tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        "API_TOKEN",
        "HIP_VISIBLE_DEVICES",
        "HCUOPT_HOLDOUT_PATH",
        "SSH_AUTH_SOCK",
        "DOCKER_HOST",
        "HCUOPT_INPUT_ROOT",
        "HCUOPT_USAGE_PATH",
        "HCUOPT_REQUEST_HASH",
    ],
)
def test_local_runner_rejects_secret_and_privileged_environment(tmp_path: Path, name: str) -> None:
    runner = _runner(allowed_environment_names=frozenset({name}))

    with pytest.raises(AgentRunnerSafetyError, match="forbidden environment"):
        runner.run(_request(environment=((name, "unsafe"),)), tmp_path)


def test_local_runner_stages_normalized_read_only_inputs(tmp_path: Path) -> None:
    request = _request(
        "readonly-input",
        options=("--input-path", "evidence/request.json"),
        inputs=(AgentInputFile("evidence/request.json", b"frozen"),),
    )

    result = _runner().run(request, tmp_path)

    assert result.status == "succeeded"
    assert json.loads(result.proposal_bytes or b"null") == {
        "content": "frozen",
        "writable": False,
    }
    assert result.evidence.input_manifest_hash.startswith("sha256:")
    assert not list(tmp_path.glob("agent-run-*"))


def test_local_runner_rejects_unallowlisted_argv_and_oversized_input(tmp_path: Path) -> None:
    runner = _runner(allowed_argv_prefixes=((str(STUB), "success"),))
    with pytest.raises(AgentRunnerSafetyError, match="argv prefix"):
        runner.run(_request("argv", values=("unsafe",)), tmp_path)

    with pytest.raises(AgentRunnerSafetyError, match="input byte limit"):
        _runner(max_input_bytes=4).run(
            _request(inputs=(AgentInputFile("request.json", b"12345"),)),
            tmp_path,
        )


def test_environment_names_are_unique_across_platform_case_rules() -> None:
    with pytest.raises(AgentRunnerSafetyError, match="duplicate name"):
        _request(environment=(("SAFE_FLAG", "one"), ("safe_flag", "two")))


@pytest.mark.parametrize("path", ["../escape.json", "/absolute.json", "a\\b.json"])
def test_input_paths_must_be_normalized(path: str) -> None:
    with pytest.raises(AgentRunnerSafetyError, match="normalized relative path"):
        AgentInputFile(path, b"unsafe")


def test_local_runner_rejects_unallowlisted_executable_and_hcu_host(
    tmp_path: Path,
) -> None:
    with pytest.raises(AgentRunnerSafetyError, match="not allowlisted"):
        _runner().run(_request(executable=tmp_path / "other-agent"), tmp_path)

    hcu_path = Path("/dev/kfd")
    with pytest.raises(AgentRunnerSafetyError, match="forbidden host path"):
        _runner(
            forbidden_host_paths=(hcu_path,),
            path_exists=lambda path: path == hcu_path,
        ).run(_request(), tmp_path)


def test_local_runner_rejects_generator_artifact_hash_mismatch(tmp_path: Path) -> None:
    artifact = tmp_path / "generator.py"
    artifact.write_bytes(STUB.read_bytes())
    request = _request(
        generator_artifact=artifact,
        generator_artifact_hash="sha256:" + "0" * 64,
    )

    with pytest.raises(AgentRunnerSafetyError, match="artifact hash"):
        _runner(allowed_argv_prefixes=((str(artifact),),)).run(request, tmp_path)


def test_local_runner_requires_generator_artifact_to_be_executed(tmp_path: Path) -> None:
    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text("print('not the generator')", encoding="utf-8")

    with pytest.raises(AgentRunnerSafetyError, match="executable or an exact argv"):
        _runner().run(
            _request(generator_artifact=unrelated, argv_artifact=STUB),
            tmp_path,
        )


def test_nonzero_exit_and_malformed_usage_fail_closed(tmp_path: Path) -> None:
    failed = _runner().run(_request("nonzero"), tmp_path)
    malformed = _runner().run(_request("malformed-usage"), tmp_path)
    missing = _runner().run(_request("missing-usage"), tmp_path)

    assert failed.status == "failed"
    assert failed.evidence.exit_code == 17
    assert failed.proposal_bytes is None
    assert malformed.status == "invalid_output"
    assert malformed.proposal_bytes is None
    assert missing.status == "invalid_output"
    assert missing.proposal_bytes is None


def test_token_budget_and_secret_summary_fail_safe(tmp_path: Path) -> None:
    exceeded = _runner().run(
        _request("success", options=("--tokens", "101"), limits=_limits(max_tokens=100)),
        tmp_path,
    )
    redacted = _runner().run(_request("secret-output"), tmp_path)

    assert exceeded.status == "token_limit_exceeded"
    assert exceeded.proposal_bytes is None
    assert exceeded.evidence.tokens_consumed == 101
    assert redacted.status == "succeeded"
    assert "super-secret-value" not in redacted.evidence.stderr_summary
    assert "<redacted>" in redacted.evidence.stderr_summary


@pytest.mark.parametrize(
    ("mode", "limits"),
    [
        ("overflow-stdout", _limits(max_stdout_bytes=64, max_total_output_bytes=256)),
        ("overflow-stderr", _limits(max_stderr_bytes=64, max_total_output_bytes=256)),
        ("overflow-stdout", _limits(max_stdout_bytes=256, max_total_output_bytes=64)),
    ],
)
def test_stream_and_total_output_limits_terminate_the_process(
    tmp_path: Path, mode: str, limits: AgentRunLimits
) -> None:
    result = _runner().run(
        _request(
            mode,
            options=("--bytes", "4096", "--seconds", "10"),
            limits=limits,
        ),
        tmp_path,
    )

    assert result.status == "output_limit_exceeded"
    assert result.proposal_bytes is None
    assert result.evidence.termination_reason == "output_limit_exceeded"
    assert result.evidence.process_tree_cleanup in {"terminated", "killed"}


def test_timeout_terminates_the_child_process_tree_after_ready_handshake(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    ready = tmp_path / "child.ready"
    request = _request(
        "spawn-child",
        options=(
            "--marker",
            str(marker),
            "--ready",
            str(ready),
            "--seconds",
            "30",
        ),
        limits=_limits(timeout_seconds=3.0),
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_runner().run, request, tmp_path)
        ready_deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < ready_deadline:
            time.sleep(0.01)
        assert ready.read_text(encoding="ascii") == "ready"
        result = future.result(timeout=5)

    assert result.status == "timed_out"
    assert result.proposal_bytes is None
    assert result.evidence.termination_reason == "timeout"
    child_pid = int(marker.read_text(encoding="ascii"))
    deadline = time.monotonic() + 3
    while _pid_is_active(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_is_active(child_pid)


def test_normal_parent_exit_still_terminates_background_child(tmp_path: Path) -> None:
    marker = tmp_path / "background.pid"
    ready = tmp_path / "background.ready"
    result = _runner().run(
        _request(
            "spawn-background",
            options=(
                "--marker",
                str(marker),
                "--ready",
                str(ready),
                "--seconds",
                "30",
            ),
        ),
        tmp_path,
    )

    assert ready.read_text(encoding="ascii") == "ready"
    assert result.status == "succeeded"
    assert result.evidence.process_tree_cleanup in {"terminated", "killed"}
    child_pid = int(marker.read_text(encoding="ascii"))
    deadline = time.monotonic() + 3
    while _pid_is_active(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_is_active(child_pid)


@pytest.mark.parametrize(
    "updates",
    [
        {"timeout_seconds": math.nan},
        {"timeout_seconds": math.inf},
        {"timeout_seconds": -math.inf},
        {"timeout_seconds": 1},
        {"termination_grace_seconds": math.nan},
        {"termination_grace_seconds": math.inf},
        {"termination_grace_seconds": 1},
        {"attempt_number": True},
        {"attempt_number": 1.0},
        {"max_stdout_bytes": True},
        {"max_stderr_bytes": 1.0},
        {"max_total_output_bytes": False},
        {"max_tokens": 1.0},
    ],
)
def test_limits_reject_non_finite_and_wrong_runtime_types(updates: dict[str, object]) -> None:
    with pytest.raises(AgentRunnerSafetyError):
        _limits(**updates)


def test_cleanup_failure_suppresses_otherwise_valid_output(tmp_path: Path) -> None:
    def remove_then_fail(path: Path) -> None:
        shutil.rmtree(path)
        raise OSError("injected cleanup failure")

    result = _runner(remove_tree=remove_then_fail).run(_request(), tmp_path)

    assert result.status == "cleanup_failed"
    assert result.proposal_bytes is None
    assert result.evidence.cleanup_status == "failed"
    assert "injected cleanup failure" in result.evidence.cleanup_summary
