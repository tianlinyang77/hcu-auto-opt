from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest

from hcuopt.adapters.agent_runner import (
    AgentInputFile,
    AgentRunLimits,
    AgentRunnerAdapter,
    AgentRunnerSafetyError,
    AgentRunRequest,
    DeterministicAgentRunner,
    LocalCommandAgentRunner,
)

ROOT = Path(__file__).parents[2]
STUB = ROOT / "tests" / "fixtures" / "agent_runner_stub.py"
REQUEST_HASH = "sha256:" + "a" * 64


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
) -> AgentRunRequest:
    return AgentRunRequest(
        attempt_id=UUID("00000000-0000-0000-0000-000000000114"),
        generation_run_id=UUID("00000000-0000-0000-0000-000000000112"),
        request_hash=REQUEST_HASH,
        executable=executable or Path(sys.executable),
        argv=(str(STUB), mode, *values, *options),
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

    assert isinstance(runner, AgentRunnerAdapter)
    result = runner.run(_request(), tmp_path)

    assert result.status == "succeeded"
    assert result.proposal_bytes == b'{"proposal":"fixture"}'
    assert result.evidence.synthetic is True
    assert result.evidence.attempts_consumed == 1
    assert result.evidence.tokens_consumed == 9
    assert result.evidence.performance_conclusion == "not_measured"
    assert result.evidence.hcu_access_allowed is False
    assert result.evidence.measurement_access_allowed is False


def test_local_runner_preserves_literal_argv_without_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    literal_values = ("value; touch", str(marker), "$(id)", "a b")

    result = _runner().run(_request("argv", values=literal_values), tmp_path)

    assert result.status == "succeeded"
    assert json.loads(result.proposal_bytes or b"null") == list(literal_values)
    assert not marker.exists()
    assert result.evidence.executable_name == Path(sys.executable).name
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


def test_timeout_terminates_the_child_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    result = _runner().run(
        _request(
            "spawn-child",
            options=("--marker", str(marker), "--seconds", "30"),
            limits=_limits(timeout_seconds=1.0),
        ),
        tmp_path,
    )

    assert result.status == "timed_out"
    assert result.proposal_bytes is None
    assert result.evidence.termination_reason == "timeout"
    child_pid = int(marker.read_text(encoding="ascii"))
    deadline = time.monotonic() + 3
    while _pid_is_active(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_is_active(child_pid)


def test_cleanup_failure_suppresses_otherwise_valid_output(tmp_path: Path) -> None:
    def remove_then_fail(path: Path) -> None:
        shutil.rmtree(path)
        raise OSError("injected cleanup failure")

    result = _runner(remove_tree=remove_then_fail).run(_request(), tmp_path)

    assert result.status == "cleanup_failed"
    assert result.proposal_bytes is None
    assert result.evidence.cleanup_status == "failed"
    assert "injected cleanup failure" in result.evidence.cleanup_summary
