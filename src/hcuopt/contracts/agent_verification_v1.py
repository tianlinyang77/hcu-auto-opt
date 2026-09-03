# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from hcuopt.contracts.agent_runner_v1 import RunnerProvenance
from hcuopt.contracts.agent_v1 import GenerationBudget
from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.platform_v1 import SHA256_PATTERN, AdapterProvenance

AGENT_PROPOSAL_VERIFIER_VERSION = "m2b-proposal-verifier-v1"


class AgentEvidenceRef(ContractModel):
    uri: str = Field(min_length=1, max_length=4000)
    content_hash: str = Field(pattern=SHA256_PATTERN)


class AgentDomainEvidenceRef(AgentEvidenceRef):
    identity_hash: str = Field(pattern=SHA256_PATTERN)


class AgentProposalVerificationContext(ContractModel):
    task_id: UUID
    target_id: str = Field(min_length=1, max_length=300)
    baseline_epoch_id: UUID
    generation_run_id: UUID
    knowledge: AgentDomainEvidenceRef
    request: AgentDomainEvidenceRef
    plan: AgentDomainEvidenceRef
    generation_status: AgentEvidenceRef
    review_records: tuple[AgentEvidenceRef, ...] = ()
    promotion_receipts: tuple[AgentEvidenceRef, ...] = ()
    previous_exact_patch_hashes: frozenset[str] = frozenset()
    previous_normalized_patch_hashes: frozenset[str] = frozenset()
    previous_candidate_identity_hashes: frozenset[str] = frozenset()
    previous_intent_hashes: frozenset[str] = frozenset()
    synthetic: Literal[True] = True
    environment: Literal["scripted_dev_only"] = "scripted_dev_only"
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False


class AgentBudgetUsage(ContractModel):
    attempt_count: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    wall_seconds: float = Field(ge=0)
    proposal_count: int = Field(ge=0)


class AgentAttemptDecision(ContractModel):
    attempt_id: UUID
    generator_id: str
    attempt_number: int = Field(ge=1, le=8)
    status: Literal["succeeded", "failed", "timed_out", "invalid"]
    reason_code: str | None = None
    cleanup_healthy: bool
    cleanup_status: Literal["verified", "failed", "not_available"]
    cleanup_summary: str | None = None
    runner_receipt_id: UUID | None = None
    runner_receipt_uri: str | None = None
    runner_receipt_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    runner_provenance: RunnerProvenance | None = None
    generator_adapter_profile: str
    generator_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    generator_provenance: AdapterProvenance | None = None
    batch_id: UUID | None = None
    batch_uri: str | None = None
    batch_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    wall_seconds: float = Field(ge=0)
    output_bytes: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    def model_post_init(self, __context: object) -> None:
        receipt_fields = (
            self.runner_receipt_id,
            self.runner_receipt_uri,
            self.runner_receipt_hash,
            self.runner_provenance,
        )
        if any(item is None for item in receipt_fields) and any(
            item is not None for item in receipt_fields
        ):
            raise ValueError("Runner Receipt display fields must be atomic")
        batch_fields = (self.batch_id, self.batch_uri, self.batch_hash)
        if any(item is None for item in batch_fields) and any(
            item is not None for item in batch_fields
        ):
            raise ValueError("Proposal Batch display fields must be atomic")
        if self.cleanup_healthy != (self.cleanup_status == "verified"):
            raise ValueError("cleanup status must match its verified flag")


class AgentProposalDecision(ContractModel):
    proposal_id: UUID
    generator_id: str
    proposal_hash: str = Field(pattern=SHA256_PATTERN)
    exact_patch_hash: str = Field(pattern=SHA256_PATTERN)
    normalized_patch_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_identity_hash: str = Field(pattern=SHA256_PATTERN)
    intent_hash: str = Field(pattern=SHA256_PATTERN)
    status: Literal["kept", "eliminated", "invalid"]
    reason_code: str | None = None
    patch_uri: str
    patch_preview: str = Field(max_length=20_000)
    touched_paths: tuple[str, ...]
    optimization_intent: str
    risk_summary: str


class AgentProposalReviewReadModel(ReadModel):
    review_id: UUID
    decision: Literal["approved", "rejected"]
    reviewer: str
    reason: str
    review_record_hash: str = Field(pattern=SHA256_PATTERN)
    review_evidence_uri: str
    review_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    reviewed_at: datetime


class AgentProposalPromotionReadModel(ReadModel):
    promotion_id: UUID
    promotion_receipt_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_id: UUID
    source_package_ref: CandidateSourcePackageRef
    source_family_hash: str = Field(pattern=SHA256_PATTERN)
    source_family_verification_evidence_uri: str
    source_family_verification_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    source_family_verifier_provenance: AdapterProvenance
    promoted_by: str
    promoted_at: datetime


class AgentProposalLifecycleDecision(ContractModel):
    proposal_id: UUID
    review_status: Literal["pending", "approved", "rejected", "not_applicable"]
    review: AgentProposalReviewReadModel | None = None
    promotion_status: Literal["pending", "promoted", "not_applicable"]
    promotion: AgentProposalPromotionReadModel | None = None
    readiness: Literal["hold"] = "hold"

    def model_post_init(self, __context: object) -> None:
        if self.review_status in {"pending", "not_applicable"}:
            if self.review is not None:
                raise ValueError("unreviewed Proposal lifecycle cannot carry a Review")
        elif self.review is None:
            raise ValueError("reviewed Proposal lifecycle requires its Review authority")
        promoted = self.promotion_status == "promoted"
        if promoted != (self.promotion is not None):
            raise ValueError("promoted Proposal lifecycle requires complete Package authority")
        if self.review_status == "rejected" and self.promotion_status != "not_applicable":
            raise ValueError("rejected Proposal cannot remain pending for promotion")
        if self.review_status == "not_applicable" and self.promotion_status != "not_applicable":
            raise ValueError("eliminated Proposal cannot remain pending for promotion")


