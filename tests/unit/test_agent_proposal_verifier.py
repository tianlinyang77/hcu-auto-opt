# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from hcuopt.agent.authority import finalize_proposal_dispositions
from hcuopt.agent.identity import (
    apex_generation_plan_hash,
    candidate_generation_request_hash,
    candidate_proposal_batch_hash,
    candidate_proposal_hash,
    candidate_proposal_promotion_receipt_hash,
    candidate_proposal_review_record_hash,
    knowledge_snapshot_hash,
)
from hcuopt.contracts.agent_read_model_v1 import AgentPublishedEvidenceRef
from hcuopt.contracts.agent_runner_v1 import (
    RunnerExecutionReceipt,
    RunnerExecutionRecord,
    RunnerProvenance,
    runner_execution_receipt_id_for,
    runner_provenance_identity_hash,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposal,
    CandidateProposalBatch,
    CandidateProposalPromotionReceipt,
    CandidateProposalRef,
    CandidateProposalReviewRecord,
    GenerationBudgetLedgerEntry,
    GenerationBudgetUsage,
    GenerationRun,
    GenerationRunStatusView,
    GeneratorAttempt,
    KnowledgeSnapshot,
)
from hcuopt.contracts.agent_verification_v1 import (
    AgentEvidenceRef,
    AgentProposalVerificationContext,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.evaluation.agent_generation_read_model import (
    AgentGenerationEvidenceReadService,
    AgentGenerationReadModelError,
    build_agent_generation_evidence_publication,
)
from hcuopt.evaluation.agent_proposal_reporting import (
    AGENT_GENERATION_WARNING,
    build_agent_generation_evidence,
    write_agent_generation_report,
)
from hcuopt.evaluation.agent_proposal_verifier import (
    AgentProposalEvidenceError,
    AgentProposalVerifier,
    build_agent_generation_read_model,
    normalize_patch_v1,
)
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.measurement.evidence import canonical_json_bytes

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("10000000-0000-0000-0000-000000000002")
PLAN_ID = UUID("10000000-0000-0000-0000-000000000003")


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _fixed_hash(character: str) -> str:
    return "sha256:" + character * 64


class PortableReader(HashedEvidenceReader):
    def _secure_read(self, path: Path) -> bytes:
        data = path.read_bytes()
        if len(data) > self.max_bytes:
            raise ValueError("too large")
        return data


def _write(path: Path, value: object | bytes) -> dict[str, str]:
    encoded = value if isinstance(value, bytes) else canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return {"uri": path.as_uri(), "content_hash": _sha(encoded)}


def _generator_provenance(
    profile: str = "m2b-generator-scripted-test",
) -> AdapterProvenance:
    return AdapterProvenance(
        profile=profile,
        capability="candidate_proposal_generation",
        adapter_name="ScriptedAgent",
        adapter_version="1.0.0",
        implementation_kind="fake",
    )


def _runner_provenance() -> AdapterProvenance:
    return AdapterProvenance(
        profile="m2b-runner-scripted-test",
        capability="agent_runner",
        adapter_name="ScriptedAgentRunner",
        adapter_version="1.0.0",
        implementation_kind="fake",
    )


def _knowledge() -> KnowledgeSnapshot:
    return KnowledgeSnapshot(
        snapshot_id=UUID("10000000-0000-0000-0000-000000000004"),
        sources=[
            {
                "knowledge_id": "skills/triton-guide",
                "source_kind": "skill",
                "version": "v1",
                "source_uri": "skill://triton-guide/v1",
                "content_hash": _fixed_hash("1"),
                "license_id": "MIT",
            }
        ],
        created_by="scripted-test",
        created_at=NOW,
    )


def _request(knowledge: KnowledgeSnapshot, *, max_proposals: int = 4) -> CandidateGenerationRequest:
    return CandidateGenerationRequest(
        request_id=REQUEST_ID,
        generation_run_id=RUN_ID,
        target_snapshot_id=UUID("10000000-0000-0000-0000-000000000005"),
        stage0_run_id=UUID("10000000-0000-0000-0000-000000000006"),
        baseline_epoch_id=UUID("10000000-0000-0000-0000-000000000007"),
        baseline_source_hash=_fixed_hash("2"),
        hotspot_id=UUID("10000000-0000-0000-0000-000000000008"),
        replacement_point="sglang.runtime.operator.forward",
        workload_id="m2b-scripted-workload",
        workload_hash=_fixed_hash("3"),
        configuration_hash=_fixed_hash("4"),
        image_digest=_fixed_hash("5"),
        profiler_evidence_uri="evidence:///profile.json",
        profiler_evidence_hash=_fixed_hash("6"),
        knowledge_snapshot_id=knowledge.snapshot_id,
        knowledge_snapshot_hash=knowledge_snapshot_hash(knowledge),
        max_proposals=max_proposals,
    )


def _plan(
    request: CandidateGenerationRequest,
    *,
    max_attempts: int = 2,
    generator_max_proposals: int = 4,
    budget_max_proposals: int = 4,
    include_second_generator: bool = False,
) -> ApexGenerationPlan:
    generators = [
        {
            "generator_id": "agent-one",
            "adapter_profile": "m2b-agent-one",
            "generator_artifact_hash": _fixed_hash("a"),
            "max_attempts": max_attempts,
            "max_proposals": generator_max_proposals,
            "timeout_seconds": 30,
            "max_output_bytes_per_attempt": 20_000,
            "max_tokens_per_attempt": 2_000,
        }
    ]
    if include_second_generator:
        generators.append(
            {
                "generator_id": "agent-two",
                "adapter_profile": "m2b-agent-two",
                "generator_artifact_hash": _fixed_hash("b"),
                "max_attempts": 1,
                "max_proposals": 2,
                "timeout_seconds": 30,
                "max_output_bytes_per_attempt": 20_000,
                "max_tokens_per_attempt": 2_000,
            }
        )
    return ApexGenerationPlan(
        plan_id=PLAN_ID,
        generation_run_id=RUN_ID,
        request_id=REQUEST_ID,
        request_hash=candidate_generation_request_hash(request),
        generators=generators,
        max_concurrency=1,
        budget={
            "max_generator_attempts": max_attempts + int(include_second_generator),
            "max_wall_seconds": 90,
            "max_total_output_bytes": 100_000,
            "max_total_tokens": 10_000,
            "max_proposals": budget_max_proposals + (2 if include_second_generator else 0),
        },
        created_by="scripted-apex",
        created_at=NOW,
    )


def _patch(body: str = "+    return x + 1") -> bytes:
    return (
        "diff --git a/sglang/runtime/operator.py b/sglang/runtime/operator.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/sglang/runtime/operator.py\n"
        "+++ b/sglang/runtime/operator.py\n"
        "@@ -1 +1 @@\n"
        "-    return x\n"
        f"{body}\n"
    ).encode()


def _terminal_attempt(
    root: Path,
    *,
    request: CandidateGenerationRequest,
    plan: ApexGenerationPlan,
    generator_id: str,
    attempt_id: UUID,
    proposals: tuple[CandidateProposal, ...],
    suffix: str,
    attempt_status: str = "succeeded",
    cleanup_healthy: bool = True,
) -> tuple[
    GeneratorAttempt,
    tuple[GenerationBudgetLedgerEntry, GenerationBudgetLedgerEntry],
    CandidateProposalBatch | None,
]:
    generator_ordinal = next(
        index for index, item in enumerate(plan.generators) if item.generator_id == generator_id
    )
    generator = plan.generators[generator_ordinal]
    runner_status = "cleanup_failed" if not cleanup_healthy else attempt_status
    successful = runner_status == "succeeded"
    raw_output = (
        canonical_json_bytes({"proposal_ids": [str(item.proposal_id) for item in proposals]})
        if successful
        else b""
    )
    raw_ref = _write(root / f"raw-output{suffix}.json", raw_output) if successful else None
    runner_adapter = _runner_provenance()
    runner_contract = RunnerProvenance(
        profile=runner_adapter.profile,
        adapter_name=runner_adapter.adapter_name,
        adapter_version=runner_adapter.adapter_version,
        implementation_kind=runner_adapter.implementation_kind,
        source_commit=runner_adapter.source_commit,
        identity_hash=runner_provenance_identity_hash(
            profile=runner_adapter.profile,
            capability=runner_adapter.capability,
            adapter_name=runner_adapter.adapter_name,
            adapter_version=runner_adapter.adapter_version,
            implementation_kind=runner_adapter.implementation_kind,
            source_commit=runner_adapter.source_commit,
        ),
    )
    record = RunnerExecutionRecord(
        attempt_id=attempt_id,
        generation_run_id=RUN_ID,
        request_id=REQUEST_ID,
        request_hash=candidate_generation_request_hash(request),
        plan_id=PLAN_ID,
        generator_id=generator_id,
        attempt_number=1,
        runner_provenance=runner_contract,
        generator_artifact_hash=generator.generator_artifact_hash,
        status=runner_status,
        synthetic=True,
        wall_seconds_consumed=1.0,
        stdout_bytes_consumed=len(raw_output),
        stderr_bytes_consumed=0,
        total_output_bytes_consumed=len(raw_output),
        tokens_consumed=10 if successful else 0,
        exit_code=0 if successful else None,
        executable_name="scripted-agent",
        argv_hash=_fixed_hash("c"),
        input_manifest_hash=_fixed_hash("d"),
        stdout_hash=_sha(raw_output),
        stderr_hash=_sha(b""),
        stdout_summary="scripted output" if successful else "",
        stderr_summary="",
        termination_reason=None if successful else runner_status,
        process_tree_cleanup="not_needed" if cleanup_healthy else "failed",
        cleanup_status="verified" if cleanup_healthy else "failed",
        cleanup_summary=(
            "scripted process domain clean" if cleanup_healthy else "scripted cleanup failure"
        ),
    )
    receipt = RunnerExecutionReceipt(
        receipt_id=runner_execution_receipt_id_for(attempt_id),
        execution=record,
        raw_output_uri=raw_ref["uri"] if raw_ref else None,
        raw_output_hash=raw_ref["content_hash"] if raw_ref else None,
        raw_output_bytes=len(raw_output),
    )
    receipt_ref = _write(root / f"receipt{suffix}.json", receipt.model_dump(mode="json"))

    batch = None
    batch_ref = None
    if successful:
        assert raw_ref is not None
        batch = CandidateProposalBatch(
            batch_id=uuid5(attempt_id, "batch"),
            request_id=REQUEST_ID,
            request_hash=candidate_generation_request_hash(request),
            generation_run_id=RUN_ID,
            generator_id=generator_id,
            adapter_provenance=_generator_provenance(generator.adapter_profile),
            status="succeeded",
            proposals=proposals,
            raw_output_uri=raw_ref["uri"],
            raw_output_hash=raw_ref["content_hash"],
            output_bytes=len(raw_output),
            token_count=10,
            attempt_count=1,
            wall_seconds=1.0,
            started_at=NOW,
            finished_at=NOW,
            synthetic=True,
        )
        batch_ref = _write(root / f"batch{suffix}.json", batch.model_dump(mode="json"))
        assert batch_ref["content_hash"] == candidate_proposal_batch_hash(batch)

    reserved = GenerationBudgetUsage(
        attempts=1,
        wall_milliseconds=generator.timeout_seconds * 1000,
        output_bytes=generator.max_output_bytes_per_attempt,
        tokens=generator.max_tokens_per_attempt,
        proposals=max(generator.max_proposals, len(proposals)),
    )
    actual = GenerationBudgetUsage(
        attempts=1,
        wall_milliseconds=1000,
        output_bytes=len(raw_output),
        tokens=10 if successful else 0,
        proposals=len(proposals) if successful else 0,
    )
    if not successful:
        actual = reserved.model_copy(update={"proposals": 0})
    error_code = (
        None
        if successful
        else "runner_cleanup_failed"
        if not cleanup_healthy
        else "runner_timeout"
        if attempt_status == "timed_out"
        else "runner_failed"
    )
    attempt = GeneratorAttempt(
        attempt_id=attempt_id,
        generation_run_id=RUN_ID,
        plan_id=PLAN_ID,
        request_id=REQUEST_ID,
        generator_id=generator_id,
        generator_ordinal=generator_ordinal,
        attempt_number=1,
        adapter_profile=generator.adapter_profile,
        state="succeeded" if successful else "failed",
        reserved=reserved,
        actual=actual,
        worker_id="scripted-worker",
        claim_token=uuid5(attempt_id, "claim"),
        lease_expires_at=NOW,
        batch_id=batch.batch_id if batch else None,
        batch_uri=batch_ref["uri"] if batch_ref else None,
        batch_hash=batch_ref["content_hash"] if batch_ref else None,
        batch_status=batch.status if batch else None,
        raw_output_uri=batch.raw_output_uri if batch else None,
        raw_output_hash=batch.raw_output_hash if batch else None,
        adapter_provenance=batch.adapter_provenance if batch else None,
        runner_receipt_id=receipt.receipt_id,
        runner_receipt_uri=receipt_ref["uri"],
        runner_receipt_hash=receipt_ref["content_hash"],
        runner_receipt_schema_version=receipt.schema_version,
        runner_provenance=runner_adapter,
        error_code=error_code,
        error_message=(
            "scripted Runner terminated without a Proposal Batch" if error_code else None
        ),
        version=3,
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )
    reserve = GenerationBudgetLedgerEntry(
        ledger_entry_id=uuid5(attempt_id, "reserve"),
        generation_run_id=RUN_ID,
        attempt_id=attempt_id,
        entry_type="reserve",
        reserved=reserved,
        idempotency_key=f"reserve-{attempt_id}",
        created_at=NOW,
    )
    settle = GenerationBudgetLedgerEntry(
        ledger_entry_id=uuid5(attempt_id, "settle"),
        generation_run_id=RUN_ID,
        attempt_id=attempt_id,
        entry_type="settle",
        reserved=reserved,
        actual=actual,
        idempotency_key=f"settle-{attempt_id}",
        created_at=NOW,
    )
    return attempt, (reserve, settle), batch


def _proposal_refs_for_fixture(
    attempt: GeneratorAttempt,
    batch: CandidateProposalBatch,
) -> tuple[CandidateProposalRef, ...]:
    pending = tuple(
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
    return finalize_proposal_dispositions(pending)


def _build(
    root: Path,
    *,
    proposals: tuple[bytes, ...] = (_patch(),),
    attempt_status: str = "succeeded",
    cleanup_healthy: bool = True,
    request_max_proposals: int = 4,
    generator_max_proposals: int = 4,
    budget_max_proposals: int = 4,
    include_second_generator: bool = False,
) -> tuple[AgentProposalVerificationContext, AgentProposalVerifier]:
    knowledge = _knowledge()
    request = _request(
        knowledge,
        max_proposals=request_max_proposals + (2 if include_second_generator else 0),
    )
    plan = _plan(
        request,
        max_attempts=(1 if attempt_status != "succeeded" or not cleanup_healthy else 2),
        generator_max_proposals=generator_max_proposals,
        budget_max_proposals=budget_max_proposals,
        include_second_generator=include_second_generator,
    )
    knowledge_ref = _write(root / "knowledge.json", knowledge.model_dump(mode="json"))
    request_ref = _write(root / "request.json", request.model_dump(mode="json"))
    plan_ref = _write(root / "plan.json", plan.model_dump(mode="json"))
    proposal_models: list[CandidateProposal] = []
    for ordinal, raw in enumerate(proposals):
        patch_ref = _write(root / f"proposal-{ordinal}.diff", raw)
        proposal_models.append(
            CandidateProposal(
                proposal_id=UUID(f"10000000-0000-0000-0000-{ordinal + 20:012d}"),
                request_id=REQUEST_ID,
                request_hash=candidate_generation_request_hash(request),
                generation_run_id=RUN_ID,
                generator_id="agent-one",
                ordinal=ordinal,
                optimization_intent=f"remove redundant conversion {ordinal}",
                rationale="Frozen evidence shows redundant work.",
                risk_summary="Requires independent correctness review.",
                patch_uri=patch_ref["uri"],
                patch_hash=patch_ref["content_hash"],
                normalized_patch_hash=_sha(normalize_patch_v1(raw)),
                touched_paths=["sglang/runtime/operator.py"],
                replacement_point=request.replacement_point,
            )
        )
    attempt, ledger, batch = _terminal_attempt(
        root,
        request=request,
        plan=plan,
        generator_id="agent-one",
        attempt_id=UUID("10000000-0000-0000-0000-000000000031"),
        proposals=tuple(proposal_models),
        suffix="",
        attempt_status=attempt_status,
        cleanup_healthy=cleanup_healthy,
    )
    has_proposals = attempt.state == "succeeded" and bool(proposal_models)
    run_state = (
        "running" if include_second_generator else "awaiting_review" if has_proposals else "failed"
    )
    run = GenerationRun(
        generation_run_id=RUN_ID,
        request=request,
        request_hash=candidate_generation_request_hash(request),
        plan=plan,
        plan_hash=apex_generation_plan_hash(plan),
        actor="scripted-apex",
        idempotency_key="scripted-verifier-run-v1",
        state=run_state,
        planned_generator_count=len(plan.generators),
        attempt_count=1,
        terminal_attempt_count=1,
        terminal_generator_count=1,
        proposal_count=len(proposal_models) if has_proposals else 0,
        retained_proposal_count=(
            len({item.normalized_patch_hash for item in proposal_models}) if has_proposals else 0
        ),
        budget_consumed=attempt.actual,
        error_code="no_retained_proposals" if run_state == "failed" else None,
        error_message=(
            "scripted Runner produced no retained Proposal" if run_state == "failed" else None
        ),
        version=3,
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW if run_state == "failed" else None,
    )
    proposal_refs = _proposal_refs_for_fixture(attempt, batch) if batch is not None else ()
    status = GenerationRunStatusView(
        run=run,
        attempts=(attempt,),
        proposals=proposal_refs,
        budget_ledger=ledger,
    )
    status_ref = _write(root / "status.json", status.model_dump(mode="json"))
    context = AgentProposalVerificationContext(
        task_id=UUID("10000000-0000-0000-0000-000000000040"),
        target_id="nmz36-agent-scripted",
        baseline_epoch_id=request.baseline_epoch_id,
        generation_run_id=RUN_ID,
        knowledge={**knowledge_ref, "identity_hash": knowledge_snapshot_hash(knowledge)},
        request={**request_ref, "identity_hash": candidate_generation_request_hash(request)},
        plan={**plan_ref, "identity_hash": apex_generation_plan_hash(plan)},
        generation_status=status_ref,
    )
    return context, AgentProposalVerifier(PortableReader(root))


def _add_second_successful_generator(
    root: Path, context: AgentProposalVerificationContext
) -> AgentProposalVerificationContext:
    request = CandidateGenerationRequest.model_validate_json((root / "request.json").read_bytes())
    plan = ApexGenerationPlan.model_validate_json((root / "plan.json").read_bytes())
    status = GenerationRunStatusView.model_validate_json((root / "status.json").read_bytes())
    raw_patch = _patch("+    return x + 2")
    patch_ref = _write(root / "proposal-agent-two.diff", raw_patch)
    proposal = CandidateProposal(
        proposal_id=UUID("10000000-0000-0000-0000-000000000099"),
        request_id=REQUEST_ID,
        request_hash=candidate_generation_request_hash(request),
        generation_run_id=RUN_ID,
        generator_id="agent-two",
        ordinal=0,
        optimization_intent="remove another redundant conversion",
        rationale="Frozen evidence shows a second redundant operation.",
        risk_summary="Requires independent correctness review.",
        patch_uri=patch_ref["uri"],
        patch_hash=patch_ref["content_hash"],
        normalized_patch_hash=_sha(normalize_patch_v1(raw_patch)),
        touched_paths=["sglang/runtime/operator.py"],
        replacement_point=request.replacement_point,
    )
    attempt, ledger, batch = _terminal_attempt(
        root,
        request=request,
        plan=plan,
        generator_id="agent-two",
        attempt_id=UUID("10000000-0000-0000-0000-000000000097"),
        proposals=(proposal,),
        suffix="-agent-two",
    )
    assert batch is not None
    pending_refs = _proposal_refs_for_fixture(attempt, batch)
    proposal_refs = finalize_proposal_dispositions((*status.proposals, *pending_refs))
    run = status.run.model_copy(
        update={
            "state": "awaiting_review",
            "attempt_count": 2,
            "terminal_attempt_count": 2,
            "terminal_generator_count": 2,
            "proposal_count": status.run.proposal_count + 1,
            "retained_proposal_count": sum(
                item.disposition == "retained" for item in proposal_refs
            ),
            "budget_consumed": status.run.budget_consumed.plus(attempt.actual),
            "version": status.run.version + 1,
        }
    )
    updated = GenerationRunStatusView(
        run=run,
        attempts=(*status.attempts, attempt),
        proposals=proposal_refs,
        budget_ledger=(*status.budget_ledger, *ledger),
    )
    status_ref = _write(root / "status.json", updated.model_dump(mode="json"))
    return context.model_copy(
        update={"generation_status": AgentEvidenceRef.model_validate(status_ref)}
    )


def _add_review_and_promotion(
    root: Path,
    context: AgentProposalVerificationContext,
) -> AgentProposalVerificationContext:
    request = CandidateGenerationRequest.model_validate_json((root / "request.json").read_bytes())
    batch = CandidateProposalBatch.model_validate_json((root / "batch.json").read_bytes())
    proposal = batch.proposals[0]
    review_evidence = _write(
        root / "human-review-evidence.json",
        {"decision": "approved", "scope": "source_only"},
    )
    review = CandidateProposalReviewRecord(
        review_id=UUID("10000000-0000-0000-0000-000000000070"),
        idempotency_key="review-agent-proposal-70",
        proposal_id=proposal.proposal_id,
        proposal_hash=candidate_proposal_hash(proposal),
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generation_run_id=request.generation_run_id,
        patch_uri=proposal.patch_uri,
        patch_hash=proposal.patch_hash,
        normalized_patch_hash=proposal.normalized_patch_hash,
        baseline_epoch_id=request.baseline_epoch_id,
        baseline_source_hash=request.baseline_source_hash,
        hotspot_id=request.hotspot_id,
        replacement_point=request.replacement_point,
        decision="approved",
        reviewer="independent-human-reviewer",
        reason="The bounded source-only change is approved for packaging.",
        review_evidence_uri=review_evidence["uri"],
        review_evidence_hash=review_evidence["content_hash"],
        reviewed_at=NOW,
    )
    review_ref = _write(root / "review.json", review.model_dump(mode="json"))
    source_package_ref = {
        "candidate_source_hash": _fixed_hash("7"),
        "source_package_hash": _fixed_hash("8"),
        "manifest_hash": _fixed_hash("9"),
        "manifest_schema_version": "m1-candidate-source-v1",
    }
    source_family_hash = _fixed_hash("a")
    candidate_id = UUID("10000000-0000-0000-0000-000000000071")
    family_evidence = _write(
        root / "source-family-verification.json",
        {
            "schema_version": "m2b-source-family-verification-evidence-v1",
            "proposal_id": str(proposal.proposal_id),
            "candidate_id": str(candidate_id),
            "source_package_ref": source_package_ref,
            "source_family_hash": source_family_hash,
            "family_manifest": {"candidate_kind": "business"},
            "verifier_provenance": {"capability": "business_candidate_family_verification"},
        },
    )
    promotion = CandidateProposalPromotionReceipt(
        promotion_id=UUID("10000000-0000-0000-0000-000000000072"),
        idempotency_key="promote-agent-proposal-72",
        proposal_id=proposal.proposal_id,
        proposal_hash=candidate_proposal_hash(proposal),
        request_id=request.request_id,
        request_hash=candidate_generation_request_hash(request),
        generation_run_id=request.generation_run_id,
        patch_uri=proposal.patch_uri,
        patch_hash=proposal.patch_hash,
        normalized_patch_hash=proposal.normalized_patch_hash,
        baseline_epoch_id=request.baseline_epoch_id,
        baseline_source_hash=request.baseline_source_hash,
        hotspot_id=request.hotspot_id,
        replacement_point=request.replacement_point,
        review=review,
        review_record_hash=candidate_proposal_review_record_hash(review),
        candidate_id=candidate_id,
        source_package_ref=source_package_ref,
        source_family_hash=source_family_hash,
        source_family_verification_evidence_uri=family_evidence["uri"],
        source_family_verification_evidence_hash=family_evidence["content_hash"],
        source_family_verifier_provenance=AdapterProvenance(
            profile="m2a-family-verifier-v1",
            capability="business_candidate_family_verification",
            adapter_name="BusinessCandidateFamilyVerifier",
            adapter_version="1.0.0",
            implementation_kind="fake",
        ),
        promoted_by="candidate-authority",
        promoted_at=NOW,
        synthetic=True,
    )
    promotion_ref = _write(root / "promotion.json", promotion.model_dump(mode="json"))
    assert review_ref["content_hash"] == candidate_proposal_review_record_hash(review)
    assert promotion_ref["content_hash"] == candidate_proposal_promotion_receipt_hash(
        promotion
    )
    return context.model_copy(
        update={
            "review_records": (AgentEvidenceRef.model_validate(review_ref),),
            "promotion_receipts": (AgentEvidenceRef.model_validate(promotion_ref),),
        }
    )


def _rewrite_status(
    root: Path,
    context: AgentProposalVerificationContext,
    status: GenerationRunStatusView,
) -> AgentProposalVerificationContext:
    status_ref = _write(root / "status.json", status.model_dump(mode="json"))
    return context.model_copy(
        update={"generation_status": AgentEvidenceRef.model_validate(status_ref)}
    )


def _replace_status_attempt(
    root: Path,
    context: AgentProposalVerificationContext,
    index: int,
    **updates: object,
) -> AgentProposalVerificationContext:
    status = GenerationRunStatusView.model_validate_json((root / "status.json").read_bytes())
    payload = status.attempts[index].model_dump(mode="json")
    payload.update(updates)
    attempt = GeneratorAttempt.model_validate(payload)
    attempts = list(status.attempts)
    attempts[index] = attempt
    return _rewrite_status(
        root,
        context,
        status.model_copy(update={"attempts": tuple(attempts)}),
    )


def test_recomputes_all_hashes_and_exposes_read_only_model(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)

    result = verifier.verify(context)
    read_model = build_agent_generation_read_model(result)

    assert result.status == "ready_for_review"
    assert result.proposals[0].status == "kept"
    assert result.knowledge_hash == context.knowledge.identity_hash
    assert result.request_hash == context.request.identity_hash
    assert result.plan_hash == context.plan.identity_hash
    assert read_model.status == result.status
    assert read_model.formal_intake_allowed is False
    assert read_model.automatic_release_allowed is False
    assert read_model.performance_conclusion == "not_measured"
    assert read_model.human_review_status == "pending"
    assert read_model.package_promotion_status == "pending"
    assert read_model.generation_run_id == context.generation_run_id
    assert read_model.request_id == REQUEST_ID
    assert read_model.plan_id == PLAN_ID
    assert read_model.budget_limit.max_generator_attempts == 2
    assert read_model.attempts[0].runner_receipt_id is not None
    assert read_model.attempts[0].runner_provenance is not None
    assert read_model.attempts[0].cleanup_status == "verified"
    assert read_model.proposals[0].normalized_patch_hash.startswith("sha256:")
    assert read_model.proposals[0].patch_preview.startswith("diff --git")
    assert read_model.proposals[0].lifecycle.review_status == "pending"


def test_lifecycle_rereads_review_promotion_and_family_evidence(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    context = _add_review_and_promotion(tmp_path, context)

    result = verifier.verify(context)
    read_model = build_agent_generation_read_model(result)

    assert result.human_review_status == "approved"
    assert result.package_promotion_status == "promoted"
    assert result.formal_readiness == "hold"
    assert read_model.proposals[0].lifecycle.review_status == "approved"
    assert read_model.proposals[0].lifecycle.promotion_status == "promoted"
    assert read_model.proposals[0].lifecycle.promotion is not None
    assert read_model.proposals[0].lifecycle.promotion.source_package_ref is not None
    assert read_model.proposals[0].lifecycle.readiness == "hold"
    assert read_model.formal_intake_allowed is False
    assert read_model.automatic_release_allowed is False


def test_lifecycle_rejects_family_evidence_drift(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    context = _add_review_and_promotion(tmp_path, context)
    family_path = tmp_path / "source-family-verification.json"
    family_path.write_bytes(canonical_json_bytes({"candidate_id": "tampered"}))

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "evidence_hash_mismatch"


def test_exact_and_normalized_patch_duplicates_are_eliminated(tmp_path: Path) -> None:
    first = _patch()
    exact_context, verifier = _build(tmp_path / "exact", proposals=(first, first))
    exact = verifier.verify(exact_context)
    assert [item.reason_code for item in exact.proposals] == [None, "duplicate_exact_patch"]

    normalized_variant = first.replace(b"\n", b"\r\n").replace(
        b"index 1111111..2222222 100644\r\n", b"index aaa..bbb 100644\r\n"
    )
    context, verifier = _build(tmp_path / "normalized", proposals=(first, normalized_variant))
    result = verifier.verify(context)
    assert result.proposals[1].reason_code == "duplicate_normalized_patch"


def test_all_prior_duplicates_produces_no_valid_proposals(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    initial = verifier.verify(context)
    context = context.model_copy(
        update={"previous_exact_patch_hashes": frozenset({initial.proposals[0].exact_patch_hash})}
    )

    result = verifier.verify(context)

    assert result.status == "no_valid_proposals"
    assert result.proposals[0].reason_code == "duplicate_exact_patch"
    assert "zero_valid_proposals" in result.failure_codes


def test_runner_timeout_and_cleanup_failure_are_preserved(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path / "timeout", proposals=(), attempt_status="timed_out")
    timeout = verifier.verify(context)
    assert timeout.status == "no_valid_proposals"
    assert set(timeout.failure_codes) == {"runner_timeout", "zero_valid_proposals"}

    context, verifier = _build(tmp_path / "cleanup", cleanup_healthy=False)
    cleanup = verifier.verify(context)
    assert cleanup.status == "invalid"
    assert "cleanup_failed" in cleanup.failure_codes
    assert cleanup.attempts[0].status == "invalid"
    assert cleanup.proposals == ()


def test_zero_proposal_failed_runner_is_not_success(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path, proposals=(), attempt_status="failed")

    result = verifier.verify(context)

    assert result.status == "no_valid_proposals"
    assert set(result.failure_codes) == {"runner_failed", "zero_valid_proposals"}


@pytest.mark.parametrize("document", ["knowledge", "request", "plan"])
def test_tampered_domain_hash_is_rejected(tmp_path: Path, document: str) -> None:
    context, verifier = _build(tmp_path)
    reference = getattr(context, document)
    bad = reference.model_copy(update={"identity_hash": _fixed_hash("f")})
    context = context.model_copy(update={document: bad})

    with pytest.raises(AgentProposalEvidenceError, match="identity hash mismatch"):
        verifier.verify(context)


def test_tampered_patch_bytes_are_rejected(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    (tmp_path / "proposal-0.diff").write_bytes(_patch("+    return x + 999"))

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "evidence_hash_mismatch"


def test_evidence_and_report_are_deterministic_and_immutable(tmp_path: Path) -> None:
    evidence_root = tmp_path / "raw"
    report_root = tmp_path / "report"
    report_root.mkdir()
    context, verifier = _build(evidence_root)
    result = verifier.verify(context)

    first = write_agent_generation_report(report_root, context, result)
    second = write_agent_generation_report(report_root, context, result)
    bundle = build_agent_generation_evidence(context, result)

    assert first == second
    assert bundle.evidence_id == build_agent_generation_evidence(context, result).evidence_id
    assert bundle.synthetic is True
    assert bundle.summary["performance_conclusion"] == "not_measured"
    assert bundle.summary["automatic_release_allowed"] is False
    assert AGENT_GENERATION_WARNING in (report_root / "report.md").read_text("utf-8")
    assert set(first) == {
        "verification.json",
        "evidence-bundle.json",
        "read-model.json",
        "report.md",
        "sha256sums.json",
    }

    (report_root / "report.md").chmod(0o644)
    (report_root / "report.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="different content"):
        write_agent_generation_report(report_root, context, result)


class _ReadModelRepository:
    def __init__(self, publication, status) -> None:  # type: ignore[no-untyped-def]
        self.publication = publication
        self.status = status

    def get_agent_generation_evidence_publication(self, generation_run_id):  # type: ignore[no-untyped-def]
        assert generation_run_id == self.publication.generation_run_id
        return self.publication

    def generation_run_status(self, generation_run_id):  # type: ignore[no-untyped-def]
        assert generation_run_id == self.status.run.generation_run_id
        return self.status


def _published_terminal_read_model(root: Path):  # type: ignore[no-untyped-def]
    context, _verifier = _build(root)
    status = GenerationRunStatusView.model_validate_json(
        (root / "status.json").read_bytes()
    )
    overall_review = _write(
        root / "overall-review.json",
        {"generation_run_id": str(context.generation_run_id), "decision": "complete"},
    )
    run_payload = status.run.model_dump(mode="json")
    run_payload.update(
        {
            "state": "completed",
            "review_evidence_uri": overall_review["uri"],
            "review_evidence_hash": overall_review["content_hash"],
            "finished_at": NOW.isoformat(),
            "version": status.run.version + 1,
        }
    )
    completed_status = GenerationRunStatusView(
        run=GenerationRun.model_validate(run_payload),
        attempts=status.attempts,
        proposals=status.proposals,
        budget_ledger=status.budget_ledger,
    )
    context = _rewrite_status(root, context, completed_status)
    reader = PortableReader(root)
    result = AgentProposalVerifier(reader).verify(context)
    report_root = root / "published"
    report_root.mkdir()
    artifacts = write_agent_generation_report(report_root, context, result)
    publication = build_agent_generation_evidence_publication(
        context,
        result,
        artifacts,
    )
    repository = _ReadModelRepository(publication, completed_status)
    return repository, reader, publication, result


def test_durable_read_model_rereads_and_rebuilds_all_evidence(tmp_path: Path) -> None:
    repository, reader, publication, result = _published_terminal_read_model(tmp_path)

    read_model = AgentGenerationEvidenceReadService(repository, reader).get(
        publication.generation_run_id
    )

    assert read_model == build_agent_generation_read_model(result)
    assert read_model.formal_readiness == "hold"
    assert read_model.performance_conclusion == "not_measured"
    assert read_model.formal_intake_allowed is False
    assert read_model.automatic_release_allowed is False


def test_durable_read_model_rejects_tampered_published_artifact(tmp_path: Path) -> None:
    repository, reader, publication, _result = _published_terminal_read_model(tmp_path)
    read_model_path = tmp_path / "published" / "read-model.json"
    read_model_path.chmod(0o644)
    read_model_path.write_text("{}", encoding="utf-8")

    with pytest.raises(AgentGenerationReadModelError) as raised:
        AgentGenerationEvidenceReadService(repository, reader).get(
            publication.generation_run_id
        )

    assert raised.value.code == "evidence_hash_mismatch"


def test_durable_read_model_rejects_missing_published_artifact(tmp_path: Path) -> None:
    repository, reader, publication, _result = _published_terminal_read_model(tmp_path)
    (tmp_path / "published" / "report.md").unlink()

    with pytest.raises(AgentGenerationReadModelError) as raised:
        AgentGenerationEvidenceReadService(repository, reader).get(
            publication.generation_run_id
        )

    assert raised.value.code == "evidence_path_escape"


def test_durable_read_model_rejects_nonterminal_or_drifted_authority(
    tmp_path: Path,
) -> None:
    repository, reader, publication, _result = _published_terminal_read_model(tmp_path)
    terminal = repository.status
    pending_payload = terminal.run.model_dump(mode="json")
    pending_payload.update(
        {
            "state": "awaiting_review",
            "review_evidence_uri": None,
            "review_evidence_hash": None,
            "finished_at": None,
            "version": terminal.run.version - 1,
        }
    )
    repository.status = terminal.model_copy(
        update={"run": GenerationRun.model_validate(pending_payload)}
    )
    service = AgentGenerationEvidenceReadService(repository, reader)

    with pytest.raises(AgentGenerationReadModelError) as nonterminal:
        service.get(publication.generation_run_id)
    assert nonterminal.value.code == "agent_generation_not_terminal"

    repository.status = terminal.model_copy(
        update={"run": terminal.run.model_copy(update={"version": terminal.run.version + 1})}
    )
    with pytest.raises(AgentGenerationReadModelError) as drifted:
        service.get(publication.generation_run_id)
    assert drifted.value.code == "agent_generation_status_drift"


def test_durable_read_model_rejects_verifier_or_manifest_drift(tmp_path: Path) -> None:
    repository, reader, publication, _result = _published_terminal_read_model(tmp_path)
    service = AgentGenerationEvidenceReadService(repository, reader)
    repository.publication = publication.model_copy(
        update={"verifier_version": "m2b-proposal-verifier-v0"}
    )
    with pytest.raises(AgentGenerationReadModelError) as version:
        service.get(publication.generation_run_id)
    assert version.value.code == "agent_verifier_version_drift"

    manifest_path = tmp_path / "published" / "sha256sums.json"
    manifest_path.chmod(0o644)
    manifest_ref = _write(
        manifest_path,
        {
            "schema_version": "m2b-generation-manifest-v1",
            "files": {
                "verification.json": {
                    "uri": publication.verification.uri,
                    "sha256": publication.verification.content_hash,
                    "byte_count": publication.verification.byte_count,
                }
            },
        },
    )
    repository.publication = publication.model_copy(
        update={
            "manifest": AgentPublishedEvidenceRef(
                uri=manifest_ref["uri"],
                content_hash=manifest_ref["content_hash"],
                byte_count=manifest_path.stat().st_size,
            )
        }
    )
    with pytest.raises(AgentGenerationReadModelError) as manifest:
        service.get(publication.generation_run_id)
    assert manifest.value.code == "agent_evidence_schema_invalid"


def test_attempt_order_does_not_change_verdict_or_digest(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path, include_second_generator=True)
    context = _add_second_successful_generator(tmp_path, context)

    forward = verifier.verify(context)
    status = GenerationRunStatusView.model_validate_json((tmp_path / "status.json").read_bytes())
    reversed_context = _rewrite_status(
        tmp_path,
        context,
        status.model_copy(update={"attempts": tuple(reversed(status.attempts))}),
    )
    reverse = verifier.verify(reversed_context)

    assert forward == reverse
    assert forward.input_digest == reverse.input_digest
    assert [item.generator_id for item in forward.proposals] == ["agent-one", "agent-two"]


def test_missing_planned_generator_fails_closed(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path, include_second_generator=True)

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "attempt_barrier_incomplete"


def test_cross_baseline_context_is_rejected(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    context = context.model_copy(
        update={"baseline_epoch_id": UUID("20000000-0000-0000-0000-000000000001")}
    )

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "baseline_binding_mismatch"


def test_request_and_generator_proposal_budgets_are_recomputed(tmp_path: Path) -> None:
    context, verifier = _build(
        tmp_path / "request",
        request_max_proposals=3,
        budget_max_proposals=4,
    )
    with pytest.raises(AgentProposalEvidenceError) as request_error:
        verifier.verify(context)
    assert request_error.value.code == "request_proposal_budget_exceeded"

    proposals = tuple(_patch(f"+    return x + {index}") for index in range(3))
    context, verifier = _build(
        tmp_path / "generator",
        proposals=proposals,
        request_max_proposals=4,
        generator_max_proposals=2,
        budget_max_proposals=4,
    )
    with pytest.raises(AgentProposalEvidenceError) as generator_error:
        verifier.verify(context)
    assert generator_error.value.code == "generator_proposal_budget_exceeded"


def test_lifecycle_cannot_be_declared_by_verifier_caller(tmp_path: Path) -> None:
    context, _ = _build(tmp_path)
    payload = context.model_dump(mode="json")
    payload["human_review"] = {"status": "accepted"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentProposalVerificationContext.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("batch_id", "duplicate_batch"),
        ("proposal_id", "duplicate_proposal_identity"),
    ],
)
def test_cross_batch_identities_are_rejected(
    tmp_path: Path, field: str, expected_code: str
) -> None:
    context, verifier = _build(tmp_path, include_second_generator=True)
    context = _add_second_successful_generator(tmp_path, context)
    status = GenerationRunStatusView.model_validate_json((tmp_path / "status.json").read_bytes())
    second_batch = json.loads((tmp_path / "batch-agent-two.json").read_text("utf-8"))
    if field == "batch_id":
        second_batch["batch_id"] = str(status.attempts[0].batch_id)
    else:
        second_batch["proposals"][0]["proposal_id"] = "10000000-0000-0000-0000-000000000020"
    batch_ref = _write(tmp_path / "batch-agent-two.json", second_batch)
    context = _replace_status_attempt(
        tmp_path,
        context,
        1,
        batch_id=second_batch["batch_id"],
        batch_hash=batch_ref["content_hash"],
    )

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == expected_code


def test_attempt_and_batch_usage_must_match(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt = json.loads(receipt_path.read_text("utf-8"))
    receipt["execution"]["wall_seconds_consumed"] = 0.5
    receipt_ref = _write(receipt_path, receipt)
    context = _replace_status_attempt(
        tmp_path,
        context,
        0,
        runner_receipt_hash=receipt_ref["content_hash"],
    )

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "attempt_usage_mismatch"


def test_generation_status_batch_metadata_must_match(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path / "status")
    context = _replace_status_attempt(
        tmp_path / "status",
        context,
        0,
        batch_status="partial",
    )
    with pytest.raises(AgentProposalEvidenceError) as status_error:
        verifier.verify(context)
    assert status_error.value.code == "attempt_batch_status_mismatch"

    context, verifier = _build(tmp_path / "provenance")
    status = GenerationRunStatusView.model_validate_json(
        (tmp_path / "provenance" / "status.json").read_bytes()
    )
    original = status.attempts[0].adapter_provenance
    assert original is not None
    context = _replace_status_attempt(
        tmp_path / "provenance",
        context,
        0,
        adapter_provenance=original.model_copy(update={"adapter_version": "9.9.9"}),
    )
    with pytest.raises(AgentProposalEvidenceError) as provenance_error:
        verifier.verify(context)
    assert provenance_error.value.code == "batch_provenance_mismatch"


def test_candidate_proposal_refs_are_independently_bound(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path / "hash")
    status = GenerationRunStatusView.model_validate_json(
        (tmp_path / "hash" / "status.json").read_bytes()
    )
    bad_reference = status.proposals[0].model_copy(update={"proposal_hash": _fixed_hash("f")})
    context = _rewrite_status(
        tmp_path / "hash",
        context,
        status.model_copy(update={"proposals": (bad_reference,)}),
    )
    with pytest.raises(AgentProposalEvidenceError) as hash_error:
        verifier.verify(context)
    assert hash_error.value.code == "proposal_ref_binding_mismatch"

    context, verifier = _build(tmp_path / "missing")
    status = GenerationRunStatusView.model_validate_json(
        (tmp_path / "missing" / "status.json").read_bytes()
    )
    context = _rewrite_status(
        tmp_path / "missing",
        context,
        status.model_copy(update={"proposals": ()}),
    )
    with pytest.raises(AgentProposalEvidenceError) as set_error:
        verifier.verify(context)
    assert set_error.value.code == "proposal_ref_set_mismatch"


def test_candidate_proposal_dispositions_are_recomputed(tmp_path: Path) -> None:
    raw_patch = _patch()
    context, verifier = _build(tmp_path, proposals=(raw_patch, raw_patch))
    status = GenerationRunStatusView.model_validate_json((tmp_path / "status.json").read_bytes())
    references = list(status.proposals)
    references[1] = references[1].model_copy(
        update={"disposition": "retained", "duplicate_of_proposal_id": None}
    )
    run = status.run.model_copy(update={"retained_proposal_count": 2})
    context = _rewrite_status(
        tmp_path,
        context,
        status.model_copy(update={"run": run, "proposals": tuple(references)}),
    )

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "proposal_disposition_mismatch"


def test_distinct_runner_and_generator_provenance_is_required(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path / "valid")
    result = verifier.verify(context)
    assert {item.capability for item in result.adapter_provenance} >= {
        "agent_runner",
        "candidate_proposal_generation",
    }

    context, verifier = _build(tmp_path / "bad-runner")
    receipt_path = tmp_path / "bad-runner" / "receipt.json"
    receipt = json.loads(receipt_path.read_text("utf-8"))
    receipt["execution"]["runner_provenance"]["profile"] = "tampered-runner-profile"
    runner_provenance = receipt["execution"]["runner_provenance"]
    runner_provenance["identity_hash"] = runner_provenance_identity_hash(
        profile=runner_provenance["profile"],
        capability=runner_provenance["capability"],
        adapter_name=runner_provenance["adapter_name"],
        adapter_version=runner_provenance["adapter_version"],
        implementation_kind=runner_provenance["implementation_kind"],
        source_commit=runner_provenance["source_commit"],
    )
    receipt_ref = _write(receipt_path, receipt)
    context = _replace_status_attempt(
        tmp_path / "bad-runner",
        context,
        0,
        runner_receipt_hash=receipt_ref["content_hash"],
    )
    with pytest.raises(AgentProposalEvidenceError) as runner_error:
        verifier.verify(context)
    assert runner_error.value.code == "runner_provenance_mismatch"

    context, verifier = _build(tmp_path / "bad-generator")
    batch_path = tmp_path / "bad-generator" / "batch.json"
    batch = json.loads(batch_path.read_text("utf-8"))
    batch["adapter_provenance"] = _runner_provenance().model_dump(mode="json")
    batch_ref = _write(batch_path, batch)
    context = _replace_status_attempt(
        tmp_path / "bad-generator",
        context,
        0,
        batch_hash=batch_ref["content_hash"],
    )
    with pytest.raises(AgentProposalEvidenceError) as generator_error:
        verifier.verify(context)
    assert generator_error.value.code == "batch_provenance_invalid"


def test_runner_provenance_identity_hash_is_independently_recomputed(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt = json.loads(receipt_path.read_text("utf-8"))
    receipt["execution"]["runner_provenance"]["identity_hash"] = _fixed_hash("f")
    receipt_ref = _write(receipt_path, receipt)
    context = _replace_status_attempt(
        tmp_path,
        context,
        0,
        runner_receipt_hash=receipt_ref["content_hash"],
    )

    with pytest.raises(AgentProposalEvidenceError) as raised:
        verifier.verify(context)

    assert raised.value.code == "evidence_schema_invalid"


def test_attempt_batch_status_and_raw_output_binding(tmp_path: Path) -> None:
    context, verifier = _build(tmp_path / "partial")
    batch_path = tmp_path / "partial" / "batch.json"
    batch = json.loads(batch_path.read_text("utf-8"))
    batch["status"] = "partial"
    batch["error_code"] = "bounded_partial_output"
    batch["error_message"] = "generator stopped after its bounded output limit"
    batch_ref = _write(batch_path, batch)
    context = _replace_status_attempt(
        tmp_path / "partial",
        context,
        0,
        batch_hash=batch_ref["content_hash"],
        batch_status="partial",
    )
    assert verifier.verify(context).status == "ready_for_review"

    context, verifier = _build(tmp_path / "failed")
    batch_path = tmp_path / "failed" / "batch.json"
    batch = json.loads(batch_path.read_text("utf-8"))
    batch["status"] = "failed"
    batch["proposals"] = []
    batch["error_code"] = "generator_failed"
    batch["error_message"] = "generator failed before producing valid proposals"
    batch_ref = _write(batch_path, batch)
    context = _replace_status_attempt(
        tmp_path / "failed",
        context,
        0,
        batch_hash=batch_ref["content_hash"],
        batch_status="failed",
    )
    with pytest.raises(AgentProposalEvidenceError) as status_error:
        verifier.verify(context)
    assert status_error.value.code == "attempt_batch_status_mismatch"

    context, verifier = _build(tmp_path / "raw")
    batch_path = tmp_path / "raw" / "batch.json"
    batch = json.loads(batch_path.read_text("utf-8"))
    batch["raw_output_hash"] = _fixed_hash("f")
    batch_ref = _write(batch_path, batch)
    context = _replace_status_attempt(
        tmp_path / "raw",
        context,
        0,
        batch_hash=batch_ref["content_hash"],
        raw_output_hash=_fixed_hash("f"),
    )
    with pytest.raises(AgentProposalEvidenceError) as raw_error:
        verifier.verify(context)
    assert raw_error.value.code == "raw_output_binding_mismatch"
