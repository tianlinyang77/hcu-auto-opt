# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from hcuopt.adapters.agent_generator import (
    AgentProposalMaterializer,
    CandidateProposalBatchStore,
    ProposalPatchStore,
)
from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
from hcuopt.adapters.agent_runner import (
    AgentDeploymentCredential,
    LocalCommandAgentRunner,
)
from hcuopt.adapters.agent_runner_receipt import RunnerExecutionReceiptStore
from hcuopt.adapters.anthropic_agent_provider import (
    AnthropicMessagesGenerationWorker,
    AnthropicMessagesProviderConfig,
)
from hcuopt.agent.anthropic_messages_provider import PROPOSAL_OUTPUT_SCHEMA
from hcuopt.agent.authority import (
    build_generation_run_start,
    generation_plan_id_for,
    generation_run_id_for,
)
from hcuopt.agent.identity import candidate_generation_request_hash, knowledge_snapshot_hash
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    GenerationAttemptClaim,
    GenerationBudget,
    GenerationRun,
    GenerationRunStartRequest,
    GeneratorAttempt,
    GeneratorPlanEntry,
    KnowledgeSnapshot,
)
from hcuopt.domain.errors import SourceArtifactError

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
SECRET = b"test-deployment-provider-key"
PROFILE = "deepseek-anthropic-provider-v1"
PROFILER_EVIDENCE = b'{"hotspot":"sglang.runtime.operator.forward"}'
SOURCE_PATH = "sglang/runtime/operator.py"
SOURCE = b"def forward(value):\n    return value\n"


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _proposal_output(*, count: int = 1, touched_path: str = SOURCE_PATH) -> dict[str, Any]:
    return {
        "schema_version": PROPOSAL_OUTPUT_SCHEMA,
        "proposals": [
            {
                "optimization_intent": f"remove redundant operation {ordinal}",
                "rationale": "The frozen source contains a removable no-op.",
                "risk_summary": "Correctness and performance remain unmeasured.",
                "touched_paths": [touched_path],
                "patch": (
                    f"diff --git a/{SOURCE_PATH} b/{SOURCE_PATH}\n"
                    f"--- a/{SOURCE_PATH}\n"
                    f"+++ b/{SOURCE_PATH}\n"
                    "@@ -1,2 +1,2 @@\n"
                    " def forward(value):\n"
                    "-    return value\n"
                    "+    return value + 0\n"
                ),
            }
            for ordinal in range(count)
        ],
    }


