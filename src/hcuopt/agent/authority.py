# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.agent.identity import (
    apex_generation_plan_hash,
    candidate_generation_request_hash,
    candidate_proposal_hash,
)
from hcuopt.contracts.agent_runner_v1 import RunnerExecutionReceipt
from hcuopt.contracts.agent_v1 import (
    CandidateProposalBatch,
    CandidateProposalRef,
    GenerationAttemptClaim,
    GenerationBudgetLedgerEntry,
    GenerationBudgetUsage,
    GenerationRun,
    GenerationRunStartRequest,
    GenerationRunStartView,
    GenerationRunStatusView,
    GeneratorAttempt,
    GeneratorPlanEntry,
)


class AgentAuthorityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def generation_run_id_for(idempotency_key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"hcuopt:agent-generation-run:{idempotency_key}")


def generation_plan_id_for(generation_run_id: UUID) -> UUID:
    return uuid5(generation_run_id, "hcuopt:agent-generation-plan:v1")


def generation_attempt_id_for(
    generation_run_id: UUID,
    generator_id: str,
    attempt_number: int,
) -> UUID:
    return uuid5(
        generation_run_id,
        f"hcuopt:agent-generator-attempt:{generator_id}:{attempt_number}",
    )


def generation_budget_entry_id_for(attempt_id: UUID, entry_type: str) -> UUID:
    return uuid5(attempt_id, f"hcuopt:agent-generation-budget:{entry_type}")


def generation_budget_idempotency_key(attempt_id: UUID, entry_type: str) -> str:
    return f"agent-generation-budget:{attempt_id}:{entry_type}"


def reserved_usage_for(generator: GeneratorPlanEntry) -> GenerationBudgetUsage:
    return GenerationBudgetUsage(
        attempts=1,
        wall_milliseconds=generator.timeout_seconds * 1000,
        output_bytes=generator.max_output_bytes_per_attempt,
        tokens=generator.max_tokens_per_attempt,
        proposals=generator.max_proposals,
    )


def actual_usage_for(batch: CandidateProposalBatch) -> GenerationBudgetUsage:
    return GenerationBudgetUsage(
        attempts=1,
        wall_milliseconds=math.ceil(batch.wall_seconds * 1000),
        output_bytes=batch.output_bytes,
        tokens=batch.token_count,
        proposals=len(batch.proposals),
    )


def actual_usage_for_runner_receipt(
    receipt: RunnerExecutionReceipt,
    *,
    proposal_count: int,
) -> GenerationBudgetUsage:
    execution = receipt.execution
    if execution.tokens_consumed is None:
        raise AgentAuthorityError(
            "runner_usage_missing",
            "Runner Receipt does not contain authoritative token usage",
        )
    return GenerationBudgetUsage(
        attempts=execution.attempts_consumed,
        wall_milliseconds=math.ceil(execution.wall_seconds_consumed * 1000),
        output_bytes=execution.total_output_bytes_consumed,
        tokens=execution.tokens_consumed,
        proposals=proposal_count,
    )