class AgentProposalVerificationResult(ContractModel):
    schema_version: Literal["m2b-proposal-verification-v1"] = "m2b-proposal-verification-v1"
    verifier_version: Literal["m2b-proposal-verifier-v1"] = AGENT_PROPOSAL_VERIFIER_VERSION
    status: Literal["ready_for_review", "no_valid_proposals", "invalid"]
    generation_run_id: UUID
    request_id: UUID
    plan_id: UUID
    target_id: str
    baseline_epoch_id: UUID
    replacement_point: str
    input_digest: str = Field(pattern=SHA256_PATTERN)
    knowledge_hash: str = Field(pattern=SHA256_PATTERN)
    request_hash: str = Field(pattern=SHA256_PATTERN)
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    budget_limit: GenerationBudget
    budget: AgentBudgetUsage
    attempts: tuple[AgentAttemptDecision, ...]
    proposals: tuple[AgentProposalDecision, ...]
    lifecycle: tuple[AgentProposalLifecycleDecision, ...]
    failure_codes: tuple[str, ...]
    evidence_uris: tuple[str, ...]
    adapter_provenance: tuple[AdapterProvenance, ...] = Field(min_length=1)
    human_review_status: Literal["pending", "approved", "rejected", "mixed"] = "pending"
    package_promotion_status: Literal["pending", "promoted", "not_applicable", "mixed"] = (
        "pending"
    )
    formal_readiness: Literal["hold"] = "hold"
    evidence_created_at: datetime
    synthetic: Literal[True] = True
    environment: Literal["scripted_dev_only"] = "scripted_dev_only"
    performance_conclusion: Literal["not_measured"] = "not_measured"
    formal_intake_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


class AgentGenerationEvidenceSummaryV1(ContractModel):
    schema_version: Literal["m2b-generation-evidence-summary-v1"] = (
        "m2b-generation-evidence-summary-v1"
    )
    generation_run_id: UUID
    input_digest: str = Field(pattern=SHA256_PATTERN)
    knowledge_hash: str = Field(pattern=SHA256_PATTERN)
    request_hash: str = Field(pattern=SHA256_PATTERN)
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    status: Literal["ready_for_review", "no_valid_proposals", "invalid"]
    budget: AgentBudgetUsage
    kept_proposal_ids: tuple[UUID, ...]
    eliminated_proposal_ids: tuple[UUID, ...]
    attempt_ids: tuple[UUID, ...]
    failure_codes: tuple[str, ...]
    human_review_status: Literal["pending", "approved", "rejected", "mixed"] = "pending"
    package_promotion_status: Literal["pending", "promoted", "not_applicable", "mixed"] = (
        "pending"
    )
    formal_readiness: Literal["hold"] = "hold"
    verifier_version: Literal["m2b-proposal-verifier-v1"] = AGENT_PROPOSAL_VERIFIER_VERSION
    environment: Literal["scripted_dev_only"] = "scripted_dev_only"
    performance_conclusion: Literal["not_measured"] = "not_measured"
    formal_intake_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


class AgentAttemptReadModel(ReadModel):
    attempt_id: UUID
    generator_id: str
    attempt_number: int
    status: str
    reason_code: str | None = None
    cleanup_healthy: bool
    cleanup_status: str
    cleanup_summary: str | None = None
    runner_receipt_id: UUID | None = None
    runner_receipt_uri: str | None = None
    runner_receipt_hash: str | None = None
    runner_provenance: RunnerProvenance | None = None
    generator_adapter_profile: str
    generator_artifact_hash: str
    generator_provenance: AdapterProvenance | None = None
    batch_id: UUID | None = None
    batch_uri: str | None = None
    batch_hash: str | None = None
    wall_seconds: float
    output_bytes: int
    output_tokens: int


class AgentProposalReadModel(ReadModel):
    proposal_id: UUID
    generator_id: str
    status: str
    reason_code: str | None = None
    optimization_intent: str
    risk_summary: str
    touched_paths: tuple[str, ...]
    patch_uri: str
    patch_preview: str
    proposal_hash: str
    exact_patch_hash: str
    normalized_patch_hash: str
    candidate_identity_hash: str
    intent_hash: str
    lifecycle: AgentProposalLifecycleDecision


class AgentGenerationReadModel(ReadModel):
    schema_version: Literal["m2b-agent-generation-read-model-v1"] = (
        "m2b-agent-generation-read-model-v1"
    )
    generated_at: datetime
    status: str
    generation_run_id: UUID
    request_id: UUID
    plan_id: UUID
    target_id: str
    baseline_epoch_id: UUID
    replacement_point: str
    input_digest: str
    knowledge_hash: str
    request_hash: str
    plan_hash: str
    budget_limit: GenerationBudget
    budget: AgentBudgetUsage
    attempts: tuple[AgentAttemptReadModel, ...]
    proposals: tuple[AgentProposalReadModel, ...]
    failure_codes: tuple[str, ...]
    human_review_status: str
    package_promotion_status: str
    formal_readiness: Literal["hold"] = "hold"
    adapter_provenance: tuple[AdapterProvenance, ...]
    synthetic: Literal[True] = True
    environment: Literal["scripted_dev_only"] = "scripted_dev_only"
    performance_conclusion: Literal["not_measured"] = "not_measured"
    formal_intake_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False
