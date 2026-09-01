# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from hcuopt.adapters.agent_generator import CandidateProposalBatchStore, ProposalPatchStore
from hcuopt.adapters.agent_runner import AgentRunResult
from hcuopt.adapters.agent_runner_receipt import RunnerExecutionReceiptStore
from hcuopt.agent.authority import (
    ApexGenerationCoordinator,
    generation_plan_id_for,
    generation_run_id_for,
)
from hcuopt.agent.identity import (
    apex_generation_plan_hash,
    candidate_generation_request_hash,
    knowledge_snapshot_hash,
)
from hcuopt.contracts.agent_runner_v1 import (
    RunnerExecutionReceiptRef,
    RunnerExecutionRecord,
    RunnerProvenance,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposalBatch,
    GenerationAttemptClaim,
    GenerationRunStartRequest,
    GeneratorAttempt,
    KnowledgeSnapshot,
)
from hcuopt.contracts.agent_verification_v1 import AgentProposalVerificationContext
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.agent_proposal_verifier import AgentProposalVerifier
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.storage.repository import PostgresRepository

try:
    import psycopg
except ImportError:  # pragma: no cover - package dependency in normal installs
    psycopg = None


DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _payload_hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _start_request(
    key: str,
    *,
    max_concurrency: int = 2,
    knowledge_snapshot: KnowledgeSnapshot | None = None,
) -> GenerationRunStartRequest:
    run_id = generation_run_id_for(key)
    request = CandidateGenerationRequest(
        request_id=uuid5(run_id, "request"),
        generation_run_id=run_id,
        target_snapshot_id=UUID("51000000-0000-0000-0000-000000000001"),
        stage0_run_id=UUID("51000000-0000-0000-0000-000000000002"),
        baseline_epoch_id=UUID("51000000-0000-0000-0000-000000000003"),
        baseline_source_hash=_hash("1"),
        hotspot_id=UUID("51000000-0000-0000-0000-000000000004"),
        replacement_point="sglang.runtime.operator.forward",
        workload_id="agent-postgres-fixture",
        workload_hash=_hash("2"),
        configuration_hash=_hash("3"),
        image_digest=_hash("4"),
        profiler_evidence_uri="evidence:///agent/postgres-profile.json",
        profiler_evidence_hash=_hash("5"),
        knowledge_snapshot_id=(
            knowledge_snapshot.snapshot_id
            if knowledge_snapshot is not None
            else UUID("51000000-0000-0000-0000-000000000005")
        ),
        knowledge_snapshot_hash=(
            knowledge_snapshot_hash(knowledge_snapshot)
            if knowledge_snapshot is not None
            else _hash("6")
        ),
        max_proposals=4,
    )
    plan = ApexGenerationPlan(
        plan_id=generation_plan_id_for(run_id),
        generation_run_id=run_id,
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generators=(
            {
                "generator_id": "agent-a",
                "adapter_profile": "m2b-scripted-agent-a-v1",
                "generator_artifact_hash": _hash("a"),
                "max_attempts": 2,
                "max_proposals": 2,
                "timeout_seconds": 10,
                "max_output_bytes_per_attempt": 20_000,
                "max_tokens_per_attempt": 2_000,
            },
            {
                "generator_id": "agent-b",
                "adapter_profile": "m2b-scripted-agent-b-v1",
                "generator_artifact_hash": _hash("b"),
                "max_attempts": 1,
                "max_proposals": 2,
                "timeout_seconds": 10,
                "max_output_bytes_per_attempt": 20_000,
                "max_tokens_per_attempt": 2_000,
            },
        ),
        max_concurrency=max_concurrency,
        budget={
            "max_generator_attempts": 3,
            "max_wall_seconds": 30,
            "max_total_output_bytes": 60_000,
            "max_total_tokens": 6_000,
            "max_proposals": 4,
        },
        created_by="apex-postgres-test",
        created_at=NOW,
    )
    return GenerationRunStartRequest(
        request=request,
        plan=plan,
        actor="agent-postgres-test",
        idempotency_key=key,
    )


