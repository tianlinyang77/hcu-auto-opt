from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hcuopt.evaluation.endpoint_workload import load_endpoint_workload_spec

ROOT = Path(__file__).parents[2]
WORKLOAD = ROOT / "config" / "workloads" / "bw20-sglang-endpoint-provisional-v1.yaml"


def test_bw20_provisional_endpoint_workload_is_byte_pinned() -> None:
    workload = load_endpoint_workload_spec(WORKLOAD)

    assert workload.workload_hash == "sha256:" + hashlib.sha256(WORKLOAD.read_bytes()).hexdigest()
    assert workload.prompt_sha256 == "sha256:" + hashlib.sha256(
        workload.prompt.encode("utf-8")
    ).hexdigest()
    assert workload.tensor_parallel_size == workload.closed_loop_concurrency == 1
    assert workload.expected_prompt_tokens == 5
    assert workload.expected_completion_tokens == 8
    assert workload.stream is False


def test_loader_rejects_self_asserted_hashes(tmp_path: Path) -> None:
    text = WORKLOAD.read_text(encoding="utf-8")
    path = tmp_path / "workload.yaml"
    path.write_text(text + "workload_hash: sha256:" + "0" * 64 + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="derived"):
        load_endpoint_workload_spec(path)