def validate_runner_receipt(
    run: GenerationRun,
    attempt: GeneratorAttempt,
    generator: GeneratorPlanEntry,
    receipt: RunnerExecutionReceipt,
    batch: CandidateProposalBatch | None,
) -> None:
    execution = receipt.execution
    authoritative_request_hash = candidate_generation_request_hash(run.request)
    if run.request_hash != authoritative_request_hash:
        raise AgentAuthorityError(
            "generation_request_authority_mismatch",
            "stored Generation Request does not match its authoritative Hash",
        )
    if (
        execution.attempt_id != attempt.attempt_id
        or execution.generation_run_id != run.generation_run_id
        or execution.request_id != run.request.request_id
        or execution.request_hash != authoritative_request_hash
        or execution.plan_id != run.plan.plan_id
        or execution.generator_id != generator.generator_id
        or execution.attempt_number != attempt.attempt_number
    ):
        raise AgentAuthorityError(
            "runner_receipt_authority_mismatch",
            "Runner Receipt does not bind the claimed Generation Attempt",
        )
    if execution.generator_artifact_hash != generator.generator_artifact_hash:
        raise AgentAuthorityError(
            "runner_artifact_mismatch",
            "Runner Receipt used a different immutable Generator Artifact",
        )
    if execution.runner_provenance.capability != "agent_runner":
        raise AgentAuthorityError(
            "runner_provenance_invalid",
            "Runner Receipt does not contain Agent Runner provenance",
        )
    if batch is None:
        if execution.status == "succeeded":
            raise AgentAuthorityError(
                "runner_batch_missing",
                "successful Runner Receipt requires its Proposal Batch",
            )
        return
    if execution.cleanup_status != "verified":
        raise AgentAuthorityError(
            "runner_cleanup_unverified",
            "Proposal Batch cannot settle before Runner cleanup is verified",
        )
    if execution.status != "succeeded":
        raise AgentAuthorityError(
            "runner_batch_status_mismatch",
            "unsuccessful Runner Receipt cannot bind a Proposal Batch",
        )
    if (
        receipt.raw_output_uri != batch.raw_output_uri
        or receipt.raw_output_hash != batch.raw_output_hash
        or receipt.raw_output_bytes != batch.output_bytes
    ):
        raise AgentAuthorityError(
            "runner_raw_output_mismatch",
            "Proposal Batch does not bind the exact Runner raw output",
        )
    if execution.synthetic != batch.synthetic:
        raise AgentAuthorityError(
            "runner_synthetic_mismatch",
            "Runner Receipt and Proposal Batch synthetic authorities differ",
        )


def usage_is_within_reservation(
    actual: GenerationBudgetUsage,
    reserved: GenerationBudgetUsage,
) -> bool:
    return all(
        consumed <= limit
        for consumed, limit in (
            (actual.attempts, reserved.attempts),
            (actual.wall_milliseconds, reserved.wall_milliseconds),
            (actual.output_bytes, reserved.output_bytes),
            (actual.tokens, reserved.tokens),
            (actual.proposals, reserved.proposals),
        )
    )


def conservative_failure_usage(
    reserved: GenerationBudgetUsage,
) -> GenerationBudgetUsage:
    """Charge unknown compute/output fully, but never invent accepted Proposals."""

    return GenerationBudgetUsage(
        attempts=1,
        wall_milliseconds=reserved.wall_milliseconds,
        output_bytes=reserved.output_bytes,
        tokens=reserved.tokens,
        proposals=0,
    )


def sum_usage(items: Sequence[GenerationBudgetUsage]) -> GenerationBudgetUsage:
    result = GenerationBudgetUsage()
    for item in items:
        result = result.plus(item)
    return result


def _reserve_entry(
    run_id: UUID,
    attempt: GeneratorAttempt,
    created_at: datetime,
) -> GenerationBudgetLedgerEntry:
    return GenerationBudgetLedgerEntry(
        ledger_entry_id=generation_budget_entry_id_for(attempt.attempt_id, "reserve"),
        generation_run_id=run_id,
        attempt_id=attempt.attempt_id,
        entry_type="reserve",
        reserved=attempt.reserved,
        idempotency_key=generation_budget_idempotency_key(attempt.attempt_id, "reserve"),
        created_at=created_at,
    )


def build_pending_attempt(
    run: GenerationRun,
    generator: GeneratorPlanEntry,
    generator_ordinal: int,
    attempt_number: int,
    created_at: datetime,
) -> tuple[GeneratorAttempt, GenerationBudgetLedgerEntry]:
    attempt = GeneratorAttempt(
        attempt_id=generation_attempt_id_for(
            run.generation_run_id,
            generator.generator_id,
            attempt_number,
        ),
        generation_run_id=run.generation_run_id,
        plan_id=run.plan.plan_id,
        request_id=run.request.request_id,
        generator_id=generator.generator_id,
        generator_ordinal=generator_ordinal,
        attempt_number=attempt_number,
        adapter_profile=generator.adapter_profile,
        state="pending",
        reserved=reserved_usage_for(generator),
        version=1,
        created_at=created_at,
        updated_at=created_at,
    )
    return attempt, _reserve_entry(run.generation_run_id, attempt, created_at)


