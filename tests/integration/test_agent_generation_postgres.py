# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid5

import pytest

from hcuopt.agent.authority import (
    ApexGenerationCoordinator,
    generation_plan_id_for,
    generation_run_id_for,
)
from hcuopt.agent.identity import candidate_generation_request_hash
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposalBatch,
    GenerationRunStartRequest,
    GeneratorAttempt,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.storage.repository import PostgresRepository

try:
    import psycopg
except ImportError:  # pragma: no cover - package dependency in normal installs
    psycopg = None


DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _start_request(
    key: str,
    *,
    max_concurrency: int = 2,
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
        knowledge_snapshot_id=UUID("51000000-0000-0000-0000-000000000005"),
        knowledge_snapshot_hash=_hash("6"),
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
                "max_attempts": 2,
                "max_proposals": 2,
                "timeout_seconds": 10,
                "max_output_bytes_per_attempt": 20_000,
                "max_tokens_per_attempt": 2_000,
            },
            {
                "generator_id": "agent-b",
                "adapter_profile": "m2b-scripted-agent-b-v1",
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
    normalized_patch_hash: str,
    failed: bool = False,
    output_bytes: int = 1024,
) -> CandidateProposalBatch:
    proposal_id = uuid5(attempt.attempt_id, "proposal-0")
    return CandidateProposalBatch(
        batch_id=uuid5(attempt.attempt_id, "batch"),
        request_id=attempt.request_id,
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
                "generation_run_id": attempt.generation_run_id,
                "generator_id": attempt.generator_id,
                "ordinal": 0,
                "optimization_intent": "remove one redundant materialization",
                "rationale": "The immutable trace binds the generated proposal.",
                "risk_summary": "Independent correctness review remains mandatory.",
                "patch_uri": f"proposal:///{proposal_id}.diff",
                "patch_hash": _hash("7"),
                "normalized_patch_hash": normalized_patch_hash,
                "touched_paths": ("sglang/runtime/operator.py",),
                "replacement_point": "sglang.runtime.operator.forward",
            },
        ),
        raw_output_uri=f"proposal:///{attempt.attempt_id}.json",
        raw_output_hash=_hash("8"),
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

    def tearDown(self) -> None:
        self.connection.close()

    def test_start_replay_claim_and_barrier_dedupe_are_deterministic(self) -> None:
        start = _start_request("agent-postgres-barrier-v1")
        first = self.coordinator.start(start, self.repository)
        replay = self.coordinator.start(start, self.repository)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.generation_run_id, replay.generation_run_id)
        claim_a = self.repository.claim_generation_attempt(
            "worker-a", lease_seconds=10, now=NOW
        )
        claim_b = self.repository.claim_generation_attempt(
            "worker-b", lease_seconds=10, now=NOW
        )
        assert claim_a is not None and claim_b is not None
        self.assertEqual(claim_a.attempt.generator_id, "agent-a")
        self.assertEqual(claim_b.attempt.generator_id, "agent-b")

        duplicate_hash = _hash("9")
        self.repository.settle_generation_attempt(
            claim_b.attempt.attempt_id,
            claim_b.attempt.claim_token,
            _batch(claim_b.attempt, normalized_patch_hash=duplicate_hash),
            now=NOW + timedelta(seconds=1),
        )
        interim = self.repository.generation_run_status(first.generation_run_id)
        self.assertEqual(interim.run.state, "running")
        self.assertEqual(interim.proposals[0].disposition, "pending")

        self.repository.settle_generation_attempt(
            claim_a.attempt.attempt_id,
            claim_a.attempt.claim_token,
            _batch(claim_a.attempt, normalized_patch_hash=duplicate_hash),
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
        first_claim = self.repository.claim_generation_attempt(
            "worker-a", lease_seconds=1, now=NOW
        )
        assert first_claim is not None

        reconciled = self.repository.reconcile_generation_run(
            start.generation_run_id,
            now=NOW + timedelta(seconds=2),
        )
        attempts = self.repository.list_generation_attempts(start.generation_run_id)

        self.assertEqual(reconciled.state, "running")
        self.assertEqual(attempts[0].state, "failed")
        self.assertEqual(attempts[0].error_code, "attempt_lease_expired")
        self.assertEqual(attempts[0].actual, attempts[0].reserved)
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
        claim = self.repository.claim_generation_attempt(
            "worker-a", lease_seconds=10, now=NOW
        )
        assert claim is not None
        terminal = self.repository.settle_generation_attempt(
            claim.attempt.attempt_id,
            claim.attempt.claim_token,
            _batch(
                claim.attempt,
                normalized_patch_hash=_hash("b"),
                output_bytes=20_001,
            ),
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(terminal.state, "failed")
        self.assertEqual(terminal.error_code, "generation_budget_exceeded")
        self.assertEqual(
            self.repository.list_candidate_proposal_refs(start.generation_run_id),
            (),
        )

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