def _batch(
    attempt: GeneratorAttempt,
    *,
    request_hash: str,
    normalized_patch_hash: str,
    failed: bool = False,
    output_bytes: int = 1024,
    raw_output_uri: str | None = None,
    raw_output_hash: str | None = None,
    patch_uri: str | None = None,
    patch_hash: str | None = None,
) -> CandidateProposalBatch:
    proposal_id = uuid5(attempt.attempt_id, "proposal-0")
    return CandidateProposalBatch(
        batch_id=uuid5(attempt.attempt_id, "batch"),
        request_id=attempt.request_id,
        request_hash=request_hash,
        generation_run_id=attempt.generation_run_id,
        generator_id=attempt.generator_id,
        adapter_provenance=AdapterProvenance(
            profile=attempt.adapter_profile,
            capability="candidate_proposal_generation",
            adapter_name="DeterministicAgent",
            adapter_version="1.0.0",
            implementation_kind="fake",
        ),
        status="failed" if failed else "succeeded",
        proposals=()
        if failed
        else (
            {
                "proposal_id": proposal_id,
                "request_id": attempt.request_id,
                "request_hash": request_hash,
                "generation_run_id": attempt.generation_run_id,
                "generator_id": attempt.generator_id,
                "ordinal": 0,
                "optimization_intent": "remove one redundant materialization",
                "rationale": "The immutable trace binds the generated proposal.",
                "risk_summary": "Independent correctness review remains mandatory.",
                "patch_uri": patch_uri or f"proposal:///{proposal_id}.diff",
                "patch_hash": patch_hash or _hash("7"),
                "normalized_patch_hash": normalized_patch_hash,
                "touched_paths": ("sglang/runtime/operator.py",),
                "replacement_point": "sglang.runtime.operator.forward",
            },
        ),
        raw_output_uri=raw_output_uri or f"proposal:///{attempt.attempt_id}.json",
        raw_output_hash=raw_output_hash or _hash("8"),
        output_bytes=output_bytes,
        token_count=128,
        attempt_count=attempt.attempt_number,
        wall_seconds=0.25,
        started_at=NOW,
        finished_at=NOW + timedelta(milliseconds=250),
        synthetic=True,
        error_code="generator_process_failed" if failed else None,
        error_message="scripted generator failed safely" if failed else None,
    )


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class AgentGenerationPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute("TRUNCATE agent_generation_runs CASCADE")
        self.connection.commit()
        self.coordinator = ApexGenerationCoordinator(clock=lambda: NOW)
        self.store_directory = tempfile.TemporaryDirectory()
        store_root = Path(self.store_directory.name)
        self.receipt_store = RunnerExecutionReceiptStore(store_root / "runner")
        self.batch_store = CandidateProposalBatchStore(
            store_root / "proposal",
            profile="m2b-postgres-batch-store-v1",
        )
        self.patch_store = ProposalPatchStore(
            store_root / "proposal",
            profile="m2b-postgres-patch-store-v1",
        )
        self.evidence_root = store_root

    def tearDown(self) -> None:
        self.connection.close()
        self.store_directory.cleanup()

    def _settlement(
        self,
        claim: GenerationAttemptClaim,
        *,
        normalized_patch_hash: str,
        request_hash: str | None = None,
        output_bytes: int = 1024,
        raw_patch: bytes | None = None,
    ) -> tuple[CandidateProposalBatch, tuple[RunnerExecutionReceiptRef, str]]:
        attempt = claim.attempt
        run = claim.run
        generator = claim.generator
        bound_request_hash = request_hash or run.request_hash
        raw_output = b"x" * output_bytes
        record = RunnerExecutionRecord(
            attempt_id=attempt.attempt_id,
            generation_run_id=attempt.generation_run_id,
            request_id=attempt.request_id,
            request_hash=bound_request_hash,
            plan_id=attempt.plan_id,
            generator_id=attempt.generator_id,
            attempt_number=attempt.attempt_number,
            runner_provenance=RunnerProvenance(
                profile="m2b-postgres-runner-v1",
                adapter_name="ScriptedRunner",
                adapter_version="1.0.0",
                implementation_kind="fake",
                identity_hash=_hash("d"),
            ),
            generator_artifact_hash=generator.generator_artifact_hash,
            status="succeeded",
            synthetic=True,
            wall_seconds_consumed=0.25,
            stdout_bytes_consumed=len(raw_output),
            stderr_bytes_consumed=0,
            total_output_bytes_consumed=len(raw_output),
            tokens_consumed=128,
            exit_code=0,
            executable_name="scripted-agent",
            argv_hash=_hash("e"),
            input_manifest_hash=_hash("f"),
            stdout_hash=_payload_hash(raw_output),
            stderr_hash=_payload_hash(b""),
            stdout_summary="scripted proposal output",
            stderr_summary="",
            process_tree_cleanup="not_needed",
            cleanup_status="verified",
            cleanup_summary="scripted process domain clean",
        )
        receipt_ref = self.receipt_store.publish(
            AgentRunResult(proposal_bytes=raw_output, evidence=record)
        )
        receipt = self.receipt_store.load(receipt_ref)
        assert receipt.raw_output_uri is not None
        assert receipt.raw_output_hash is not None
        stored_patch = self.patch_store.publish(raw_patch) if raw_patch is not None else None
        batch = _batch(
            attempt,
            request_hash=bound_request_hash,
            normalized_patch_hash=(
                stored_patch.normalized_patch_hash
                if stored_patch is not None
                else normalized_patch_hash
            ),
            output_bytes=output_bytes,
            raw_output_uri=receipt.raw_output_uri,
            raw_output_hash=receipt.raw_output_hash,
            patch_uri=stored_patch.uri if stored_patch is not None else None,
            patch_hash=stored_patch.patch_hash if stored_patch is not None else None,
        )
        stored_batch = self.batch_store.publish(batch)
        return stored_batch.batch, (receipt_ref, stored_batch.uri)

    def test_start_replay_claim_and_barrier_dedupe_are_deterministic(self) -> None:
        knowledge = KnowledgeSnapshot(
            snapshot_id=UUID("51000000-0000-0000-0000-000000000005"),
            sources=(
                {
                    "knowledge_id": "skill/scripted-postgres-agent",
                    "source_kind": "skill",
                    "version": "1.0.0",
                    "source_uri": "skill:///scripted-postgres-agent/SKILL.md",
                    "content_hash": _payload_hash(b"scripted agent guidance\n"),
                    "license_id": "MulanPSL-2.0",
                },
            ),
            created_by="agent-postgres-test",
            created_at=NOW,
        )
        start = _start_request(
            "agent-postgres-barrier-v1",
            knowledge_snapshot=knowledge,
        )
        first = self.coordinator.start(start, self.repository)
        replay = self.coordinator.start(start, self.repository)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.generation_run_id, replay.generation_run_id)
        claim_a = self.repository.claim_generation_attempt("worker-a", lease_seconds=10, now=NOW)
        claim_b = self.repository.claim_generation_attempt("worker-b", lease_seconds=10, now=NOW)
        assert claim_a is not None and claim_b is not None
        self.assertEqual(claim_a.attempt.generator_id, "agent-a")
        self.assertEqual(claim_b.attempt.generator_id, "agent-b")

        duplicate_hash = _hash("9")
        duplicate_patch = (
            b"diff --git a/sglang/runtime/operator.py b/sglang/runtime/operator.py\n"
            b"--- a/sglang/runtime/operator.py\n"
            b"+++ b/sglang/runtime/operator.py\n"
            b"@@ -1 +1 @@\n"
            b"-return value\n"
            b"+return value + 1\n"
        )
        batch_b, (receipt_b, batch_b_uri) = self._settlement(
            claim_b,
            normalized_patch_hash=duplicate_hash,
            raw_patch=duplicate_patch,
        )
        self.repository.settle_generation_attempt(
            claim_b.attempt.attempt_id,
            claim_b.attempt.claim_token,
            batch_b,
            receipt_b,
            runner_receipt_reader=self.receipt_store,
            batch_uri=batch_b_uri,
            now=NOW + timedelta(seconds=1),
        )
        interim = self.repository.generation_run_status(first.generation_run_id)
        self.assertEqual(interim.run.state, "running")
        self.assertEqual(interim.proposals[0].disposition, "pending")

        batch_a, (receipt_a, batch_a_uri) = self._settlement(
            claim_a,
            normalized_patch_hash=duplicate_hash,
            raw_patch=duplicate_patch,
        )
        self.repository.settle_generation_attempt(
            claim_a.attempt.attempt_id,
            claim_a.attempt.claim_token,
            batch_a,
            receipt_a,
            runner_receipt_reader=self.receipt_store,
            batch_uri=batch_a_uri,
            now=NOW + timedelta(seconds=2),
        )
        status = self.repository.generation_run_status(first.generation_run_id)
        self.assertEqual(status.run.state, "awaiting_review")
        self.assertEqual(status.run.proposal_count, 2)
        self.assertEqual(status.run.retained_proposal_count, 1)
        self.assertEqual(status.proposals[0].generator_id, "agent-a")
        self.assertEqual(status.proposals[0].disposition, "retained")
        self.assertEqual(status.proposals[1].disposition, "duplicate")
        self.assertEqual(
            status.proposals[1].duplicate_of_proposal_id,
            status.proposals[0].proposal_id,
        )

        def publish_evidence(name: str, value: object) -> dict[str, str]:
            payload = canonical_json_bytes(value)
            path = self.evidence_root / "verification-input" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            return {
                "uri": path.resolve(strict=True).as_uri(),
                "content_hash": _payload_hash(payload),
            }

        knowledge_ref = publish_evidence("knowledge.json", knowledge)
        request_ref = publish_evidence("request.json", start.request)
        plan_ref = publish_evidence("plan.json", start.plan)
        status_ref = publish_evidence("status.json", status)
        context = AgentProposalVerificationContext(
            task_id=UUID("51000000-0000-0000-0000-000000000009"),
            target_id="postgres-scripted-agent",
            baseline_epoch_id=start.request.baseline_epoch_id,
            generation_run_id=first.generation_run_id,
            knowledge={
                **knowledge_ref,
                "identity_hash": knowledge_snapshot_hash(knowledge),
            },
            request={
                **request_ref,
                "identity_hash": candidate_generation_request_hash(start.request),
            },
            plan={
                **plan_ref,
                "identity_hash": apex_generation_plan_hash(start.plan),
            },
            generation_status=status_ref,
        )
        verification = AgentProposalVerifier(HashedEvidenceReader(self.evidence_root)).verify(
            context
        )
        self.assertEqual(verification.status, "ready_for_review")
        self.assertEqual(
            [item.status for item in verification.proposals],
            ["kept", "eliminated"],
        )
        self.assertIn(
            verification.proposals[1].reason_code,
            {"duplicate_exact_patch", "duplicate_normalized_patch"},
        )
        self.assertFalse(verification.formal_intake_allowed)
        self.assertFalse(verification.automatic_release_allowed)

        completed = self.repository.complete_generation_review(
            first.generation_run_id,
            review_evidence_uri="evidence:///agent/review.json",
            review_evidence_hash=_hash("a"),
            now=NOW + timedelta(seconds=3),
        )
        self.assertEqual(completed.state, "completed")
        self.assertFalse(completed.automatic_release_allowed)

    def test_failure_retries_and_expired_claim_consumes_conservative_budget(self) -> None:
        start = self.coordinator.start(
            _start_request("agent-postgres-retry-v1"),
            self.repository,
        )
        first_claim = self.repository.claim_generation_attempt("worker-a", lease_seconds=1, now=NOW)
        assert first_claim is not None

        reconciled = self.repository.reconcile_generation_run(
            start.generation_run_id,
            now=NOW + timedelta(seconds=2),
        )
        attempts = self.repository.list_generation_attempts(start.generation_run_id)

        self.assertEqual(reconciled.state, "running")
        self.assertEqual(attempts[0].state, "failed")
        self.assertEqual(attempts[0].error_code, "attempt_lease_expired")
        self.assertEqual(
            attempts[0].actual.model_dump(exclude={"proposals"}),
            attempts[0].reserved.model_dump(exclude={"proposals"}),
        )
        self.assertEqual(attempts[0].actual.proposals, 0)
        self.assertEqual(attempts[1].attempt_number, 2)
        self.assertEqual(attempts[1].state, "pending")
        self.assertEqual(reconciled.budget_consumed.attempts, 1)

    def test_concurrent_claims_respect_plan_concurrency(self) -> None:
        start = self.coordinator.start(
            _start_request("agent-postgres-concurrency-v1", max_concurrency=1),
            self.repository,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(
                pool.map(
                    lambda ordinal: self.repository.claim_generation_attempt(
                        f"worker-{ordinal}",
                        lease_seconds=10,
                        generation_run_id=start.generation_run_id,
                        now=NOW,
                    ),
                    range(2),
                )
            )

        self.assertEqual(sum(item is not None for item in claims), 1)
        running = [
            item
            for item in self.repository.list_generation_attempts(start.generation_run_id)
            if item.state == "running"
        ]
        self.assertEqual(len(running), 1)

    def test_attempt_budget_overrun_fails_closed_without_proposal_ref(self) -> None:
        start = self.coordinator.start(
            _start_request("agent-postgres-budget-v1"),
            self.repository,
        )
        claim = self.repository.claim_generation_attempt("worker-a", lease_seconds=10, now=NOW)
        assert claim is not None
        batch, (receipt, batch_uri) = self._settlement(
            claim,
            normalized_patch_hash=_hash("b"),
            output_bytes=20_001,
        )
        terminal = self.repository.settle_generation_attempt(
            claim.attempt.attempt_id,
            claim.attempt.claim_token,
            batch,
            receipt,
            runner_receipt_reader=self.receipt_store,
            batch_uri=batch_uri,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(terminal.state, "failed")
        self.assertEqual(terminal.error_code, "generation_budget_exceeded")
        self.assertEqual(
            self.repository.list_candidate_proposal_refs(start.generation_run_id),
            (),
        )

    def test_request_hash_drift_fails_closed_before_proposal_persistence(self) -> None:
        drift_cases: tuple[tuple[str, str | UUID], ...] = (
            ("baseline_source_hash", _hash("a")),
            ("hotspot_id", UUID("51000000-0000-0000-0000-000000000099")),
            ("knowledge_snapshot_hash", _hash("b")),
        )
        for field, value in drift_cases:
            with self.subTest(field=field):
                start = self.coordinator.start(
                    _start_request(f"agent-postgres-request-drift-{field}-v1"),
                    self.repository,
                )
                claim = self.repository.claim_generation_attempt(
                    "worker-a",
                    lease_seconds=10,
                    generation_run_id=start.generation_run_id,
                    now=NOW,
                )
                assert claim is not None
                drifted_request = claim.request.model_copy(update={field: value})
                drifted_hash = candidate_generation_request_hash(drifted_request)
                batch, (receipt, batch_uri) = self._settlement(
                    claim,
                    request_hash=drifted_hash,
                    normalized_patch_hash=_hash("c"),
                )

                with self.assertRaises(Conflict):
                    self.repository.settle_generation_attempt(
                        claim.attempt.attempt_id,
                        claim.attempt.claim_token,
                        batch,
                        receipt,
                        runner_receipt_reader=self.receipt_store,
                        batch_uri=batch_uri,
                        now=NOW + timedelta(seconds=1),
                    )

                status = self.repository.generation_run_status(start.generation_run_id)
                self.assertEqual(status.run.state, "running")
                self.assertEqual(status.proposals, ())
                attempts = self.repository.list_generation_attempts(start.generation_run_id)
                self.assertEqual(attempts[0].state, "running")

    def test_generation_budget_ledger_is_append_only(self) -> None:
        start = self.coordinator.start(
            _start_request("agent-postgres-ledger-v1"),
            self.repository,
        )
        with self.assertRaises(psycopg.Error):
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE agent_generation_budget_ledger
                    SET actual = '{"attempts": 1}'::jsonb
                    WHERE generation_run_id = %s
                    """,
                    (start.generation_run_id,),
                )
        self.connection.rollback()
