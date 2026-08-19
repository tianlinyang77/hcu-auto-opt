from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.real_profile import build_nmz36_framework_smoke_registry
from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
from hcuopt.contracts.v1 import (
    NoopBuildResult,
    PairedFrameworkSmokeResult,
    SourcePreparationResult,
)
from hcuopt.evaluation.sglang_smoke import verify_evidence_manifest
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target
from hcuopt.workers.handlers import JobHandlers

RUN_TARGET_LOCK = os.environ.get("HCUOPT_RUN_TARGET_LOCK") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = PROJECT_ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"
FORBIDDEN_PERFORMANCE_FIELDS = {
    "speedup_ratio",
    "e2e_speedup_ratio",
    "latency",
    "throughput",
    "ci_low",
    "ci_high",
}


@pytest.mark.target_lock
@pytest.mark.skipif(not RUN_TARGET_LOCK, reason="requires explicit Target Lock execution")
def test_real_f1d_sglang_baseline_noop_equivalence() -> None:
    target = load_target(TARGET_PATH)
    assert socket.gethostname() == target.execution_host.name
    output_dir = Path(os.environ["HCUOPT_F1D_OUTPUT_DIR"]).resolve()
    work_root = Path(target.execution_host.work_root).resolve()
    assert output_dir.is_relative_to(work_root)
    assert not output_dir.is_relative_to(
        Path(target.execution_host.prohibited_work_root).resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _assert_hcu_idle(target.execution_host.accelerator.device_index)

    registry = build_nmz36_framework_smoke_registry(target, output_dir)
    handlers = JobHandlers(registry, output_dir)
    source = SourcePreparationResult.model_validate(
        handlers.handle_source_prepare({"target": target.model_dump(mode="json")})
    )
    candidate_id = uuid4()
    build = NoopBuildResult.model_validate(
        handlers.handle_noop_build(
            {
                "candidate_id": str(candidate_id),
                "baseline_source": source.source.model_dump(mode="json"),
            }
        )
    )
    task_id = uuid4()
    evaluation_run_id = uuid4()
    fencing_token = int(time.time())
    resource_id = "hcu-7"

    try:
        raw_result = handlers.handle_framework_smoke(
            {
                "task_id": str(task_id),
                "candidate_id": str(candidate_id),
                "round_id": str(uuid4()),
                "baseline_epoch_id": str(uuid4()),
                "target": target.model_dump(mode="json"),
                "target_fingerprint": _target_fingerprint(target),
                "artifact": build.artifact.model_dump(mode="json"),
                "evaluation_run_id": str(evaluation_run_id),
                "baseline_execution_request_id": str(uuid4()),
                "noop_execution_request_id": str(uuid4()),
                "evidence_id": str(uuid4()),
                "retest_ordinal": 0,
                "_job_context": {
                    "attempt_number": 1,
                    "resource_id": resource_id,
                    "fencing_token": fencing_token,
                },
            }
        )
    finally:
        final_cleanup = registry.require("resource_cleaner").cleanup(
            resource_id, fencing_token
        )

    result = PairedFrameworkSmokeResult.model_validate(raw_result)
    assert result.synthetic is False
    assert result.evaluation.passed is True
    assert result.evaluation.measurement is None
    assert [item.variant for item in result.executions] == ["baseline", "noop"]
    assert len({item.execution_request.request_id for item in result.executions}) == 2
    assert len({item.execution_attempt.execution_attempt_id for item in result.executions}) == 2
    assert all(item.execution_result.status == "succeeded" for item in result.executions)
    assert all(
        cleanup_is_healthy(item.cleanup_evidence) for item in result.executions
    )
    assert cleanup_is_healthy(final_cleanup)
    assert all(
        item.implementation_kind == "real" for item in result.adapter_provenance
    )
    assert result.evidence.summary["performance_conclusion"] == "not_measured"
    assert not _find_forbidden_performance_fields(result.model_dump(mode="json"))

    evidence_root = file_uri_to_path(result.evidence.summary["evidence_root_uri"])
    verify_evidence_manifest(evidence_root)
    assert (evidence_root / "report.md").is_file()
    assert (evidence_root / "baseline" / "server.log").is_file()
    assert (evidence_root / "noop" / "server.log").is_file()
    _assert_hcu_idle(target.execution_host.accelerator.device_index)

    acceptance = {
        "target_id": target.target_id,
        "task_id": str(task_id),
        "evaluation_run_id": str(evaluation_run_id),
        "evidence_root_uri": evidence_root.resolve().as_uri(),
        "evaluation_passed": result.evaluation.passed,
        "synthetic": result.synthetic,
        "execution_request_ids": {
            item.variant: str(item.execution_request.request_id)
            for item in result.executions
        },
        "execution_attempt_ids": {
            item.variant: str(item.execution_attempt.execution_attempt_id)
            for item in result.executions
        },
        "final_cleanup": final_cleanup,
        "performance_conclusion": "not_measured",
    }
    (output_dir / "f1d-acceptance.json").write_text(
        json.dumps(acceptance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _target_fingerprint(target: object) -> str:
    payload = target.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _assert_hcu_idle(device_index: int) -> None:
    state = _run(
        (
            "hy-smi",
            "-d",
            str(device_index),
            "--showuse",
            "--showmemuse",
            "--showmeminfo",
            "vram",
        )
    )
    use = _extract_number(state, r"HCU use \(%\):\s*([0-9.]+)")
    memory_percent = _extract_number(state, r"HCU memory use \(%\):\s*([0-9.]+)")
    memory_mib = _extract_number(state, r"vram Total Used Memory \(MiB\):\s*([0-9.]+)")
    assert use == 0.0, f"HCU {device_index} is busy: {use}%"
    assert memory_percent == 0.0, (
        f"HCU {device_index} memory is in use: {memory_percent}%"
    )
    assert memory_mib <= 16.0, f"HCU {device_index} retains {memory_mib} MiB"

    managed = _run(
        (
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=io.hcuopt.managed=true",
            "--filter",
            f"label=io.hcuopt.resource-id=hcu-{device_index}",
        )
    )
    assert not managed.strip(), f"managed HCU container remains: {managed.strip()}"


def _run(argv: tuple[str, ...]) -> str:
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return completed.stdout


def _extract_number(value: str, pattern: str) -> float:
    match = re.search(pattern, value)
    assert match is not None, f"missing HCU state field matching {pattern!r}"
    return float(match.group(1))


def _find_forbidden_performance_fields(value: object) -> set[str]:
    if isinstance(value, dict):
        found = FORBIDDEN_PERFORMANCE_FIELDS.intersection(value)
        for nested in value.values():
            found.update(_find_forbidden_performance_fields(nested))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for nested in value:
            found.update(_find_forbidden_performance_fields(nested))
        return found
    return set()
