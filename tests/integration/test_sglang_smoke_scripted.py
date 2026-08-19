from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from hcuopt.evaluation import sglang_smoke_runner as runner
from hcuopt.evaluation.sglang_smoke import compare_outputs, normalize_response

ROOT = Path(__file__).parents[2]
STUB_SERVER = ROOT / "tests" / "fixtures" / "sglang_stub_server.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _spec(port: int, **updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "protocol_version": "sglang-smoke-v1",
        "workload_id": "scripted-sglang-smoke-v1",
        "target_id": "scripted-target",
        "model_path": "/fixture/model",
        "served_model_name": "hcuopt-smoke",
        "host": "127.0.0.1",
        "port": port,
        "tensor_parallel_size": 1,
        "prompt": "The capital of France is",
        "temperature": 0.0,
        "max_new_tokens": 8,
        "sampling_seed": 0,
        "stream": False,
        "ready_path": "/health_generate",
        "generate_path": "/generate",
        "ready_timeout_seconds": 3.0,
        "ready_poll_interval_seconds": 0.02,
        "request_timeout_seconds": 2.0,
        "stop_grace_seconds": 0.5,
        "execution_timeout_seconds": 8,
        "runner_python_executable": "python",
        "server_entrypoint": "sglang",
        "server_subcommand": "serve",
        "trust_remote_code": True,
        "attention_backend": "fa3",
        "page_size": 64,
        "mem_fraction_static": 0.85,
        "cookbook_repository": "https://github.com/HYGON-AI/inference-cookbook-das",
        "cookbook_commit": "2a7f431301e41e6ea1f377129bf5ba3e43ae299f",
        "cookbook_paths": [
            "CONTRIBUTING.md",
            "docs/model-deployment/sglang/qwen3.5.md",
        ],
    }
    value.update(updates)
    return value


