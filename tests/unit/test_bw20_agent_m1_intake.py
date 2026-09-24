# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.deployment.bw20_agent_m1_intake import (
    FROZEN_CASE_SEED,
    FROZEN_TARGET_CASE_IDS,
    HOTSPOT_KEY,
    STAGE0_RUN_ID,
    STAGE0_TASK_ID,
    TARGET_SNAPSHOT_ID,
    _grounded_hotspot_summary,
    bootstrap,
)
from hcuopt.deployment.nmz36_m1_allocator import build_m1_allocator_hotspot_spec
from hcuopt.measurement.m1_allocator_reference import build_case, input_hash
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path
from hcuopt.targets import load_target

ROOT = Path(__file__).resolve().parents[2]


def test_grounded_summary_matches_frozen_target_inputs() -> None:
    spec = build_m1_allocator_hotspot_spec("bw20-test-hotspot")
    expectations = {item.case_id: item.input_hash for item in spec.input_expectations}
    summary = _grounded_hotspot_summary("operator advisory")
    assert summary.startswith("operator advisory\n\n")
    assert "not performance measurements" in summary
    for case_id, length, adjacent_equals in (
        ("target-4090-direct-nosort", 4090, 4026),
        ("target-4091-direct-nosort", 4091, 4027),
    ):
        assert case_id in FROZEN_TARGET_CASE_IDS
        case = build_case(case_id, FROZEN_CASE_SEED, "ordinary")
        assert expectations[case_id] == input_hash(case)
        assert len(case.values) == length
        assert len(set(case.page_ids)) == 64
        assert sum(a == b for a, b in zip(case.page_ids, case.page_ids[1:], strict=False)) == (
            adjacent_equals
        )
        assert f"{case_id}: {length} token indices, 64 unique pages" in summary
        assert f"{adjacent_equals} adjacent equal page-id pairs" in summary
    assert "cannot help either target case" in summary


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


class Repository:
    def __init__(self, source: SourceSnapshot) -> None:
        self.source = source
        self.hotspot = None
        self.start = None

    def stage0_run_summary(self, _run_id):
        target = load_target(ROOT / "config/targets/bw20-sglang-0.5.12.yaml")
        return {
            "run": {
                "stage0_run_id": STAGE0_RUN_ID,
                "target_snapshot_id": TARGET_SNAPSHOT_ID,
                "mode": "formal",
                "state": "finalized",
            },
            "task": {
                "task_id": STAGE0_TASK_ID,
                "stage0_authority": "formal",
                "project_mode": "degraded_manual_intake",
                "automatic_release_allowed": False,
            },
            "target": target.model_dump(mode="json"),
        }

    def record_source_snapshot(self, task_id, source, provenance, synthetic, _key):
        assert task_id == STAGE0_TASK_ID
        assert source == self.source
        assert provenance[0]["implementation_kind"] == "real"
        assert synthetic is False
        return {"snapshot_id": source.snapshot_id}

    def create_manual_candidate_task(self, request):
        self.task_request = request
        return {"task_id": uuid5(NAMESPACE_URL, "bw20-agent-m1-test-task")}

    def manual_candidate_summary(self, _task_id):
        return {"baseline": {"baseline_epoch_id": uuid5(NAMESPACE_URL, "baseline")}}

    def create_manual_hotspot_intake(self, _task_id, request):
        self.hotspot = request
        return {"hotspot_id": uuid5(NAMESPACE_URL, f"hcuopt:m1-hotspot:{request.idempotency_key}")}


class Coordinator:
    def start(self, request, repository):
        repository.start = request
        return SimpleNamespace(state="created")


def test_bootstrap_binds_real_authority_but_stops_before_candidate(tmp_path: Path) -> None:
    source = tmp_path / "baseline"
    allocator = source / "python/sglang/srt/mem_cache/allocator.py"
    allocator.parent.mkdir(parents=True)
    allocator.write_text("def free(self):\n    return None\n", encoding="utf-8")
    _git(source, "init")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "add", ".")
    _git(
        source,
        "-c",
        "user.name=HCU Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "test(source): add baseline",
    )
    target_repository = "git@github.com:HYGON-AI/sglang-das.git"
    _git(source, "remote", "add", "origin", target_repository)
    snapshot = SourceSnapshot(
        kind="baseline",
        repository=target_repository,
        commit=_git(source, "rev-parse", "HEAD"),
        tree_hash=_git(source, "rev-parse", "HEAD^{tree}"),
        source_hash=canonical_source_hash(source),
        worktree_uri=source.as_uri(),
        clean=True,
    )
    snapshot_file = tmp_path / "baseline.json"
    snapshot_file.write_text(snapshot.model_dump_json(), encoding="utf-8")
    repository = Repository(snapshot)

    result = bootstrap(
        repository,
        source_root=ROOT,
        baseline_snapshot_file=snapshot_file,
        evidence_root=tmp_path / "evidence",
        generation_root=tmp_path / "generation",
        base_url="https://api.example.invalid/anthropic",
        model="test-model",
        coordinator=Coordinator(),
    )

    assert result["generation_state"] == "created"
    assert result["candidate_created"] is False
    assert result["hcu_accessed"] is False
    assert result["performance_conclusion"] == "not_measured"
    assert repository.hotspot.share_ratio == 0.0
    assert repository.hotspot.meta["historical_evidence_role"] == ("transferred_hypothesis_only")
    assert repository.start.request.hotspot_id == uuid5(
        NAMESPACE_URL, f"hcuopt:m1-hotspot:{HOTSPOT_KEY}"
    )
    input_path = file_uri_to_path(result["input_uri"])
    envelope = json.loads(input_path.read_text(encoding="utf-8"))
    assert envelope["context"]["performance_conclusion"] == "not_measured"
    assert envelope["context"]["formal_intake_allowed"] is False
    assert "4026 adjacent equal page-id pairs" in envelope["context"]["hotspot_summary"]
    assert "4027 adjacent equal page-id pairs" in envelope["context"]["hotspot_summary"]
    prior = next(
        item
        for item in envelope["context"]["knowledge"]
        if item["knowledge_id"] == "repository/hcu-auto-opt/m1-formal-bw20-20260916"
    )
    assert prior["authority"] == "advisory_only"
    assert "torch.unique_consecutive(free_index // self.page_size)" in prior["content"]
    assert "does not authorize automatic or production release" in prior["content"]


