# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import ValidationError

from hcuopt.agent.identity import (
    apex_generation_plan_hash,
    candidate_generation_request_hash,
    candidate_proposal_hash,
    knowledge_snapshot_hash,
)
from hcuopt.contracts.agent_runner_v1 import (
    RunnerExecutionReceipt,
    RunnerExecutionReceiptRef,
    runner_execution_receipt_hash,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposal,
    CandidateProposalBatch,
    CandidateProposalRef,
    GenerationBudgetUsage,
    GenerationRunStatusView,
    GeneratorAttempt,
    KnowledgeSnapshot,
)
from hcuopt.contracts.agent_verification_v1 import (
    AGENT_PROPOSAL_VERIFIER_VERSION,
    AgentAttemptDecision,
    AgentEvidenceRef,
    AgentGenerationReadModel,
    AgentProposalDecision,
    AgentProposalReadModel,
    AgentProposalVerificationContext,
    AgentProposalVerificationResult,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.evaluation.evidence_reader import EvidenceReadError, HashedEvidenceReader
from hcuopt.measurement.evidence import canonical_json_bytes

_DIFF_PATH = re.compile(r"^(?:---|\+\+\+)\s+(?:[ab]/)?([^\t\n]+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class _VerifiedAttempt:
    authority: GeneratorAttempt
    receipt: RunnerExecutionReceipt | None

    @property
    def attempt_id(self):
        return self.authority.attempt_id

    @property
    def generator_id(self) -> str:
        return self.authority.generator_id

    @property
    def attempt_ordinal(self) -> int:
        return self.authority.attempt_number - 1

    @property
    def status(self) -> str:
        if self.receipt is None:
            return self.authority.state
        return self.receipt.execution.status

    @property
    def failure_code(self) -> str | None:
        return self.authority.error_code

    @property
    def cleanup_healthy(self) -> bool:
        return self.receipt is not None and self.receipt.execution.cleanup_status == "verified"

    @property
    def output_bytes(self) -> int:
        if self.receipt is None:
            return self.authority.actual.output_bytes
        return self.receipt.execution.total_output_bytes_consumed

    @property
    def output_tokens(self) -> int:
        if self.receipt is None or self.receipt.execution.tokens_consumed is None:
            return self.authority.actual.tokens
        return self.receipt.execution.tokens_consumed

    @property
    def wall_seconds(self) -> float:
        if self.receipt is None:
            return self.authority.actual.wall_milliseconds / 1000
        return self.receipt.execution.wall_seconds_consumed


class AgentProposalEvidenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _bytes_hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def normalize_patch_v1(raw: bytes) -> bytes:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise AgentProposalEvidenceError("patch_invalid_utf8", "patch is not UTF-8") from exc
    if "\x00" in text:
        raise AgentProposalEvidenceError("patch_invalid", "patch contains NUL")
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.rstrip(" \t")
        if line.startswith("index "):
            continue
        if line.startswith(("--- ", "+++ ")):
            line = line.split("\t", 1)[0]
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return ("\n".join(lines) + "\n").encode()


def normalized_intent_hash(intent: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", intent).casefold().split())
    return _hash({"normalization": "intent-nfkc-casefold-v1", "value": normalized})


def candidate_identity_hash(proposal: CandidateProposal, *, normalized_patch_hash: str) -> str:
    return _hash(
        {
            "schema_version": "m2b-candidate-identity-v1",
            "request_id": str(proposal.request_id),
            "generation_run_id": str(proposal.generation_run_id),
            "normalized_patch_hash": normalized_patch_hash,
            "touched_paths": sorted(proposal.touched_paths),
            "replacement_point": proposal.replacement_point,
            "track": proposal.track,
            "release_mode": proposal.release_mode,
        }
    )


def build_agent_generation_read_model(
    result: AgentProposalVerificationResult,
) -> AgentGenerationReadModel:
    """Copy D verdicts for UI display; never recompute or upgrade authority."""

    return AgentGenerationReadModel(
        status=result.status,
        input_digest=result.input_digest,
        budget=result.budget,
        attempts=[item.model_dump() for item in result.attempts],
        proposals=[
            AgentProposalReadModel(
                proposal_id=item.proposal_id,
                generator_id=item.generator_id,
                status=item.status,
                reason_code=item.reason_code,
                optimization_intent=item.optimization_intent,
                risk_summary=item.risk_summary,
                touched_paths=item.touched_paths,
                patch_uri=item.patch_uri,
            )
            for item in result.proposals
        ],
        failure_codes=result.failure_codes,
        human_review_status=result.human_review_status,
        package_promotion_status=result.package_promotion_status,
        synthetic=result.synthetic,
        environment=result.environment,
        performance_conclusion=result.performance_conclusion,
        formal_intake_allowed=result.formal_intake_allowed,
        automatic_release_allowed=result.automatic_release_allowed,
    )


class AgentProposalVerifier:
    def __init__(self, reader: HashedEvidenceReader) -> None:
        self.reader = reader
        self.provenance = AdapterProvenance(
            profile="m2b-proposal-verifier-v1",
            capability="candidate_proposal_verification",
            adapter_name="AgentProposalVerifier",
            adapter_version=AGENT_PROPOSAL_VERIFIER_VERSION,
            implementation_kind="real",
        )

    def verify(self, context: AgentProposalVerificationContext) -> AgentProposalVerificationResult:
        try:
            return self._verify(context)
        except (EvidenceReadError, ValidationError) as exc:
            code = getattr(exc, "code", "evidence_schema_invalid")
            raise AgentProposalEvidenceError(code, str(exc)) from exc

    def _load(self, ref: object, model: type):
        encoded = self.reader.read_bytes(ref.uri, ref.content_hash)
        return model.model_validate_json(encoded)

    def _load_runner_receipt(
        self,
        attempt: GeneratorAttempt,
        *,
        request_hash: str,
        plan: ApexGenerationPlan,
        generator_artifact_hash: str,
    ) -> RunnerExecutionReceipt | None:
        if attempt.runner_receipt_id is None:
            if attempt.state == "succeeded":
                raise AgentProposalEvidenceError(
                    "runner_receipt_missing",
                    "successful Generation Attempt has no Runner Receipt",
                )
            return None
        assert attempt.runner_receipt_uri is not None
        assert attempt.runner_receipt_hash is not None
        reference = RunnerExecutionReceiptRef(
            receipt_id=attempt.runner_receipt_id,
            attempt_id=attempt.attempt_id,
            generation_run_id=attempt.generation_run_id,
            request_id=attempt.request_id,
            request_hash=request_hash,
            uri=attempt.runner_receipt_uri,
            content_hash=attempt.runner_receipt_hash,
        )
        receipt = self._load(reference, RunnerExecutionReceipt)
        execution = receipt.execution
        if runner_execution_receipt_hash(receipt) != reference.content_hash:
            raise AgentProposalEvidenceError(
                "runner_receipt_hash_mismatch",
                "Runner Receipt domain Hash was not independently reproduced",
            )
        if (
            receipt.receipt_id != reference.receipt_id
            or execution.attempt_id != attempt.attempt_id
            or execution.generation_run_id != attempt.generation_run_id
            or execution.request_id != attempt.request_id
            or execution.request_hash != request_hash
            or execution.plan_id != plan.plan_id
            or execution.generator_id != attempt.generator_id
            or execution.attempt_number != attempt.attempt_number
        ):
            raise AgentProposalEvidenceError(
                "runner_receipt_binding_mismatch",
                "Runner Receipt does not bind the Generation Status Attempt",
            )
        if execution.generator_artifact_hash != generator_artifact_hash:
            raise AgentProposalEvidenceError(
                "runner_artifact_mismatch",
                "Runner Receipt used a different Generator Artifact",
            )
        if attempt.state == "succeeded" and execution.status != "succeeded":
            raise AgentProposalEvidenceError(
                "runner_status_mismatch",
                "successful Generation Attempt binds an unsuccessful Runner Receipt",
            )
        if (
            attempt.state == "failed"
            and execution.status == "succeeded"
            and attempt.error_code != "generation_budget_exceeded"
        ):
            raise AgentProposalEvidenceError(
                "runner_status_mismatch",
                "failed Generation Attempt contradicts its Runner Receipt",
            )
        return receipt

    @staticmethod
    def _validate_budget_ledger(status: GenerationRunStatusView) -> None:
        attempts = {item.attempt_id: item for item in status.attempts}
        entries: dict[tuple[object, str], object] = {}
        for entry in status.budget_ledger:
            key = (entry.attempt_id, entry.entry_type)
            if key in entries or entry.attempt_id not in attempts:
                raise AgentProposalEvidenceError(
                    "budget_ledger_identity_invalid",
                    "Generation Budget Ledger contains a duplicate or unknown Attempt",
                )
            entries[key] = entry
            attempt = attempts[entry.attempt_id]
            if entry.reserved != attempt.reserved:
                raise AgentProposalEvidenceError(
                    "budget_ledger_reservation_mismatch",
                    "Generation Budget Ledger reservation differs from its Attempt",
                )
            if entry.entry_type == "settle" and entry.actual != attempt.actual:
                raise AgentProposalEvidenceError(
                    "budget_ledger_settlement_mismatch",
                    "Generation Budget Ledger settlement differs from its Attempt",
                )
        for attempt in status.attempts:
            if (attempt.attempt_id, "reserve") not in entries:
                raise AgentProposalEvidenceError(
                    "budget_ledger_reservation_missing",
                    "Generation Attempt has no immutable Budget reservation",
                )
            terminal = attempt.state in {"succeeded", "failed", "cancelled"}
            if terminal != ((attempt.attempt_id, "settle") in entries):
                raise AgentProposalEvidenceError(
                    "budget_ledger_settlement_missing",
                    "Generation terminal state and Budget settlement differ",
                )

        reserved = status.run.budget_reserved.model_copy()
        consumed = status.run.budget_consumed.model_copy()
        expected_reserved = type(reserved)()
        expected_consumed = type(consumed)()
        for attempt in status.attempts:
            if attempt.state in {"pending", "running"}:
                expected_reserved = expected_reserved.plus(attempt.reserved)
            if attempt.state in {"succeeded", "failed", "cancelled"}:
                expected_consumed = expected_consumed.plus(attempt.actual)
        if reserved != expected_reserved or consumed != expected_consumed:
            raise AgentProposalEvidenceError(
                "budget_ledger_summary_mismatch",
                "Generation Run Budget summary differs from its immutable Ledger",
            )

    @staticmethod
    def _validate_attempt_usage(
        attempt: GeneratorAttempt,
        receipt: RunnerExecutionReceipt | None,
        batch: CandidateProposalBatch | None,
    ) -> None:
        if receipt is None:
            return
        execution = receipt.execution
        if batch is None:
            if attempt.state == "failed" and attempt.actual != attempt.reserved.model_copy(
                update={"proposals": 0}
            ):
                raise AgentProposalEvidenceError(
                    "attempt_usage_mismatch",
                    "failed Runner Attempt does not carry its conservative Budget charge",
                )
            return
        if execution.tokens_consumed is None:
            raise AgentProposalEvidenceError(
                "runner_usage_missing",
                "successful Runner Receipt omitted authoritative token usage",
            )
        expected = GenerationBudgetUsage(
            attempts=execution.attempts_consumed,
            wall_milliseconds=math.ceil(execution.wall_seconds_consumed * 1000),
            output_bytes=execution.total_output_bytes_consumed,
            tokens=execution.tokens_consumed,
            proposals=len(batch.proposals),
        )
        if attempt.error_code == "generation_budget_exceeded":
            expected = attempt.reserved.model_copy(update={"proposals": 0})
        if attempt.actual != expected:
            raise AgentProposalEvidenceError(
                "attempt_usage_mismatch",
                "Generation Status usage differs from its Runner Receipt",
            )

    @staticmethod
    def _validate_proposal_ref(
        reference: CandidateProposalRef,
        proposal: CandidateProposal,
        attempt: GeneratorAttempt,
        batch: CandidateProposalBatch,
    ) -> None:
        if (
            reference.proposal_hash != candidate_proposal_hash(proposal)
            or reference.generation_run_id != proposal.generation_run_id
            or reference.request_id != proposal.request_id
            or reference.attempt_id != attempt.attempt_id
            or reference.batch_id != batch.batch_id
            or reference.generator_id != proposal.generator_id
            or reference.generator_ordinal != attempt.generator_ordinal
            or reference.proposal_ordinal != proposal.ordinal
            or reference.patch_uri != proposal.patch_uri
            or reference.patch_hash != proposal.patch_hash
            or reference.normalized_patch_hash != proposal.normalized_patch_hash
        ):
            raise AgentProposalEvidenceError(
                "proposal_ref_binding_mismatch",
                "A Candidate Proposal Ref does not bind the immutable C Proposal",
            )

    @staticmethod
    def _validate_proposal_dispositions(
        status: GenerationRunStatusView,
    ) -> None:
        references = sorted(
            status.proposals,
            key=lambda item: (
                item.normalized_patch_hash,
                item.generator_ordinal,
                item.proposal_ordinal,
                item.proposal_hash,
                str(item.proposal_id),
            ),
        )
        retained_by_patch: dict[str, object] = {}
        for reference in references:
            retained_id = retained_by_patch.get(reference.normalized_patch_hash)
            expected_disposition = "retained" if retained_id is None else "duplicate"
            expected_duplicate = None if retained_id is None else retained_id
            if (
                reference.disposition != expected_disposition
                or reference.duplicate_of_proposal_id != expected_duplicate
            ):
                raise AgentProposalEvidenceError(
                    "proposal_disposition_mismatch",
                    "A Proposal disposition was not independently reproduced",
                )
            retained_by_patch.setdefault(reference.normalized_patch_hash, reference.proposal_id)
        if status.run.proposal_count != len(
            status.proposals
        ) or status.run.retained_proposal_count != sum(
            item.disposition == "retained" for item in status.proposals
        ):
            raise AgentProposalEvidenceError(
                "proposal_summary_mismatch",
                "Generation Run Proposal summary differs from its Proposal Refs",
            )

    @staticmethod
    def _validate_dedupe_crosscheck(
        references: tuple[CandidateProposalRef, ...],
        decisions: list[AgentProposalDecision],
    ) -> None:
        decisions_by_id = {item.proposal_id: item for item in decisions}
        references_by_id = {item.proposal_id: item for item in references}
        for reference in references:
            decision = decisions_by_id.get(reference.proposal_id)
            if decision is None or decision.proposal_hash != reference.proposal_hash:
                raise AgentProposalEvidenceError(
                    "proposal_dedupe_mismatch",
                    "D verdict does not bind the A Proposal Ref",
                )
            if reference.disposition != "duplicate":
                continue
            retained = references_by_id.get(reference.duplicate_of_proposal_id)
            if (
                retained is None
                or retained.disposition != "retained"
                or retained.normalized_patch_hash != reference.normalized_patch_hash
                or decision.status != "eliminated"
                or decision.reason_code
                not in {"duplicate_exact_patch", "duplicate_normalized_patch"}
            ):
                raise AgentProposalEvidenceError(
                    "proposal_dedupe_mismatch",
                    "D could not reproduce A's duplicate Proposal relationship",
                )

    def _verify(self, context: AgentProposalVerificationContext) -> AgentProposalVerificationResult:
        knowledge = self._load(context.knowledge, KnowledgeSnapshot)
        request = self._load(context.request, CandidateGenerationRequest)
        plan = self._load(context.plan, ApexGenerationPlan)
        knowledge_hash = knowledge_snapshot_hash(knowledge)
        request_hash = candidate_generation_request_hash(request)
        plan_hash = apex_generation_plan_hash(plan)
        for name, actual, expected in (
            ("knowledge", knowledge_hash, context.knowledge.identity_hash),
            ("request", request_hash, context.request.identity_hash),
            ("plan", plan_hash, context.plan.identity_hash),
        ):
            if actual != expected:
                raise AgentProposalEvidenceError(
                    f"{name}_hash_mismatch", f"{name} identity hash mismatch"
                )
        if (
            request.knowledge_snapshot_id != knowledge.snapshot_id
            or request.knowledge_snapshot_hash != knowledge_hash
        ):
            raise AgentProposalEvidenceError(
                "knowledge_binding_mismatch", "Request is not bound to Knowledge"
            )
        if context.baseline_epoch_id != request.baseline_epoch_id:
            raise AgentProposalEvidenceError(
                "baseline_binding_mismatch",
                "verification Context is bound to another Baseline Epoch",
            )
        if (
            request.generation_run_id != context.generation_run_id
            or plan.generation_run_id != context.generation_run_id
            or plan.request_id != request.request_id
            or plan.request_hash != request_hash
        ):
            raise AgentProposalEvidenceError(
                "plan_binding_mismatch", "Plan/Request/Run binding mismatch"
            )

        status_view = self._load(context.generation_status, GenerationRunStatusView)
        if (
            status_view.run.generation_run_id != context.generation_run_id
            or status_view.run.request != request
            or status_view.run.request_hash != request_hash
            or status_view.run.plan != plan
            or status_view.run.plan_hash != plan_hash
        ):
            raise AgentProposalEvidenceError(
                "generation_status_binding_mismatch",
                "Generation Status does not bind the verified Run authority",
            )

        plan_entries = {item.generator_id: item for item in plan.generators}
        generator_ordinals = {
            item.generator_id: ordinal for ordinal, item in enumerate(plan.generators)
        }
        loaded_attempts = sorted(
            status_view.attempts,
            key=lambda item: (
                generator_ordinals.get(item.generator_id, len(generator_ordinals)),
                item.attempt_number,
                str(item.attempt_id),
            ),
        )
        attempts: list[_VerifiedAttempt] = []
        batches: list[tuple[_VerifiedAttempt, CandidateProposalBatch]] = []
        evidence_uris = [
            context.knowledge.uri,
            context.request.uri,
            context.plan.uri,
            context.generation_status.uri,
        ]
        provenance: list[AdapterProvenance] = [self.provenance]
        attempt_decisions: list[AgentAttemptDecision] = []
        failures: set[str] = set()
        attempt_identities: set[tuple[str, int]] = set()
        attempt_ids: set[object] = set()
        batch_ids: set[object] = set()
        proposal_ids: set[object] = set()
        proposal_refs = {item.proposal_id: item for item in status_view.proposals}
        if len(proposal_refs) != len(status_view.proposals):
            raise AgentProposalEvidenceError(
                "proposal_ref_identity_invalid",
                "Generation Status contains duplicate Proposal Ref identities",
            )
        bound_proposal_refs: set[object] = set()
        for authority_attempt in loaded_attempts:
            identity = (authority_attempt.generator_id, authority_attempt.attempt_number)
            if identity in attempt_identities or authority_attempt.attempt_id in attempt_ids:
                raise AgentProposalEvidenceError(
                    "duplicate_attempt", "Attempt identity is duplicated"
                )
            attempt_identities.add(identity)
            attempt_ids.add(authority_attempt.attempt_id)
            if (
                authority_attempt.generation_run_id != context.generation_run_id
                or authority_attempt.request_id != request.request_id
                or authority_attempt.plan_id != plan.plan_id
                or authority_attempt.generator_id not in plan_entries
            ):
                raise AgentProposalEvidenceError(
                    "attempt_binding_mismatch", "Attempt binding does not match the Plan"
                )
            entry = plan_entries[authority_attempt.generator_id]
            if authority_attempt.attempt_number > entry.max_attempts:
                raise AgentProposalEvidenceError(
                    "attempt_budget_exceeded", "Attempt ordinal exceeds generator budget"
                )

            receipt = self._load_runner_receipt(
                authority_attempt,
                request_hash=request_hash,
                plan=plan,
                generator_artifact_hash=entry.generator_artifact_hash,
            )
            attempt = _VerifiedAttempt(authority=authority_attempt, receipt=receipt)
            attempts.append(attempt)
            if receipt is not None:
                evidence_uris.append(authority_attempt.runner_receipt_uri or "")
                execution = receipt.execution
                runner_provenance = AdapterProvenance(
                    profile=execution.runner_provenance.profile,
                    capability=execution.runner_provenance.capability,
                    adapter_name=execution.runner_provenance.adapter_name,
                    adapter_version=execution.runner_provenance.adapter_version,
                    implementation_kind=execution.runner_provenance.implementation_kind,
                    source_commit=execution.runner_provenance.source_commit,
                )
                if authority_attempt.runner_provenance != runner_provenance:
                    raise AgentProposalEvidenceError(
                        "runner_provenance_mismatch",
                        "Generation Status and Runner Receipt provenance differ",
                    )
                provenance.append(runner_provenance)
            if not attempt.cleanup_healthy:
                failures.add("cleanup_failed")
            if attempt.failure_code:
                failures.add(attempt.failure_code)
            decision_status = "invalid" if not attempt.cleanup_healthy else attempt.status
            attempt_decisions.append(
                AgentAttemptDecision(
                    attempt_id=attempt.attempt_id,
                    generator_id=attempt.generator_id,
                    status=(
                        decision_status
                        if decision_status in {"succeeded", "failed", "timed_out", "invalid"}
                        else "failed"
                    ),
                    reason_code=(
                        "cleanup_failed" if not attempt.cleanup_healthy else attempt.failure_code
                    ),
                    cleanup_healthy=attempt.cleanup_healthy,
                )
            )
            if authority_attempt.batch_id is not None:
                if authority_attempt.batch_uri is None or authority_attempt.batch_hash is None:
                    raise AgentProposalEvidenceError(
                        "batch_reference_missing",
                        "Generation Status omitted the immutable Proposal Batch reference",
                    )
                batch_ref = AgentEvidenceRef(
                    uri=authority_attempt.batch_uri,
                    content_hash=authority_attempt.batch_hash,
                )
                evidence_uris.append(authority_attempt.batch_uri)
                batch = self._load(batch_ref, CandidateProposalBatch)
                if batch.batch_id in batch_ids:
                    raise AgentProposalEvidenceError(
                        "duplicate_batch", "Batch identity is duplicated across Attempts"
                    )
                batch_ids.add(batch.batch_id)
                for proposal in batch.proposals:
                    if proposal.proposal_id in proposal_ids:
                        raise AgentProposalEvidenceError(
                            "duplicate_proposal_identity",
                            "Proposal identity is duplicated across Batches",
                        )
                    proposal_ids.add(proposal.proposal_id)
                if (
                    batch.request_id != request.request_id
                    or batch.generation_run_id != context.generation_run_id
                    or batch.generator_id != attempt.generator_id
                    or batch.attempt_count != attempt.attempt_ordinal + 1
                    or batch.batch_id != authority_attempt.batch_id
                ):
                    raise AgentProposalEvidenceError(
                        "batch_binding_mismatch", "Batch binding does not match its Attempt"
                    )
                if batch.adapter_provenance.capability != "candidate_proposal_generation":
                    raise AgentProposalEvidenceError(
                        "batch_provenance_invalid",
                        "Batch must carry C Candidate Generator provenance",
                    )
                if authority_attempt.batch_status != batch.status or (
                    authority_attempt.state == "succeeded" and batch.status == "failed"
                ):
                    raise AgentProposalEvidenceError(
                        "attempt_batch_status_mismatch",
                        "successful Runner Attempt cannot bind a failed Proposal Batch",
                    )
                if (
                    receipt is None
                    or authority_attempt.raw_output_uri != batch.raw_output_uri
                    or authority_attempt.raw_output_hash != batch.raw_output_hash
                    or receipt.raw_output_uri != batch.raw_output_uri
                    or receipt.raw_output_hash != batch.raw_output_hash
                ):
                    raise AgentProposalEvidenceError(
                        "raw_output_binding_mismatch",
                        "Attempt and Batch bind different raw Generator output",
                    )
                raw = self.reader.read_raw_bytes(batch.raw_output_uri, batch.raw_output_hash)
                if len(raw) != batch.output_bytes or receipt.raw_output_bytes != batch.output_bytes:
                    raise AgentProposalEvidenceError(
                        "output_size_mismatch", "Batch raw output byte count mismatch"
                    )
                if authority_attempt.adapter_provenance != batch.adapter_provenance:
                    raise AgentProposalEvidenceError(
                        "batch_provenance_mismatch",
                        "Generation Status and C Proposal Batch provenance differ",
                    )
                for proposal in batch.proposals:
                    reference = proposal_refs.get(proposal.proposal_id)
                    if reference is None:
                        raise AgentProposalEvidenceError(
                            "proposal_ref_set_mismatch",
                            "C Proposal has no A Candidate Proposal Ref",
                        )
                    self._validate_proposal_ref(reference, proposal, authority_attempt, batch)
                    bound_proposal_refs.add(reference.proposal_id)
                evidence_uris.append(batch.raw_output_uri)
                provenance.append(batch.adapter_provenance)
                batches.append((attempt, batch))
            self._validate_attempt_usage(
                authority_attempt,
                receipt,
                batch if authority_attempt.batch_id is not None else None,
            )

        if bound_proposal_refs != set(proposal_refs):
            raise AgentProposalEvidenceError(
                "proposal_ref_set_mismatch",
                "A Candidate Proposal Refs and C Proposal Batches contain different members",
            )

        self._validate_budget_ledger(status_view)
        self._validate_proposal_dispositions(status_view)
        self._validate_generator_barrier(plan, attempts)
        self._validate_budget(request, plan, attempts, batches)
        decisions = self._dedupe(
            context,
            request,
            batches,
            evidence_uris,
            generator_ordinals=generator_ordinals,
        )
        self._validate_dedupe_crosscheck(status_view.proposals, decisions)
        if sum(item.status == "kept" for item in decisions) == 0:
            failures.add("zero_valid_proposals")
        invalid = any(not item.cleanup_healthy for item in attempts) or any(
            item.status == "invalid" for item in decisions
        )
        status = (
            "invalid"
            if invalid
            else "ready_for_review"
            if any(item.status == "kept" for item in decisions)
            else "no_valid_proposals"
        )
        input_digest = _hash(
            {
                "verifier": AGENT_PROPOSAL_VERIFIER_VERSION,
                "context": _canonical_context(context),
                "knowledge_hash": knowledge_hash,
                "request_hash": request_hash,
                "plan_hash": plan_hash,
                "generation_status": {
                    "run": status_view.run.model_dump(mode="json"),
                    "proposals": [
                        item.model_dump(mode="json")
                        for item in sorted(
                            status_view.proposals,
                            key=lambda member: (
                                member.generator_ordinal,
                                member.proposal_ordinal,
                                str(member.proposal_id),
                            ),
                        )
                    ],
                    "budget_ledger": [
                        item.model_dump(mode="json")
                        for item in sorted(
                            status_view.budget_ledger,
                            key=lambda member: (
                                str(member.attempt_id),
                                member.entry_type,
                                str(member.ledger_entry_id),
                            ),
                        )
                    ],
                },
                "attempts": [
                    {
                        "authority": item.authority.model_dump(mode="json"),
                        "receipt": (
                            item.receipt.model_dump(mode="json")
                            if item.receipt is not None
                            else None
                        ),
                    }
                    for item in attempts
                ],
                "decisions": [item.model_dump(mode="json") for item in decisions],
            }
        )
        return AgentProposalVerificationResult(
            status=status,
            input_digest=input_digest,
            knowledge_hash=knowledge_hash,
            request_hash=request_hash,
            plan_hash=plan_hash,
            budget={
                "attempt_count": len(attempts),
                "output_bytes": sum(item.output_bytes for item in attempts),
                "output_tokens": sum(item.output_tokens for item in attempts),
                "wall_seconds": sum(item.wall_seconds for item in attempts),
                "proposal_count": sum(len(batch.proposals) for _, batch in batches),
            },
            attempts=attempt_decisions,
            proposals=decisions,
            failure_codes=sorted(failures),
            evidence_uris=sorted(set(evidence_uris)),
            adapter_provenance=_unique_provenance(provenance),
            human_review_status="pending",
            package_promotion_status="pending",
            evidence_created_at=plan.created_at,
        )

    @staticmethod
    def _validate_generator_barrier(
        plan: ApexGenerationPlan,
        attempts: list[_VerifiedAttempt],
    ) -> None:
        for entry in plan.generators:
            members = [item for item in attempts if item.generator_id == entry.generator_id]
            ordinals = [item.attempt_ordinal for item in members]
            if ordinals != list(range(len(ordinals))):
                raise AgentProposalEvidenceError(
                    "attempt_sequence_invalid",
                    f"Generator {entry.generator_id} Attempt sequence is incomplete",
                )
            succeeded = [item for item in members if item.status == "succeeded"]
            if len(succeeded) > 1 or (succeeded and succeeded[-1] is not members[-1]):
                raise AgentProposalEvidenceError(
                    "attempt_sequence_invalid",
                    f"Generator {entry.generator_id} continued after success",
                )
            terminal = bool(succeeded) or len(members) == entry.max_attempts
            if not terminal:
                raise AgentProposalEvidenceError(
                    "attempt_barrier_incomplete",
                    f"Generator {entry.generator_id} has not reached a terminal outcome",
                )

    @staticmethod
    def _validate_budget(
        request: CandidateGenerationRequest,
        plan: ApexGenerationPlan,
        attempts: list[_VerifiedAttempt],
        batches: list[tuple[_VerifiedAttempt, CandidateProposalBatch]],
    ) -> None:
        if plan.budget.max_proposals > request.max_proposals:
            raise AgentProposalEvidenceError(
                "request_proposal_budget_exceeded",
                "Plan Proposal budget exceeds the Request limit",
            )
        plan_entries = {item.generator_id: item for item in plan.generators}
        for attempt, batch in batches:
            entry = plan_entries[attempt.generator_id]
            if len(batch.proposals) > entry.max_proposals:
                raise AgentProposalEvidenceError(
                    "generator_proposal_budget_exceeded",
                    "Batch exceeds its Generator Proposal limit",
                )
            if batch.wall_seconds > attempt.wall_seconds:
                raise AgentProposalEvidenceError(
                    "attempt_usage_mismatch",
                    "Batch wall usage exceeds its Runner Attempt receipt",
                )
            if attempt.wall_seconds > entry.timeout_seconds:
                raise AgentProposalEvidenceError(
                    "generator_timeout_budget_exceeded",
                    "Attempt exceeds its Generator timeout",
                )
        usage = (
            len(attempts),
            sum(item.wall_seconds for item in attempts),
            sum(item.output_bytes for item in attempts),
            sum(item.output_tokens for item in attempts),
            sum(len(batch.proposals) for _, batch in batches),
        )
        limits = (
            plan.budget.max_generator_attempts,
            plan.budget.max_wall_seconds,
            plan.budget.max_total_output_bytes,
            plan.budget.max_total_tokens,
            plan.budget.max_proposals,
        )
        if any(value > limit for value, limit in zip(usage, limits, strict=True)):
            raise AgentProposalEvidenceError(
                "generation_budget_exceeded", "verified usage exceeds Generation Budget"
            )

    def _dedupe(
        self,
        context: AgentProposalVerificationContext,
        request: CandidateGenerationRequest,
        batches: list[tuple[_VerifiedAttempt, CandidateProposalBatch]],
        evidence_uris: list[str],
        *,
        generator_ordinals: dict[str, int],
    ) -> list[AgentProposalDecision]:
        seen_exact = set(context.previous_exact_patch_hashes)
        seen_normalized = set(context.previous_normalized_patch_hashes)
        seen_identity = set(context.previous_candidate_identity_hashes)
        seen_intent = set(context.previous_intent_hashes)
        decisions: list[AgentProposalDecision] = []
        ordered_batches = sorted(
            batches,
            key=lambda item: (
                generator_ordinals[item[0].generator_id],
                item[0].attempt_ordinal,
                str(item[0].attempt_id),
                str(item[1].batch_id),
            ),
        )
        for attempt, batch in ordered_batches:
            for proposal in sorted(
                batch.proposals,
                key=lambda item: (
                    item.ordinal,
                    candidate_proposal_hash(item),
                    str(item.proposal_id),
                ),
            ):
                if proposal.replacement_point != request.replacement_point:
                    raise AgentProposalEvidenceError(
                        "proposal_binding_mismatch", "Proposal replacement point mismatches Request"
                    )
                raw = self.reader.read_raw_bytes(proposal.patch_uri, proposal.patch_hash)
                evidence_uris.append(proposal.patch_uri)
                exact_hash = _bytes_hash(raw)
                normalized = normalize_patch_v1(raw)
                normalized_hash = _bytes_hash(normalized)
                if normalized_hash != proposal.normalized_patch_hash:
                    raise AgentProposalEvidenceError(
                        "normalized_patch_hash_mismatch",
                        "Proposal normalized patch hash was not independently reproduced",
                    )
                self._verify_patch_paths(normalized, proposal.touched_paths)
                proposal_hash = candidate_proposal_hash(proposal)
                identity_hash = candidate_identity_hash(
                    proposal, normalized_patch_hash=normalized_hash
                )
                intent_hash = normalized_intent_hash(proposal.optimization_intent)
                reason = "cleanup_failed" if not attempt.cleanup_healthy else None
                if reason is None and exact_hash in seen_exact:
                    reason = "duplicate_exact_patch"
                elif reason is None and normalized_hash in seen_normalized:
                    reason = "duplicate_normalized_patch"
                elif reason is None and identity_hash in seen_identity:
                    reason = "duplicate_candidate_identity"
                elif reason is None and intent_hash in seen_intent:
                    reason = "duplicate_optimization_intent"
                status = (
                    "invalid" if reason == "cleanup_failed" else "eliminated" if reason else "kept"
                )
                decisions.append(
                    AgentProposalDecision(
                        proposal_id=proposal.proposal_id,
                        generator_id=proposal.generator_id,
                        proposal_hash=proposal_hash,
                        exact_patch_hash=exact_hash,
                        normalized_patch_hash=normalized_hash,
                        candidate_identity_hash=identity_hash,
                        intent_hash=intent_hash,
                        status=status,
                        reason_code=reason,
                        patch_uri=proposal.patch_uri,
                        touched_paths=proposal.touched_paths,
                        optimization_intent=proposal.optimization_intent,
                        risk_summary=proposal.risk_summary,
                    )
                )
                seen_exact.add(exact_hash)
                seen_normalized.add(normalized_hash)
                seen_identity.add(identity_hash)
                seen_intent.add(intent_hash)
        return decisions

    @staticmethod
    def _verify_patch_paths(normalized: bytes, declared: tuple[str, ...]) -> None:
        paths = {
            match.group(1)
            for match in _DIFF_PATH.finditer(normalized.decode())
            if match.group(1) != "/dev/null"
        }
        for path in paths:
            candidate = PurePosixPath(path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise AgentProposalEvidenceError("patch_path_invalid", "unsafe patch path")
        if paths != set(declared):
            raise AgentProposalEvidenceError(
                "patch_path_binding_mismatch", "patch paths do not match touched_paths"
            )


def _unique_provenance(
    values: Iterable[AdapterProvenance],
) -> tuple[AdapterProvenance, ...]:
    found: dict[tuple[str, str, str, str], AdapterProvenance] = {}
    for value in values:
        key = (value.profile, value.capability, value.adapter_name, value.adapter_version)
        found[key] = value
    return tuple(found[key] for key in sorted(found))


def _canonical_context(context: AgentProposalVerificationContext) -> dict[str, object]:
    value = context.model_dump(mode="json")
    # The Status content hash authenticates its bytes during loading, while the
    # digest below binds the normalized semantic members.  Excluding the byte
    # hash keeps equivalent database row orderings from changing D's verdict.
    value["generation_status"] = {"uri": value["generation_status"]["uri"]}
    for field in (
        "previous_exact_patch_hashes",
        "previous_normalized_patch_hashes",
        "previous_candidate_identity_hashes",
        "previous_intent_hashes",
    ):
        value[field] = sorted(value[field])
    return value
