from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from hcuopt.evaluation import sglang_endpoint_runner as runner

ROOT = Path(__file__).parents[2]
STUB_SERVER = ROOT / "tests" / "fixtures" / "sglang_stub_server.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _spec(port: int, **updates: Any) -> dict[str, Any]:
    smoke_spec = {
        "protocol_version": "sglang-smoke-v1",
        "workload_id": "scripted-endpoint-v1",
        "target_id": "scripted-target",
        "model_path": "/fixture/model",
        "served_model_name": "hcuopt-endpoint",
        "host": "127.0.0.1",
        "port": port,
        "tensor_parallel_size": 1,
        "prompt": "The capital of France is",
        "temperature": 0.0,
        "max_new_tokens": 2,
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
        "cookbook_paths": ["docs/model-deployment/sglang/qwen3.5.md"],
    }
    value: dict[str, Any] = {
        "protocol_version": "sglang-endpoint-acquisition-v1",
        "arm": "baseline",
        "acquisition_ordinal": 0,
        "warmup_requests": 1,
        "measured_requests": 2,
        "expected_prompt_tokens": 5,
        "expected_completion_tokens": 2,
        "ignore_eos": True,
        "activation_attestation": None,
        "smoke_spec": smoke_spec,
    }
    value.update(updates)
    return value


def _stub_argv(port: int, *arguments: str) -> list[str]:
    return [sys.executable, str(STUB_SERVER), "--port", str(port), *arguments]


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_provisional_acquisition_records_warmup_and_each_measured_request(
    tmp_path: Path,
) -> None:
    port = _free_port()
    evidence = tmp_path / "acquisition"
    cache = tmp_path / "cache"
    cache.mkdir()
    result = runner.run_acquisition(
        _spec(port),
        evidence,
        server_argv_override=_stub_argv(port),
        cache_parent=cache,
    )

    assert result == {
        "protocol_version": "sglang-endpoint-acquisition-v1",
        "status": "succeeded",
        "run_mode": "provisional",
        "completed_requests": 2,
        "expected_requests": 2,
        "cleanup_succeeded": True,
        "cache_cleanup_succeeded": True,
        "error": None,
        "producer_verdict": None,
        "automatic_release_allowed": False,
    }
    assert _read(evidence / "warmup" / "0000" / "sample.json")["measured"] is False
    assert (
        _read(evidence / "warmup" / "0000" / "request.json")["body"]["sampling_params"][
            "ignore_eos"
        ]
        is True
    )
    samples = [_read(evidence / "requests" / f"{index:04d}" / "sample.json") for index in range(2)]
    assert all(item["succeeded"] for item in samples)
    assert [item["completion_tokens"] for item in samples] == [2, 2]
    assert _read(evidence / "stop.json")["cleanup_succeeded"] is True
    namespace = _read(evidence / "cache-namespace.json")
    assert namespace["empty_before_start"] is True
    assert namespace["namespace_hash"].startswith("sha256:")
    assert _read(evidence / "cache-cleanup.json")["removed"] is True
    assert not list(cache.iterdir())


def test_token_mismatch_fails_closed_and_preserves_raw_response(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "token-mismatch"
    cache = tmp_path / "cache"
    cache.mkdir()
    result = runner.run_acquisition(
        _spec(port, expected_completion_tokens=3),
        evidence,
        server_argv_override=_stub_argv(port),
        cache_parent=cache,
    )

    assert result["status"] == "failed"
    assert result["completed_requests"] == 0
    assert "token counts" in result["error"]["message"]
    assert _read(evidence / "warmup" / "0000" / "response.json")["http_status"] == 200
    assert _read(evidence / "stop.json")["cleanup_succeeded"] is True


def test_http_failure_stops_acquisition_without_dropping_failed_request(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "http-failure"
    cache = tmp_path / "cache"
    cache.mkdir()
    result = runner.run_acquisition(
        _spec(port),
        evidence,
        server_argv_override=_stub_argv(port, "--generate-status", "500"),
        cache_parent=cache,
    )

    assert result["status"] == "failed"
    failed = _read(evidence / "warmup" / "0000" / "sample.json")
    assert failed["succeeded"] is False
    assert failed["http_status"] == 500
    assert not (evidence / "requests").exists()
    assert result["cleanup_succeeded"] is True


def test_required_activation_attestation_is_bound_to_imported_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    evidence = tmp_path / "activation"
    cache = tmp_path / "cache"
    hook = tmp_path / "hook"
    modules = tmp_path / "modules"
    cache.mkdir()
    hook.mkdir()
    modules.mkdir()
    shutil.copyfile(
        ROOT / "src" / "hcuopt" / "evaluation" / "endpoint_sitecustomize.py",
        hook / "sitecustomize.py",
    )
    target = modules / "target_overlay.py"
    target.write_text("VALUE = 7\n", encoding="utf-8")
    expected = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(hook), str(modules))))
    monkeypatch.setenv("HCUOPT_ENDPOINT_TARGET_MODULE", "target_overlay")
    monkeypatch.setenv("HCUOPT_ENDPOINT_TARGET_PATH", str(target.resolve()))
    monkeypatch.setenv("HCUOPT_ENDPOINT_TARGET_SHA256", expected)
    monkeypatch.setenv(
        "HCUOPT_ENDPOINT_ACTIVATION_PATH",
        str((evidence / "activation.json").resolve()),
    )
    spec = _spec(
        port,
        activation_attestation={
            "module_name": "target_overlay",
            "module_path": str(target.resolve()),
            "expected_sha256": expected,
        },
    )

    result = runner.run_acquisition(
        spec,
        evidence,
        server_argv_override=_stub_argv(port, "--import-module", "target_overlay"),
        cache_parent=cache,
    )

    assert result["status"] == "succeeded", result
    activation = _read(evidence / "activation.json")
    assert activation["module_sha256"] == expected
    assert activation["process_id"] == _read(evidence / "start.json")["pid"]


def test_unknown_spec_field_fails_before_server_start(tmp_path: Path) -> None:
    port = _free_port()
    evidence = tmp_path / "invalid-spec"
    spec = _spec(port)
    spec["verdict"] = "faster"
    result = runner.run_acquisition(
        spec,
        evidence,
        server_argv_override=_stub_argv(port),
        cache_parent=tmp_path,
    )

    assert result["status"] == "failed"
    assert "unknown fields: verdict" in result["error"]["message"]
    assert _read(evidence / "start.json")["status"] == "not_started"
    assert _read(evidence / "stop.json")["cleanup_succeeded"] is True


def test_runner_refuses_to_overwrite_historic_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "historic"
    evidence.mkdir()
    (evidence / "result.json").write_text("historic\n", encoding="utf-8")

    with pytest.raises(runner.smoke.SmokeRunnerError, match="refusing to overwrite"):
        runner.run_acquisition(_spec(_free_port()), evidence)
    assert (evidence / "result.json").read_text(encoding="utf-8") == "historic\n"
