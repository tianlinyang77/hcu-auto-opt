# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from hcuopt.agent.authority import (
    AgentAuthorityError,
    actual_usage_for,
    build_generation_run_start,
    finalize_proposal_dispositions,
    generation_plan_id_for,
    generation_run_id_for,
    proposal_refs_for_batch,
    usage_is_within_reservation,
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

NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
IDEMPOTENCY_KEY = "agent-authority-fixture-v1"


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _start_request() -> GenerationRunStartRequest:
    run_id = generation_run_id_for(IDEMPOTENCY_KEY)
    request = CandidateGenerationRequest(
        request_id=UUID("10000000-0000-0000-0000-000000000001"),
        generation_run_id=run_id,
        target_snapshot_id=UUID("10000000-0000-0000-0000-000000000002"),
        stage0_run_id=UUID("10000000-0000-0000-0000-000000000003"),
        baseline_epoch_id=UUID("10000000-0000-0000-0000-000000000004"),
        baseline_source_hash=_hash("1"),
        hotspot_id=UUID("10000000-0000-0000-0000-000000000005"),
        replacement_point="sglang.runtime.operator.forward",
        workload_id="agent-authority-fixture",
        workload_hash=_hash("2"),
        configuration_hash=_hash("3"),
        image_digest=_hash("4"),
        profiler_evidence_uri="evidence:///agent/profile.json",
        profiler_evidence_hash=_hash("5"),
        knowledge_snapshot_id=UUID("10000000-0000-0000-0000-000000000006"),
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
                "adapter_profile": "m2b-agent-a-v1",
                "max_attempts": 2,
                "max_proposals": 2,
                "timeout_seconds": 10,
                "max_output_bytes_per_attempt": 20_000,
                "max_tokens_per_attempt": 2_000,
            },
            {
                "generator_id": "agent-b",
                "adapter_profile": "m2b-agent-b-v1",
                "max_attempts": 1,
                "max_proposals": 2,
                "timeout_seconds": 10,
                "max_output_bytes_per_attempt": 20_000,
                "max_tokens_per_attempt": 2_000,
            },
        ),
        max_concurrency=2,
        budget={
            "max_generator_attempts": 3,
            "max_wall_seconds": 30,
            "max_total_output_bytes": 60_000,
            "max_total_tokens": 6_000,
            "max_proposals": 4,
        },
        created_by="apex-control-plane",
        created_at=NOW,
    )
    return GenerationRunStartRequest(
        request=request,
        plan=plan,
        actor="agent-authority-test",
        idempotency_key=IDEMPOTENCY_KEY,
    )


def _running(attempt: GeneratorAttempt) -> GeneratorAttempt:
    return GeneratorAttempt.model_validate(
        {
            **attempt.model_dump(mode="json"),
            "state": "running",
            "worker_id": "agent-worker-1",
            "claim_token": "10000000-0000-0000-0000-000000000099",
            "lease_expires_at": NOW + timedelta(seconds=10),
            "started_at": NOW,
            "updated_at": NOW,
            "version": 2,
        }
    )


def _batch(
    attempt: GeneratorAttempt,
    *,
    normalized_patch_hash: str,
    proposal_id: UUID,
) -> CandidateProposalBatch:
    return CandidateProposalBatch(
        batch_id=UUID(int=proposal_id.int + 1000),
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
        status="succeeded",
        proposals=(
            {
                "proposal_id": proposal_id,
                "request_id": attempt.request_id,
                "generation_run_id": attempt.generation_run_id,
                "generator_id": attempt.generator_id,
                "ordinal": 0,
                "optimization_intent": "remove one redundant materialization",
                "rationale": "The immutable trace binds this proposal to the hotspot.",
                "risk_summary": "Independent correctness review remains mandatory.",
                "patch_uri": f"proposal:///{proposal_id}.diff",
                "patch_hash": _hash("7"),
                "normalized_patch_hash": normalized_patch_hash,
                "touched_paths": ("sglang/runtime/operator.py",),
                "replacement_point": "sglang.runtime.operator.forward",
            },
        ),
        raw_output_uri=f"proposal:///{proposal_id}.json",
        raw_output_hash=_hash("8"),
        output_bytes=1024,
        token_count=128,
        attempt_count=attempt.attempt_number,
        wall_seconds=0.25,
        started_at=NOW,
        finished_at=NOW + timedelta(milliseconds=250),
        synthetic=True,
    )