def _stub_argv(port: int, *arguments: str) -> list[str]:
    return [
        sys.executable,
        str(STUB_SERVER),
        "--port",
        str(port),
        *arguments,
    ]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_complete_variant(directory: Path) -> None:
    assert {path.name for path in directory.iterdir()} == set(
        runner._evidence_file_names()
    )
    assert not list(directory.glob("*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX evidence permission semantics")
def test_evidence_is_host_readable_under_restrictive_umask(tmp_path: Path) -> None:
    evidence = tmp_path / "restricted-umask"
    previous_umask = os.umask(0o077)
    try:
        spec = _spec(_free_port())
        spec["seed"] = 0
        result = runner.run_smoke(spec, evidence)
    finally:
        os.umask(previous_umask)

    assert result["status"] == "failed"
    _assert_complete_variant(evidence)
    assert {
        path.name: path.stat().st_mode & 0o777 for path in evidence.iterdir()
    } == {name: 0o644 for name in runner._evidence_file_names()}


def test_runner_retries_ready_generates_once_and_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    evidence = tmp_path / "baseline"
    monkeypatch.setenv("HIP_VISIBLE_DEVICES", "7")
    monkeypatch.setenv("SECRET_TOKEN", "must-not-be-recorded")
    result = runner.run_smoke(
        _spec(port),
        evidence,
        server_argv_override=_stub_argv(port, "--ready-failures", "2"),
    )

    assert result["status"] == "succeeded"
    assert result["normalized_output"] == {
        "text": " Paris",
        "finish_reason_type": "stop",
        "prompt_tokens": 5,
        "completion_tokens": 2,
    }
    ready = [json.loads(line) for line in (evidence / "ready.jsonl").read_text().splitlines()]
    assert ready[-1]["outcome"] == "ready"
    assert sum(item["outcome"] == "retry" for item in ready) >= 2
    response = _read_json(evidence / "response.json")
    assert response["body_json"]["meta_info"]["generate_count"] == 1
    assert _read_json(evidence / "request.json")["body"]["sampling_params"] == {
        "temperature": 0.0,
        "max_new_tokens": 8,
        "sampling_seed": 0,
    }
    stop = _read_json(evidence / "stop.json")
    assert stop["cleanup_succeeded"] is True
    assert stop["term_sent"] is True
    environment = (evidence / "environment.json").read_text(encoding="utf-8")
    assert "HIP_VISIBLE_DEVICES" in environment
    assert "SECRET_TOKEN" not in environment
    _assert_complete_variant(evidence)

    if os.name == "posix":
        pid = _read_json(evidence / "start.json")["pid"]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_server_early_exit_preserves_failure_and_cleanup_evidence(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "early-exit"
    result = runner.run_smoke(
        _spec(port),
        evidence,
        server_argv_override=_stub_argv(port, "--exit-immediately"),
    )
    assert result["status"] == "failed"
    assert "exited before ready" in result["error"]["message"]
    assert _read_json(evidence / "stop.json")["cleanup_succeeded"] is True
    _assert_complete_variant(evidence)


def test_ready_timeout_still_stops_the_server(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "ready-timeout"
    result = runner.run_smoke(
        _spec(port, ready_timeout_seconds=0.2),
        evidence,
        server_argv_override=_stub_argv(port, "--ready-failures", "10000"),
    )
    assert result["status"] == "failed"
    assert "ready deadline exceeded" in result["error"]["message"]
    assert _read_json(evidence / "response.json")["attempted"] is False
    assert _read_json(evidence / "stop.json")["cleanup_succeeded"] is True
    ready = [json.loads(line) for line in (evidence / "ready.jsonl").read_text().splitlines()]
    assert ready[-1]["outcome"] == "timeout"
    _assert_complete_variant(evidence)


def test_generate_non_200_preserves_the_response_envelope(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "generate-500"
    result = runner.run_smoke(
        _spec(port),
        evidence,
        server_argv_override=_stub_argv(port, "--generate-status", "500"),
    )
    assert result["status"] == "failed"
    response = _read_json(evidence / "response.json")
    assert response["attempted"] is True
    assert response["http_status"] == 500
    assert response["body_json"] == {"error": "scripted failure"}
    assert _read_json(evidence / "stop.json")["cleanup_succeeded"] is True


def test_generate_timeout_is_recorded_as_an_attempted_request(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "generate-timeout"
    result = runner.run_smoke(
        _spec(port, request_timeout_seconds=0.1),
        evidence,
        server_argv_override=_stub_argv(port, "--generate-delay", "1"),
    )
    assert result["status"] == "failed"
    response = _read_json(evidence / "response.json")
    assert response["attempted"] is True
    assert response["http_status"] is None
    assert _read_json(evidence / "stop.json")["cleanup_succeeded"] is True


@pytest.mark.parametrize(
    ("argument", "message"),
    (
        ("--invalid-json", "not valid JSON"),
        ("--invalid-shape", "meta_info must be an object"),
    ),
)
def test_invalid_generate_response_is_evidence_backed(
    tmp_path: Path, argument: str, message: str
) -> None:
    port = _free_port()
    evidence = tmp_path / argument.removeprefix("--")
    result = runner.run_smoke(
        _spec(port),
        evidence,
        server_argv_override=_stub_argv(port, argument),
    )
    assert result["status"] == "failed"
    assert message in result["error"]["message"]
    assert _read_json(evidence / "response.json")["attempted"] is True
    assert _read_json(evidence / "stop.json")["cleanup_succeeded"] is True


def test_oversized_response_is_bounded_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "MAX_RESPONSE_BYTES", 1024)
    port = _free_port()
    evidence = tmp_path / "oversized"
    result = runner.run_smoke(
        _spec(port),
        evidence,
        server_argv_override=_stub_argv(port, "--oversized-bytes", "2048"),
    )
    assert result["status"] == "failed"
    response = _read_json(evidence / "response.json")
    assert response["truncated"] is True
    assert len(response["body_text"].encode()) == 1024
    assert _read_json(evidence / "stop.json")["cleanup_succeeded"] is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group semantics")
def test_ignored_term_escalates_to_process_group_kill(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "forced-kill"
    result = runner.run_smoke(
        _spec(port, stop_grace_seconds=0.1),
        evidence,
        server_argv_override=_stub_argv(port, "--ignore-term"),
    )
    assert result["status"] == "succeeded"
    stop = _read_json(evidence / "stop.json")
    assert stop["term_sent"] is True
    assert stop["kill_sent"] is True
    assert stop["status"] == "killed"
    assert stop["cleanup_succeeded"] is True
    pid = _read_json(evidence / "start.json")["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux child-subreaper semantics",
)
def test_adopted_descendant_is_killed_and_reaped(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "descendant-kill"
    result = runner.run_smoke(
        _spec(port, stop_grace_seconds=0.1),
        evidence,
        server_argv_override=_stub_argv(port, "--spawn-child-ignore-term"),
        enable_child_subreaper=True,
    )
    assert result["status"] == "succeeded"
    start = _read_json(evidence / "start.json")
    assert start["child_subreaper_enabled"] is True
    stop = _read_json(evidence / "stop.json")
    assert stop["kill_sent"] is True
    assert stop["cleanup_succeeded"] is True


def test_baseline_and_noop_runs_compare_only_normalized_output(tmp_path: Path) -> None:
    baseline_port = _free_port()
    noop_port = _free_port()
    baseline_dir = tmp_path / "baseline"
    noop_dir = tmp_path / "noop"
    baseline = runner.run_smoke(
        _spec(baseline_port),
        baseline_dir,
        server_argv_override=_stub_argv(baseline_port),
    )
    noop = runner.run_smoke(
        _spec(noop_port),
        noop_dir,
        server_argv_override=_stub_argv(noop_port),
    )
    assert baseline["status"] == noop["status"] == "succeeded"
    baseline_output = normalize_response(_read_json(baseline_dir / "response.json")["body_json"])
    noop_output = normalize_response(_read_json(noop_dir / "response.json")["body_json"])
    assert compare_outputs(baseline_output, noop_output).passed is True

    changed_port = _free_port()
    changed_dir = tmp_path / "changed-noop"
    changed = runner.run_smoke(
        _spec(changed_port),
        changed_dir,
        server_argv_override=_stub_argv(
            changed_port,
            "--response-text",
            " Lyon",
        ),
    )
    assert changed["status"] == "succeeded"
    changed_output = normalize_response(
        _read_json(changed_dir / "response.json")["body_json"]
    )
    comparison = compare_outputs(baseline_output, changed_output)
    assert comparison.passed is False
    assert set(comparison.differences) == {"text"}


def test_baseline_success_and_noop_failure_keep_both_evidence_trees(tmp_path: Path) -> None:
    baseline_port = _free_port()
    noop_port = _free_port()
    baseline_dir = tmp_path / "baseline"
    noop_dir = tmp_path / "noop"
    baseline = runner.run_smoke(
        _spec(baseline_port),
        baseline_dir,
        server_argv_override=_stub_argv(baseline_port),
    )
    noop = runner.run_smoke(
        _spec(noop_port),
        noop_dir,
        server_argv_override=_stub_argv(noop_port, "--invalid-json"),
    )
    assert baseline["status"] == "succeeded"
    assert noop["status"] == "failed"
    _assert_complete_variant(baseline_dir)
    _assert_complete_variant(noop_dir)


def test_invalid_spec_fails_before_spawn_but_writes_all_evidence(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "invalid-spec"
    spec = _spec(port)
    spec["seed"] = 0
    result = runner.run_smoke(
        spec,
        evidence,
        server_argv_override=_stub_argv(port),
    )
    assert result["status"] == "failed"
    assert "unknown fields: seed" in result["error"]["message"]
    assert _read_json(evidence / "start.json")["status"] == "not_started"
    assert _read_json(evidence / "stop.json")["cleanup_succeeded"] is True
    _assert_complete_variant(evidence)


def test_cli_loads_spec_writes_evidence_and_returns_failure_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = tmp_path / "invalid-spec.json"
    spec = _spec(_free_port())
    spec["seed"] = 0
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    evidence = tmp_path / "cli-evidence"
    exit_code = runner.main(
        ["--spec", str(spec_path), "--evidence-dir", str(evidence)]
    )
    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    _assert_complete_variant(evidence)


def test_runner_refuses_to_overwrite_a_previous_attempt(tmp_path: Path) -> None:
    evidence = tmp_path / "attempt-0001" / "baseline"
    evidence.mkdir(parents=True)
    (evidence / "result.json").write_text("historic\n", encoding="utf-8")
    with pytest.raises(runner.SmokeRunnerError, match="refusing to overwrite"):
        runner.run_smoke(_spec(_free_port()), evidence)
    assert (evidence / "result.json").read_text(encoding="utf-8") == "historic\n"
