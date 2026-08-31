# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from pathlib import PurePosixPath

from pydantic import ValidationError

from hcuopt.agent.identity import (
    apex_generation_plan_hash,
    candidate_generation_request_hash,
    candidate_proposal_hash,
    knowledge_snapshot_hash,
)
from hcuopt.contracts.agent_v1 import (
    ApexGenerationPlan,
    CandidateGenerationRequest,
    CandidateProposal,
    CandidateProposalBatch,
    KnowledgeSnapshot,
)
from hcuopt.contracts.agent_verification_v1 import (
    AGENT_PROPOSAL_VERIFIER_VERSION,
    AgentAttemptDecision,
    AgentAttemptEvidence,
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

        plan_entries = {item.generator_id: item for item in plan.generators}
        generator_ordinals = {
            item.generator_id: ordinal for ordinal, item in enumerate(plan.generators)
        }
        loaded_attempts = [(ref, self._load(ref, AgentAttemptEvidence)) for ref in context.attempts]
        loaded_attempts.sort(
            key=lambda item: (
                generator_ordinals.get(item[1].generator_id, len(generator_ordinals)),
                item[1].attempt_ordinal,
                str(item[1].attempt_id),
                item[0].content_hash,
            )
        )
        attempts: list[AgentAttemptEvidence] = []
        batches: list[tuple[AgentAttemptEvidence, CandidateProposalBatch]] = []
        evidence_uris = [context.knowledge.uri, context.request.uri, context.plan.uri]
        provenance: list[AdapterProvenance] = [self.provenance]
        attempt_decisions: list[AgentAttemptDecision] = []
        failures: set[str] = set()
        attempt_identities: set[tuple[str, int]] = set()
        attempt_ids: set[object] = set()
        batch_ids: set[object] = set()
        proposal_ids: set[object] = set()
        for ref, attempt in loaded_attempts:
            evidence_uris.append(ref.uri)
            attempts.append(attempt)
            identity = (attempt.generator_id, attempt.attempt_ordinal)
            if identity in attempt_identities or attempt.attempt_id in attempt_ids:
                raise AgentProposalEvidenceError(
                    "duplicate_attempt", "Attempt identity is duplicated"
                )
            attempt_identities.add(identity)
            attempt_ids.add(attempt.attempt_id)
            if (
                attempt.generation_run_id != context.generation_run_id
                or attempt.request_id != request.request_id
                or attempt.plan_id != plan.plan_id
                or attempt.generator_id not in plan_entries
            ):
                raise AgentProposalEvidenceError(
                    "attempt_binding_mismatch", "Attempt binding does not match the Plan"
                )
            entry = plan_entries[attempt.generator_id]
            if attempt.attempt_ordinal >= entry.max_attempts:
                raise AgentProposalEvidenceError(
                    "attempt_budget_exceeded", "Attempt ordinal exceeds generator budget"
                )
            cleanup_hash = _hash(attempt.cleanup.model_dump(mode="json"))
            if cleanup_hash != attempt.cleanup_evidence_hash:
                raise AgentProposalEvidenceError(
                    "cleanup_hash_mismatch", "Cleanup evidence hash mismatch"
                )
            if not attempt.cleanup.healthy:
                failures.add("cleanup_failed")
            if attempt.failure_code:
                failures.add(attempt.failure_code)
            status = "invalid" if not attempt.cleanup.healthy else attempt.status
            attempt_decisions.append(
                AgentAttemptDecision(
                    attempt_id=attempt.attempt_id,
                    generator_id=attempt.generator_id,
                    status=status,
                    reason_code=(
                        "cleanup_failed" if not attempt.cleanup.healthy else attempt.failure_code
                    ),
                    cleanup_healthy=attempt.cleanup.healthy,
                )
            )
            provenance.append(attempt.adapter_provenance)
            if attempt.batch:
                evidence_uris.append(attempt.batch.uri)
                batch = self._load(attempt.batch, CandidateProposalBatch)
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
                    or batch.started_at != attempt.started_at
                    or batch.finished_at != attempt.finished_at
                    or batch.adapter_provenance != attempt.adapter_provenance
                ):
                    raise AgentProposalEvidenceError(
                        "batch_binding_mismatch", "Batch binding does not match its Attempt"
                    )
                raw = self.reader.read_raw_bytes(batch.raw_output_uri, batch.raw_output_hash)
                if len(raw) != batch.output_bytes or attempt.output_bytes != batch.output_bytes:
                    raise AgentProposalEvidenceError(
                        "output_size_mismatch", "Batch raw output byte count mismatch"
                    )
                evidence_uris.append(batch.raw_output_uri)
                provenance.append(batch.adapter_provenance)
                batches.append((attempt, batch))

        self._validate_generator_barrier(plan, attempts)
        self._validate_budget(request, plan, attempts, batches)
        decisions = self._dedupe(
            context,
            request,
            batches,
            evidence_uris,
            generator_ordinals=generator_ordinals,
        )
        if sum(item.status == "kept" for item in decisions) == 0:
            failures.add("zero_valid_proposals")
        invalid = any(not item.cleanup.healthy for item in attempts) or any(
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
                "attempts": [item.model_dump(mode="json") for item in attempts],
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
        attempts: list[AgentAttemptEvidence],
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
        attempts: list[AgentAttemptEvidence],
        batches: list[tuple[AgentAttemptEvidence, CandidateProposalBatch]],
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
            if attempt.wall_seconds != batch.wall_seconds:
                raise AgentProposalEvidenceError(
                    "attempt_usage_mismatch",
                    "Attempt and Batch wall usage differ",
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
        batches: list[tuple[AgentAttemptEvidence, CandidateProposalBatch]],
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
                reason = "cleanup_failed" if not attempt.cleanup.healthy else None
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
    value["attempts"] = sorted(
        value["attempts"], key=lambda item: (item["uri"], item["content_hash"])
    )
    for field in (
        "previous_exact_patch_hashes",
        "previous_normalized_patch_hashes",
        "previous_candidate_identity_hashes",
        "previous_intent_hashes",
    ):
        value[field] = sorted(value[field])
    return value