def test_start_freezes_deterministic_run_attempts_and_reservations() -> None:
    run, attempts, ledger = build_generation_run_start(_start_request(), created_at=NOW)

    assert run.state == "created"
    assert run.generation_run_id == generation_run_id_for(IDEMPOTENCY_KEY)
    assert run.attempt_count == 2
    assert run.budget_reserved.attempts == 2
    assert run.budget_reserved.wall_milliseconds == 20_000
    assert [item.attempt_number for item in attempts] == [1, 1]
    assert len({item.attempt_id for item in attempts}) == 2
    assert {item.entry_type for item in ledger} == {"reserve"}
    assert all(not item.automatic_release_allowed for item in ledger)


def test_start_rejects_reused_identity_or_proposal_budget() -> None:
    start = _start_request()
    changed_request = start.request.model_copy(
        update={"generation_run_id": UUID("20000000-0000-0000-0000-000000000001")}
    )
    with pytest.raises(AgentAuthorityError, match="idempotency key"):
        build_generation_run_start(
            start.model_copy(update={"request": changed_request}),
            created_at=NOW,
        )

    oversized_plan = start.plan.model_copy(
        update={
            "budget": start.plan.budget.model_copy(update={"max_proposals": 5}),
        }
    )
    with pytest.raises(AgentAuthorityError, match="Proposal budget"):
        build_generation_run_start(
            start.model_copy(update={"plan": oversized_plan}),
            created_at=NOW,
        )


def test_batch_usage_and_refs_are_bound_to_the_claimed_attempt() -> None:
    run, attempts, _ledger = build_generation_run_start(_start_request(), created_at=NOW)
    attempt = _running(attempts[0])
    batch = _batch(
        attempt,
        normalized_patch_hash=_hash("9"),
        proposal_id=UUID("30000000-0000-0000-0000-000000000001"),
    )

    usage = actual_usage_for(batch)
    refs = proposal_refs_for_batch(attempt, run.plan.generators[0], batch)

    assert usage.wall_milliseconds == 250
    assert usage_is_within_reservation(usage, attempt.reserved)
    assert refs[0].disposition == "pending"
    assert refs[0].attempt_id == attempt.attempt_id

    mismatched = batch.model_copy(update={"attempt_count": 2})
    with pytest.raises(AgentAuthorityError, match="attempt count"):
        proposal_refs_for_batch(attempt, run.plan.generators[0], mismatched)


def test_barrier_dedupe_is_deterministic_not_completion_order() -> None:
    run, attempts, _ledger = build_generation_run_start(_start_request(), created_at=NOW)
    first_attempt = _running(attempts[0])
    second_attempt = _running(attempts[1])
    normalized = _hash("a")
    first = proposal_refs_for_batch(
        first_attempt,
        run.plan.generators[0],
        _batch(
            first_attempt,
            normalized_patch_hash=normalized,
            proposal_id=UUID("40000000-0000-0000-0000-000000000001"),
        ),
    )[0]
    second = proposal_refs_for_batch(
        second_attempt,
        run.plan.generators[1],
        _batch(
            second_attempt,
            normalized_patch_hash=normalized,
            proposal_id=UUID("40000000-0000-0000-0000-000000000002"),
        ),
    )[0]

    forward = finalize_proposal_dispositions((first, second))
    reverse = finalize_proposal_dispositions((second, first))

    assert forward == reverse
    assert forward[0].disposition == "retained"
    assert forward[1].disposition == "duplicate"
    assert forward[1].duplicate_of_proposal_id == forward[0].proposal_id