def _anthropic_response(
    output: object,
    *,
    input_tokens: int = 11,
    output_tokens: int = 13,
    include_usage: bool = True,
    stop_reason: str = "end_turn",
) -> bytes:
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(output)}],
        "stop_reason": stop_reason,
    }
    if include_usage:
        payload["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    return json.dumps(payload).encode()


@contextmanager
def _server(
    response_body: bytes,
    *,
    status: int = 200,
    delay_seconds: float = 0.0,
) -> Iterator[tuple[str, dict[str, object]]]:
    observed: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            observed["path"] = self.path
            observed["api_key"] = self.headers.get("x-api-key")
            observed["body"] = self.rfile.read(length)
            if delay_seconds:
                time.sleep(delay_seconds)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            try:
                self.wfile.write(response_body)
            except OSError:
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/messages", observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _knowledge(root: Path) -> tuple[KnowledgeSnapshotStore, KnowledgeSnapshot]:
    content = b"Prefer minimal single-file startup overlay patches."
    snapshot = KnowledgeSnapshot(
        snapshot_id=UUID("74000000-0000-0000-0000-000000000001"),
        sources=(
            {
                "knowledge_id": "skill/provider-test",
                "source_kind": "skill",
                "version": "1.0.0",
                "source_uri": "skill:///provider-test/SKILL.md",
                "content_hash": _hash(content),
                "license_id": "MulanPSL-2.0",
            },
        ),
        created_by="provider-test",
        created_at=NOW,
    )
    store = KnowledgeSnapshotStore(root, profile="provider-test-knowledge-v1")
    store.publish(snapshot, {("skill/provider-test", "1.0.0"): content})
    return store, snapshot


def _claim(
    snapshot: KnowledgeSnapshot,
    *,
    artifact_hash: str,
    max_proposals: int = 2,
    max_output_bytes: int = 128 * 1024,
) -> GenerationAttemptClaim:
    run_id = generation_run_id_for("anthropic-provider-unit-test-v1")
    request = CandidateGenerationRequest(
        request_id=UUID("74000000-0000-0000-0000-000000000002"),
        generation_run_id=run_id,
        target_snapshot_id=UUID("74000000-0000-0000-0000-000000000003"),
        stage0_run_id=UUID("74000000-0000-0000-0000-000000000004"),
        baseline_epoch_id=UUID("74000000-0000-0000-0000-000000000005"),
        baseline_source_hash=_hash(b"baseline"),
        hotspot_id=UUID("74000000-0000-0000-0000-000000000006"),
        replacement_point="sglang.runtime.operator.forward",
        workload_id="anthropic-provider-test",
        workload_hash=_hash(b"workload"),
        configuration_hash=_hash(b"configuration"),
        image_digest=_hash(b"image"),
        profiler_evidence_uri="evidence:///profile.json",
        profiler_evidence_hash=_hash(PROFILER_EVIDENCE),
        knowledge_snapshot_id=snapshot.snapshot_id,
        knowledge_snapshot_hash=knowledge_snapshot_hash(snapshot),
        max_proposals=max_proposals,
    )
    generator = GeneratorPlanEntry(
        generator_id="deepseek-flash-provider",
        adapter_profile=PROFILE,
        generator_artifact_hash=artifact_hash,
        max_attempts=1,
        max_proposals=max_proposals,
        timeout_seconds=10,
        max_output_bytes_per_attempt=max_output_bytes,
        max_tokens_per_attempt=1_000,
    )
    plan = ApexGenerationPlan(
        plan_id=generation_plan_id_for(run_id),
        generation_run_id=run_id,
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generators=(generator,),
        max_concurrency=1,
        budget=GenerationBudget(
            max_generator_attempts=1,
            max_wall_seconds=10,
            max_total_output_bytes=max_output_bytes,
            max_total_tokens=1_000,
            max_proposals=max_proposals,
        ),
        created_by="provider-test",
        created_at=NOW,
    )
    start = GenerationRunStartRequest(
        request=request,
        plan=plan,
        actor="provider-test",
        idempotency_key="anthropic-provider-unit-test-v1",
    )
    run, attempts, _ledger = build_generation_run_start(start, created_at=NOW)
    running_run = GenerationRun.model_validate(
        {**run.model_dump(mode="json"), "state": "running"}
    )
    running_attempt = GeneratorAttempt.model_validate(
        {
            **attempts[0].model_dump(mode="json"),
            "state": "running",
            "worker_id": "provider-test-worker",
            "claim_token": "74000000-0000-0000-0000-000000000007",
            "lease_expires_at": (NOW + timedelta(seconds=30)).isoformat(),
            "started_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )
    return GenerationAttemptClaim(
        run=running_run,
        attempt=running_attempt,
        generator=generator,
        request=request,
    )


def _worker(
    tmp_path: Path,
    endpoint: str,
    *,
    max_response_bytes: int = 128 * 1024,
    max_proposals: int = 2,
    max_output_bytes: int = 128 * 1024,
    timeout_seconds: float = 5,
) -> tuple[AnthropicMessagesGenerationWorker, GenerationAttemptClaim]:
    knowledge_store, snapshot = _knowledge(tmp_path / "knowledge")
    patch_store = ProposalPatchStore(tmp_path / "proposals", profile="provider-c-v1")
    batch_store = CandidateProposalBatchStore(
        tmp_path / "proposals", profile="provider-c-v1"
    )
    materializer = AgentProposalMaterializer(
        profile=PROFILE,
        patch_store=patch_store,
        batch_store=batch_store,
        clock=lambda: NOW,
    )
    provider_artifact = (
        Path(__file__).parents[2]
        / "src"
        / "hcuopt"
        / "agent"
        / "anthropic_messages_provider.py"
    ).resolve()
    runner = LocalCommandAgentRunner(
        allowed_executables=(Path(sys.executable),),
        allowed_argv_prefixes=((str(provider_artifact),),),
        deployment_credentials=(
            AgentDeploymentCredential(
                environment_name="HCUOPT_DEPLOYMENT_PROVIDER_API_KEY_FILE",
                content=SECRET,
            ),
        ),
        forbidden_host_paths=(),
        poll_interval_seconds=0.01,
    )
    worker = AnthropicMessagesGenerationWorker(
        config=AnthropicMessagesProviderConfig(
            endpoint=endpoint,
            model="deepseek-flash",
            max_tokens=1_000,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            allow_insecure_http=True,
        ),
        runner=runner,
        receipt_store=RunnerExecutionReceiptStore(tmp_path / "receipts"),
        knowledge_store=knowledge_store,
        materializer=materializer,
        provider_artifact=provider_artifact,
    )
    return worker, _claim(
        snapshot,
        artifact_hash=worker.generator_artifact_hash,
        max_proposals=max_proposals,
        max_output_bytes=max_output_bytes,
    )


@pytest.mark.parametrize("proposal_count", [1, 2])
def test_real_provider_materializes_bounded_replayable_proposals(
    tmp_path: Path,
    proposal_count: int,
) -> None:
    with _server(_anthropic_response(_proposal_output(count=proposal_count))) as (
        endpoint,
        observed,
    ):
        worker, claim = _worker(tmp_path, endpoint)
        result = worker.run_claim(
            claim,
            source_files={SOURCE_PATH: SOURCE},
            profiler_evidence=PROFILER_EVIDENCE,
            runner_output_dir=tmp_path / "runner",
        )

    assert result.failure_code is None
    assert result.batch is not None
    assert result.batch_uri is not None
    assert len(result.batch.proposals) == proposal_count
    assert result.batch.token_count == 24
    assert result.batch.synthetic is False
    assert result.batch.performance_conclusion == "not_measured"
    assert result.batch.automatic_release_allowed is False
    assert observed["api_key"] == SECRET.decode()
    assert SECRET not in bytes(observed["body"])
    receipt = worker.receipt_store.load(result.receipt_ref)
    assert SECRET.decode() not in repr(receipt)
    assert receipt.raw_output_hash == result.batch.raw_output_hash


@pytest.mark.parametrize(
    ("response_body", "expected_status"),
    [
        (_anthropic_response("not-json"), "failed"),
        (_anthropic_response(_proposal_output(), include_usage=False), "failed"),
        (_anthropic_response(_proposal_output(), stop_reason="max_tokens"), "failed"),
        (b'{"error":"unavailable"}', "failed"),
    ],
)
def test_invalid_provider_results_fail_closed_before_batch(
    tmp_path: Path,
    response_body: bytes,
    expected_status: str,
) -> None:
    status = 503 if response_body == b'{"error":"unavailable"}' else 200
    with _server(response_body, status=status) as (endpoint, _observed):
        worker, claim = _worker(tmp_path, endpoint)
        result = worker.run_claim(
            claim,
            source_files={SOURCE_PATH: SOURCE},
            profiler_evidence=PROFILER_EVIDENCE,
            runner_output_dir=tmp_path / "runner",
        )

    assert result.failure_code == expected_status
    assert result.batch is None
    receipt = worker.receipt_store.load(result.receipt_ref)
    assert receipt.raw_output_uri is None
    assert SECRET.decode() not in repr(receipt)


def test_oversized_provider_response_fails_closed(tmp_path: Path) -> None:
    response = _anthropic_response(_proposal_output()) + b" " * 2_048
    with _server(response) as (endpoint, _observed):
        worker, claim = _worker(tmp_path, endpoint, max_response_bytes=1_024)
        result = worker.run_claim(
            claim,
            source_files={SOURCE_PATH: SOURCE},
            profiler_evidence=PROFILER_EVIDENCE,
            runner_output_dir=tmp_path / "runner",
        )

    assert result.failure_code == "failed"
    assert result.batch is None


def test_provider_timeout_fails_closed(tmp_path: Path) -> None:
    with _server(
        _anthropic_response(_proposal_output()), delay_seconds=0.5
    ) as (endpoint, _observed):
        worker, claim = _worker(tmp_path, endpoint, timeout_seconds=0.1)
        result = worker.run_claim(
            claim,
            source_files={SOURCE_PATH: SOURCE},
            profiler_evidence=PROFILER_EVIDENCE,
            runner_output_dir=tmp_path / "runner",
        )

    assert result.failure_code == "failed"
    assert result.batch is None


def test_unsafe_patch_path_is_rejected_by_c_materialization(tmp_path: Path) -> None:
    response = _anthropic_response(_proposal_output(touched_path="../escape.py"))
    with _server(response) as (endpoint, _observed):
        worker, claim = _worker(tmp_path, endpoint)
        result = worker.run_claim(
            claim,
            source_files={SOURCE_PATH: SOURCE},
            profiler_evidence=PROFILER_EVIDENCE,
            runner_output_dir=tmp_path / "runner",
        )

    assert result.failure_code == "failed"
    assert result.batch is None


def test_profiler_hash_and_provider_artifact_are_bound_before_execution(
    tmp_path: Path,
) -> None:
    with _server(_anthropic_response(_proposal_output())) as (endpoint, observed):
        worker, claim = _worker(tmp_path, endpoint)
        with pytest.raises(SourceArtifactError, match="Profiler Evidence"):
            worker.run_claim(
                claim,
                source_files={SOURCE_PATH: SOURCE},
                profiler_evidence=b"drifted",
                runner_output_dir=tmp_path / "runner",
            )
        drifted = claim.model_copy(
            update={
                "generator": claim.generator.model_copy(
                    update={"generator_artifact_hash": _hash(b"different")}
                )
            }
        )
        with pytest.raises(SourceArtifactError, match="deployed provider Artifact"):
            worker.run_claim(
                drifted,
                source_files={SOURCE_PATH: SOURCE},
                profiler_evidence=PROFILER_EVIDENCE,
                runner_output_dir=tmp_path / "runner",
            )

    assert observed == {}