def build_generation_run_start(
    start: GenerationRunStartRequest,
    *,
    created_at: datetime,
) -> tuple[
    GenerationRun,
    tuple[GeneratorAttempt, ...],
    tuple[GenerationBudgetLedgerEntry, ...],
]:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise AgentAuthorityError("generation_time_naive", "Generation time must be timezone-aware")
    expected_run_id = generation_run_id_for(start.idempotency_key)
    if start.request.generation_run_id != expected_run_id:
        raise AgentAuthorityError(
            "generation_run_id_mismatch",
            "Generation Run identity does not match the idempotency key",
        )
    if start.plan.generation_run_id != expected_run_id:
        raise AgentAuthorityError(
            "generation_plan_run_mismatch",
            "Generation Plan is bound to a different Run",
        )
    if start.plan.plan_id != generation_plan_id_for(expected_run_id):
        raise AgentAuthorityError(
            "generation_plan_id_mismatch",
            "Generation Plan identity is not deterministic for its Run",
        )
    request_hash = candidate_generation_request_hash(start.request)
    if start.plan.request_id != start.request.request_id or start.plan.request_hash != request_hash:
        raise AgentAuthorityError(
            "generation_request_binding_mismatch",
            "Generation Plan does not bind the exact Candidate Generation Request",
        )
    if start.plan.budget.max_proposals > start.request.max_proposals:
        raise AgentAuthorityError(
            "generation_proposal_budget_exceeded",
            "Generation Plan Proposal budget exceeds the Request limit",
        )
    plan_hash = apex_generation_plan_hash(start.plan)
    initial_reserved = sum_usage(
        tuple(reserved_usage_for(generator) for generator in start.plan.generators)
    )
    run = GenerationRun(
        generation_run_id=expected_run_id,
        request=start.request,
        request_hash=request_hash,
        plan=start.plan,
        plan_hash=plan_hash,
        actor=start.actor,
        idempotency_key=start.idempotency_key,
        state="created",
        planned_generator_count=len(start.plan.generators),
        attempt_count=len(start.plan.generators),
        terminal_attempt_count=0,
        terminal_generator_count=0,
        proposal_count=0,
        retained_proposal_count=0,
        budget_reserved=initial_reserved,
        version=1,
        created_at=created_at,
        updated_at=created_at,
    )
    attempts_and_entries = tuple(
        build_pending_attempt(run, generator, ordinal, 1, created_at)
        for ordinal, generator in enumerate(start.plan.generators)
    )
    attempts = tuple(item[0] for item in attempts_and_entries)
    entries = tuple(item[1] for item in attempts_and_entries)
    return run, attempts, entries


def proposal_refs_for_batch(
    run: GenerationRun,
    attempt: GeneratorAttempt,
    generator: GeneratorPlanEntry,
    batch: CandidateProposalBatch,
) -> tuple[CandidateProposalRef, ...]:
    authoritative_request_hash = candidate_generation_request_hash(run.request)
    if (
        run.request_hash != authoritative_request_hash
        or run.plan.request_hash != authoritative_request_hash
    ):
        raise AgentAuthorityError(
            "generation_request_authority_mismatch",
            "stored Generation Request does not match its authoritative Hash",
        )
    if (
        attempt.generation_run_id != run.generation_run_id
        or batch.generation_run_id != run.generation_run_id
    ):
        raise AgentAuthorityError("proposal_batch_cross_run", "Proposal Batch crosses Runs")
    if (
        attempt.request_id != run.request.request_id
        or batch.request_id != run.request.request_id
    ):
        raise AgentAuthorityError("proposal_batch_cross_request", "Proposal Batch crosses Requests")
    if batch.request_hash != authoritative_request_hash or any(
        proposal.request_hash != authoritative_request_hash
        for proposal in batch.proposals
    ):
        raise AgentAuthorityError(
            "proposal_batch_request_hash_mismatch",
            "Proposal Batch does not bind the authoritative Generation Request Hash",
        )
    if batch.generator_id != attempt.generator_id or generator.generator_id != attempt.generator_id:
        raise AgentAuthorityError(
            "proposal_batch_cross_generator",
            "Proposal Batch crosses generator authority",
        )
    if batch.attempt_count != attempt.attempt_number:
        raise AgentAuthorityError(
            "proposal_batch_attempt_mismatch",
            "Proposal Batch attempt count does not match its claimed Attempt",
        )
    if len(batch.proposals) > generator.max_proposals:
        raise AgentAuthorityError(
            "proposal_batch_limit_exceeded",
            "Proposal Batch exceeds its generator Proposal limit",
        )
    return tuple(
        CandidateProposalRef(
            proposal_id=proposal.proposal_id,
            proposal_hash=candidate_proposal_hash(proposal),
            generation_run_id=attempt.generation_run_id,
            request_id=attempt.request_id,
            attempt_id=attempt.attempt_id,
            batch_id=batch.batch_id,
            generator_id=attempt.generator_id,
            generator_ordinal=attempt.generator_ordinal,
            proposal_ordinal=proposal.ordinal,
            patch_uri=proposal.patch_uri,
            patch_hash=proposal.patch_hash,
            normalized_patch_hash=proposal.normalized_patch_hash,
            disposition="pending",
        )
        for proposal in sorted(batch.proposals, key=lambda item: (item.ordinal, item.proposal_id))
    )


