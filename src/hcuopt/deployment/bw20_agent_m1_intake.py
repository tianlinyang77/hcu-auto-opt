# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bootstrap the BW20 Agent M1 authority without creating a Candidate or using HCU."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
from hcuopt.adapters.agent_promotion import BaselineOverlaySource
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.messages_generator import (
    MessagesSettings,
    messages_profile_for_input,
    prepare_messages_input,
)
from hcuopt.adapters.profiles import (
    BW20_MANUAL_CANDIDATE_PROFILE,
    real_manual_candidate_profile,
)
from hcuopt.agent.authority import (
    ApexGenerationCoordinator,
    generation_plan_id_for,
    generation_run_id_for,
)
from hcuopt.agent.identity import (
    candidate_generation_request_hash,
    knowledge_snapshot_hash,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    GenerationRunStartRequest,
    KnowledgeSnapshot,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance, SourceSnapshot, TargetSpec
from hcuopt.contracts.v1 import (
    ManualCandidateTaskCreate,
    ManualHotspotIntakeCreate,
)
from hcuopt.deployment.nmz36_m1_allocator import (
    ALLOCATOR_RELATIVE_PATH,
    ALLOCATOR_REPLACEMENT_POINT,
    build_m1_allocator_hotspot_spec,
)
from hcuopt.domain.enums import ManualCandidateKind
from hcuopt.evaluation.m1_protocol import m1_hotspot_spec_sha256
from hcuopt.generators import anthropic_messages
from hcuopt.measurement.evidence import (
    canonical_json_bytes,
    write_evidence_bytes,
)
from hcuopt.source_hash import file_uri_to_path
from hcuopt.storage.repository import PostgresRepository

STAGE0_RUN_ID = UUID("dc883c86-9aa8-5550-a8ea-0094a9866519")
STAGE0_TASK_ID = UUID("1fcb76b1-33f2-5408-adf2-f951e9a885cd")
TARGET_SNAPSHOT_ID = UUID("a8c8a876-5e0a-5dee-b759-de1b05054c63")
PROFILE = BW20_MANUAL_CANDIDATE_PROFILE
WORKLOAD_ID = "bw20-m1-allocator-free-page-contiguous-v1"
TASK_KEY = "bw20-m1-agent-allocator-free-v1"
HOTSPOT_KEY = "bw20-m1-agent-allocator-free-hotspot-v1"
GENERATION_KEY = "bw20-m1-agent-allocator-free-generation-v1"
CREATED_AT = datetime(2026, 9, 15, tzinfo=timezone.utc)
DEFAULT_HOTSPOT_SUMMARY = (
    "Review one source-only optimization for PagedTokenToKVPoolAllocator.free. "
    "The only accepted business scope is the frozen page-contiguous input family. "
    "Historical nmz36 profiling is a transferred hypothesis; do not claim BW20 share, "
    "correctness, speedup, or general safety. Return one bounded single-file proposal."
)
HISTORICAL_PROFILER_URI = (
    "file:///home/github/hcu-auto-opt-m1-runs/20260825-prefill-v7-hcu7-uncached/"
    "traces/1787646066.6493704/"
    "m1-prefill-v2-1787646066.6515782-TP-0.trace.json.gz"
)
HISTORICAL_PROFILER_HASH = "sha256:17ff2c0aa23a5365ab7478f0b1cb0756d11509c135b5cf2ddb48baa20b77392a"


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _content_path(root: Path, kind: str, digest: str) -> Path:
    value = digest.removeprefix("sha256:")
    return root / kind / "sha256" / value[:2] / value[2:] / "evidence.json"


def _publish(root: Path, kind: str, value: Any):
    encoded = canonical_json_bytes(value)
    return write_evidence_bytes(_content_path(root, kind, _sha256(encoded)), encoded)


def _require_stage0(repository: Any) -> tuple[dict[str, Any], TargetSpec]:
    stage0 = repository.stage0_run_summary(STAGE0_RUN_ID)
    run = stage0.get("run") or {}
    task = stage0.get("task") or {}
    target = TargetSpec.model_validate(stage0["target"])
    if (
        UUID(str(run.get("stage0_run_id"))) != STAGE0_RUN_ID
        or UUID(str(run.get("target_snapshot_id"))) != TARGET_SNAPSHOT_ID
        or UUID(str(task.get("task_id"))) != STAGE0_TASK_ID
        or run.get("mode") != "formal"
        or run.get("state") != "finalized"
        or task.get("stage0_authority") != "formal"
        or task.get("project_mode") != "degraded_manual_intake"
        or task.get("automatic_release_allowed") is not False
    ):
        raise RuntimeError("BW20 Agent M1 requires the frozen finalized Stage 0 v4 authority")
    real_manual_candidate_profile(PROFILE).validate_target(
        target,
        scope="optimization",
        evidence_resolved_blockers=frozenset({"stage0_not_measured"}),
    )
    return stage0, target


def _register_baseline(repository: Any, snapshot_file: Path) -> SourceSnapshot:
    snapshot = SourceSnapshot.model_validate_json(snapshot_file.read_bytes())
    manager = GitSourceManager(PROFILE)
    manager._assert_snapshot_unchanged(  # noqa: SLF001 - deployment revalidation boundary
        snapshot,
        file_uri_to_path(snapshot.worktree_uri),
    )
    if snapshot.kind != "baseline" or not snapshot.clean:
        raise RuntimeError("BW20 Agent M1 requires a clean Baseline SourceSnapshot")
    row = repository.record_source_snapshot(
        STAGE0_TASK_ID,
        snapshot,
        [manager.provenance.model_dump(mode="json")],
        False,
        f"stage0:{STAGE0_RUN_ID}:bw20-agent-m1-baseline-source:v1",
    )
    if UUID(str(row["snapshot_id"])) != snapshot.snapshot_id:
        raise RuntimeError("persisted BW20 Baseline SourceSnapshot identity drifted")
    return snapshot


def _knowledge(
    source_root: Path,
    generation_root: Path,
    *,
    generation_key: str,
    created_at: datetime,
) -> tuple[KnowledgeSnapshotStore, KnowledgeSnapshot]:
    document = (source_root / "docs" / "m1-hotspot-overlay.md").resolve(strict=True)
    payload = document.read_bytes()
    source_hash = _sha256(payload)
    snapshot = KnowledgeSnapshot(
        snapshot_id=uuid5(NAMESPACE_URL, f"hcuopt:{generation_key}:knowledge:v1"),
        sources=(
            {
                "knowledge_id": "repository/hcu-auto-opt/m1-hotspot-overlay",
                "source_kind": "repository_document",
                "version": "c7f81ac",
                "source_uri": document.as_uri(),
                "content_hash": source_hash,
                "license_id": "MulanPSL-2.0",
            },
        ),
        created_by="bw20-agent-m1-bootstrap",
        created_at=created_at,
    )
    store = KnowledgeSnapshotStore(
        generation_root / "knowledge",
        profile="bw20-agent-m1-knowledge-v1",
    )
    store.publish(
        snapshot,
        {("repository/hcu-auto-opt/m1-hotspot-overlay", "c7f81ac"): payload},
    )
    return store, snapshot


def bootstrap(
    repository: Any,
    *,
    source_root: Path,
    baseline_snapshot_file: Path,
    evidence_root: Path,
    generation_root: Path,
    base_url: str,
    model: str,
    task_key: str = TASK_KEY,
    hotspot_key: str = HOTSPOT_KEY,
    generation_key: str = GENERATION_KEY,
    hotspot_summary: str = DEFAULT_HOTSPOT_SUMMARY,
    created_at: datetime = CREATED_AT,
    coordinator: ApexGenerationCoordinator | None = None,
) -> dict[str, Any]:
    """Create reproducible authority objects through A, stopping before human review."""

    _, target = _require_stage0(repository)
    baseline = _register_baseline(repository, baseline_snapshot_file.resolve(strict=True))
    evidence_root = evidence_root.resolve()
    generation_root = generation_root.resolve()

    hotspot_id = uuid5(NAMESPACE_URL, f"hcuopt:m1-hotspot:{hotspot_key}")
    correctness = build_m1_allocator_hotspot_spec(str(hotspot_id))
    correctness_artifact = _publish(evidence_root, "correctness-spec", correctness)
    if correctness_artifact.sha256 != m1_hotspot_spec_sha256(correctness):
        raise RuntimeError("BW20 M1 correctness specification Hash drifted")
    workload = {
        "schema_version": "bw20-m1-allocator-workload-v1",
        "workload_id": WORKLOAD_ID,
        "target_id": target.target_id,
        "operation": ALLOCATOR_REPLACEMENT_POINT,
        "scope": "single-request-page-contiguous-allocator-microbenchmark",
        "page_size": 64,
        "correctness_spec_hash": correctness_artifact.sha256,
        "bw20_profiler_share": "not_measured",
        "historical_profiler_role": "transferred_hypothesis_only",
        "performance_conclusion": "not_measured",
    }
    workload_artifact = _publish(evidence_root, "workload", workload)
    configuration = {
        "schema_version": "bw20-m1-allocator-configuration-v1",
        "target_snapshot_id": str(TARGET_SNAPSHOT_ID),
        "image_digest": target.inference_image.registry_digest,
        "physical_device_index": 7,
        "logical_device_index": 0,
        "clock_policy": "host_auto_observe_only_v1",
        "measurement_harness": "M1TrustedMeasurementHarness-v1",
        "automatic_release_allowed": False,
    }
    configuration_artifact = _publish(evidence_root, "configuration", configuration)

    task = repository.create_manual_candidate_task(
        ManualCandidateTaskCreate(
            name="BW20 M1 Agent allocator-free startup-overlay Candidate",
            stage0_run_id=STAGE0_RUN_ID,
            adapter_profile=PROFILE,
            baseline_source_snapshot_id=baseline.snapshot_id,
            workload_id=WORKLOAD_ID,
            workload_hash=workload_artifact.sha256,
            configuration_hash=configuration_artifact.sha256,
            idempotency_key=task_key,
            budget={"max_wall_seconds": 7200, "max_samples": 400},
        )
    )
    task_id = UUID(str(task["task_id"]))
    summary = repository.manual_candidate_summary(task_id)
    baseline_epoch_id = UUID(str(summary["baseline"]["baseline_epoch_id"]))

    profiler = AdapterProvenance(
        profile=PROFILE,
        capability="profiler",
        adapter_name="transferred-nmz36-torch-profiler-hypothesis",
        adapter_version="2026-08-25",
        implementation_kind="real",
    )
    hotspot = repository.create_manual_hotspot_intake(
        task_id,
        ManualHotspotIntakeCreate(
            baseline_epoch_id=baseline_epoch_id,
            symbol="allocator.py PagedTokenToKVPoolAllocator.free / torch.unique",
            operation_name="page reclamation in allocator free",
            shape=[4090],
            dtype="int64",
            meta={
                "stage": "allocator_microbenchmark",
                "page_size": 64,
                "historical_target": "nmz36",
                "historical_evidence_role": "transferred_hypothesis_only",
                "bw20_share_ratio": "not_measured",
                "bw20_opportunity_score": "not_measured",
                "workload_uri": workload_artifact.uri,
                "workload_hash": workload_artifact.sha256,
                "configuration_uri": configuration_artifact.uri,
                "configuration_hash": configuration_artifact.sha256,
                "formal_performance_conclusion": None,
            },
            implementation_location=(
                "python/sglang/srt/mem_cache/allocator.py:PagedTokenToKVPoolAllocator.free"
            ),
            replacement_point=ALLOCATOR_REPLACEMENT_POINT,
            call_path=(
                "historical nmz36 SGLang prefill trace",
                "PagedTokenToKVPoolAllocator.free",
                "torch.unique",
                "aten::_unique2",
            ),
            profiler_raw_output_uri=HISTORICAL_PROFILER_URI,
            profiler_raw_output_hash=HISTORICAL_PROFILER_HASH,
            correctness_spec_uri=correctness_artifact.uri,
            correctness_spec_hash=correctness_artifact.sha256,
            share_ratio=0.0,
            opportunity_score=0.0,
            upstream_dedup_status="unknown",
            upstream_reference=None,
            patchability="patchable",
            selection_reason=(
                "Transferred nmz36 evidence identifies a bounded source-level hypothesis. "
                "No BW20 endpoint share or opportunity is claimed; the frozen page-contiguous "
                "correctness contract and BW20 measurement must independently accept or reject it."
            ),
            candidate_kind=ManualCandidateKind.BUSINESS,
            actor="bw20-agent-m1-bootstrap",
            adapter_provenance=[profiler],
            synthetic=False,
            idempotency_key=hotspot_key,
        ),
    )
    if UUID(str(hotspot["hotspot_id"])) != hotspot_id:
        raise RuntimeError("BW20 M1 Hotspot identity drifted")

    knowledge_store, knowledge = _knowledge(
        source_root.resolve(strict=True),
        generation_root,
        generation_key=generation_key,
        created_at=created_at,
    )
    generation_run_id = generation_run_id_for(generation_key)
    request = CandidateGenerationRequest(
        request_id=uuid5(generation_run_id, "hcuopt:bw20-agent-m1-request:v1"),
        generation_run_id=generation_run_id,
        target_snapshot_id=TARGET_SNAPSHOT_ID,
        stage0_run_id=STAGE0_RUN_ID,
        baseline_epoch_id=baseline_epoch_id,
        baseline_source_hash=baseline.source_hash,
        hotspot_id=hotspot_id,
        replacement_point=ALLOCATOR_REPLACEMENT_POINT,
        workload_id=WORKLOAD_ID,
        workload_hash=workload_artifact.sha256,
        configuration_hash=configuration_artifact.sha256,
        image_digest=target.inference_image.registry_digest,
        profiler_evidence_uri=HISTORICAL_PROFILER_URI,
        profiler_evidence_hash=HISTORICAL_PROFILER_HASH,
        knowledge_snapshot_id=knowledge.snapshot_id,
        knowledge_snapshot_hash=knowledge_snapshot_hash(knowledge),
        max_proposals=1,
    )
    settings = MessagesSettings(
        base_url=base_url,
        model=model,
        max_output_tokens=4096,
        timeout_seconds=180,
    )
    prepared_input = prepare_messages_input(
        request,
        baseline=BaselineOverlaySource(snapshot=baseline, path=ALLOCATOR_RELATIVE_PATH),
        knowledge_store=knowledge_store,
        settings=settings,
        hotspot_summary=hotspot_summary,
    )
    input_artifact = write_evidence_bytes(
        generation_root / "inputs" / str(generation_run_id) / "input.json",
        prepared_input.content,
    )
    generator_artifact = Path(anthropic_messages.__file__).resolve(strict=True)
    request_hash = candidate_generation_request_hash(request)
    plan = ApexGenerationPlan(
        plan_id=generation_plan_id_for(generation_run_id),
        generation_run_id=generation_run_id,
        request_id=request.request_id,
        request_hash=request_hash,
        generators=(
            {
                "generator_id": "deepseek-messages-agent",
                "adapter_profile": messages_profile_for_input(prepared_input),
                "generator_artifact_hash": _sha256(generator_artifact.read_bytes()),
                "max_attempts": 1,
                "max_proposals": 1,
                "timeout_seconds": 210,
                "max_output_bytes_per_attempt": 1_000_000,
                "max_tokens_per_attempt": 45_000,
            },
        ),
        max_concurrency=1,
        budget={
            "max_generator_attempts": 1,
            "max_wall_seconds": 210,
            "max_total_output_bytes": 1_000_000,
            "max_total_tokens": 45_000,
            "max_proposals": 1,
        },
        created_by="bw20-agent-m1-bootstrap",
        created_at=created_at,
    )
    start_request = GenerationRunStartRequest(
        request=request,
        plan=plan,
        actor="bw20-agent-m1-bootstrap",
        idempotency_key=generation_key,
    )
    start_artifact = _publish(generation_root, "start-request", start_request)
    started = (coordinator or ApexGenerationCoordinator()).start(start_request, repository)
    return {
        "schema_version": "bw20-agent-m1-bootstrap-result-v1",
        "task_id": str(task_id),
        "baseline_epoch_id": str(baseline_epoch_id),
        "baseline_source_snapshot_id": str(baseline.snapshot_id),
        "hotspot_id": str(hotspot_id),
        "generation_run_id": str(generation_run_id),
        "generation_state": started.state,
        "input_uri": input_artifact.uri,
        "input_hash": input_artifact.sha256,
        "start_request_uri": start_artifact.uri,
        "start_request_hash": start_artifact.sha256,
        "profiler_evidence_role": "transferred_hypothesis_only",
        "bw20_profiler_share": "not_measured",
        "candidate_created": False,
        "hcu_accessed": False,
        "performance_conclusion": "not_measured",
        "automatic_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--baseline-snapshot", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--generation-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--task-key", default=TASK_KEY)
    parser.add_argument("--hotspot-key", default=HOTSPOT_KEY)
    parser.add_argument("--generation-key", default=GENERATION_KEY)
    parser.add_argument("--hotspot-summary-file", type=Path)
    parser.add_argument(
        "--created-at",
        default=CREATED_AT.isoformat(),
        help="fixed timezone-aware ISO-8601 timestamp used for idempotent replay",
    )
    args = parser.parse_args(argv)
    database_url = os.getenv("HCUOPT_DATABASE_URL")
    if not database_url:
        parser.error("HCUOPT_DATABASE_URL is required")
    created_at = datetime.fromisoformat(args.created_at.replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        parser.error("--created-at must be timezone-aware")
    hotspot_summary = DEFAULT_HOTSPOT_SUMMARY
    if args.hotspot_summary_file is not None:
        hotspot_summary = args.hotspot_summary_file.read_text(encoding="utf-8").strip()
    result = bootstrap(
        PostgresRepository(database_url),
        source_root=args.source_root,
        baseline_snapshot_file=args.baseline_snapshot,
        evidence_root=args.evidence_root,
        generation_root=args.generation_root,
        base_url=args.base_url,
        model=args.model,
        task_key=args.task_key,
        hotspot_key=args.hotspot_key,
        generation_key=args.generation_key,
        hotspot_summary=hotspot_summary,
        created_at=created_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