def test_bootstrap_creates_independent_generation_for_corrected_advisory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "baseline"
    allocator = source / "python/sglang/srt/mem_cache/allocator.py"
    allocator.parent.mkdir(parents=True)
    allocator.write_text("def free(self):\n    return None\n", encoding="utf-8")
    _git(source, "init")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "add", ".")
    _git(
        source,
        "-c",
        "user.name=HCU Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "test(source): add baseline",
    )
    _git(source, "remote", "add", "origin", "git@github.com:HYGON-AI/sglang-das.git")
    snapshot = SourceSnapshot(
        kind="baseline",
        repository="git@github.com:HYGON-AI/sglang-das.git",
        commit=_git(source, "rev-parse", "HEAD"),
        tree_hash=_git(source, "rev-parse", "HEAD^{tree}"),
        source_hash=canonical_source_hash(source),
        worktree_uri=source.as_uri(),
        clean=True,
    )
    snapshot_file = tmp_path / "baseline.json"
    snapshot_file.write_text(snapshot.model_dump_json(), encoding="utf-8")
    repository = Repository(snapshot)
    generation_key = "bw20-m1-agent-allocator-free-generation-v2"
    advisory = (
        "page-contiguous means all token indices from one page occur in one adjacent group. "
        "The derived page-id sequence contains repeated adjacent values, while the order of "
        "different page groups can be non-monotonic; do not require strict or non-decreasing order."
    )

    result = bootstrap(
        repository,
        source_root=ROOT,
        baseline_snapshot_file=snapshot_file,
        evidence_root=tmp_path / "evidence",
        generation_root=tmp_path / "generation",
        base_url="https://api.example.invalid/anthropic",
        model="test-model",
        generation_key=generation_key,
        hotspot_summary=advisory,
        coordinator=Coordinator(),
    )

    assert result["generation_run_id"] != "061c23b8-8a52-5c89-8afc-f24a3da9b739"
    assert repository.start.idempotency_key == generation_key
    assert repository.start.request.generation_run_id == repository.start.plan.generation_run_id
    envelope = json.loads(file_uri_to_path(result["input_uri"]).read_text(encoding="utf-8"))
    assert envelope["context"]["hotspot_summary"].startswith(advisory + "\n\n")
    assert "repeated adjacent values" in envelope["context"]["hotspot_summary"]
    assert "non-monotonic" in envelope["context"]["hotspot_summary"]
    assert "strictly increasing quotient page IDs cannot help" in envelope["context"][
        "hotspot_summary"
    ]


def test_bootstrap_can_start_a_new_formal_task_after_invalid_infrastructure_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "baseline"
    allocator = source / "python/sglang/srt/mem_cache/allocator.py"
    allocator.parent.mkdir(parents=True)
    allocator.write_text("def free(self):\n    return None\n", encoding="utf-8")
    _git(source, "init")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "add", ".")
    _git(
        source,
        "-c",
        "user.name=HCU Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "test(source): add baseline",
    )
    _git(source, "remote", "add", "origin", "git@github.com:HYGON-AI/sglang-das.git")
    snapshot = SourceSnapshot(
        kind="baseline",
        repository="git@github.com:HYGON-AI/sglang-das.git",
        commit=_git(source, "rev-parse", "HEAD"),
        tree_hash=_git(source, "rev-parse", "HEAD^{tree}"),
        source_hash=canonical_source_hash(source),
        worktree_uri=source.as_uri(),
        clean=True,
    )
    snapshot_file = tmp_path / "baseline.json"
    snapshot_file.write_text(snapshot.model_dump_json(), encoding="utf-8")
    repository = Repository(snapshot)
    task_key = "bw20-m1-agent-allocator-free-v2"
    hotspot_key = "bw20-m1-agent-allocator-free-hotspot-v2"

    result = bootstrap(
        repository,
        source_root=ROOT,
        baseline_snapshot_file=snapshot_file,
        evidence_root=tmp_path / "evidence",
        generation_root=tmp_path / "generation",
        base_url="https://api.example.invalid/anthropic",
        model="test-model",
        task_key=task_key,
        hotspot_key=hotspot_key,
        generation_key="bw20-m1-agent-allocator-free-generation-v7",
        coordinator=Coordinator(),
    )

    assert repository.task_request.idempotency_key == task_key
    assert repository.hotspot.idempotency_key == hotspot_key
    assert repository.start.request.hotspot_id == uuid5(
        NAMESPACE_URL, f"hcuopt:m1-hotspot:{hotspot_key}"
    )
    assert result["candidate_created"] is False