def finalize_proposal_dispositions(
    proposals: Sequence[CandidateProposalRef],
) -> tuple[CandidateProposalRef, ...]:
    ordered = sorted(
        proposals,
        key=lambda item: (
            item.normalized_patch_hash,
            item.generator_ordinal,
            item.proposal_ordinal,
            item.proposal_hash,
            item.proposal_id,
        ),
    )
    retained_by_patch: dict[str, UUID] = {}
    finalized: list[CandidateProposalRef] = []
    for proposal in ordered:
        retained_id = retained_by_patch.get(proposal.normalized_patch_hash)
        if retained_id is None:
            retained_by_patch[proposal.normalized_patch_hash] = proposal.proposal_id
            finalized.append(
                proposal.model_copy(
                    update={"disposition": "retained", "duplicate_of_proposal_id": None}
                )
            )
        else:
            finalized.append(
                proposal.model_copy(
                    update={
                        "disposition": "duplicate",
                        "duplicate_of_proposal_id": retained_id,
                    }
                )
            )
    return tuple(
        sorted(
            finalized,
            key=lambda item: (
                item.generator_ordinal,
                item.proposal_ordinal,
                item.proposal_hash,
                item.proposal_id,
            ),
        )
    )


class GenerationAuthorityRepository(Protocol):
    def create_generation_run(
        self,
        run: GenerationRun,
        attempts: tuple[GeneratorAttempt, ...],
        ledger_entries: tuple[GenerationBudgetLedgerEntry, ...],
    ) -> tuple[GenerationRun, bool]: ...

    def reconcile_generation_run(
        self,
        generation_run_id: UUID,
        *,
        now: datetime,
    ) -> GenerationRun: ...

    def generation_run_status(self, generation_run_id: UUID) -> GenerationRunStatusView: ...


class ApexGenerationCoordinator:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def start(
        self,
        request: GenerationRunStartRequest,
        repository: GenerationAuthorityRepository,
    ) -> GenerationRunStartView:
        run, attempts, entries = build_generation_run_start(
            request,
            created_at=self.clock(),
        )
        stored, created = repository.create_generation_run(run, attempts, entries)
        current = repository.reconcile_generation_run(
            stored.generation_run_id,
            now=self.clock(),
        )
        return GenerationRunStartView.model_validate(
            {**current.model_dump(mode="json"), "replayed": not created}
        )

    def status(
        self,
        generation_run_id: UUID,
        repository: GenerationAuthorityRepository,
    ) -> GenerationRunStatusView:
        return repository.generation_run_status(generation_run_id)

    def reconcile(
        self,
        generation_run_id: UUID,
        repository: GenerationAuthorityRepository,
    ) -> GenerationRunStatusView:
        repository.reconcile_generation_run(generation_run_id, now=self.clock())
        return repository.generation_run_status(generation_run_id)


__all__ = [
    "AgentAuthorityError",
    "ApexGenerationCoordinator",
    "GenerationAttemptClaim",
    "actual_usage_for",
    "build_generation_run_start",
    "build_pending_attempt",
    "conservative_failure_usage",
    "finalize_proposal_dispositions",
    "generation_attempt_id_for",
    "generation_budget_entry_id_for",
    "generation_plan_id_for",
    "generation_run_id_for",
    "proposal_refs_for_batch",
    "reserved_usage_for",
    "sum_usage",
    "usage_is_within_reservation",
]
