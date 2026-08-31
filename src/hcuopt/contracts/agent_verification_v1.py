# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN, AdapterProvenance

AGENT_PROPOSAL_VERIFIER_VERSION = "m2b-proposal-verifier-v1"


class AgentEvidenceRef(ContractModel):
    uri: str = Field(min_length=1, max_length=4000)
    content_hash: str = Field(pattern=SHA256_PATTERN)


class AgentDomainEvidenceRef(AgentEvidenceRef):
    identity_hash: str = Field(pattern=SHA256_PATTERN)


class AgentCleanupEvidence(ContractModel):
    process_reaped: bool
    sandbox_removed: bool
    output_sealed: bool
    failure_codes: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return (
            self.process_reaped
            and self.sandbox_removed
            and self.output_sealed
            and not self.failure_codes
        )


class AgentAttemptEvidence(ContractModel):
    schema_version: Literal["m2b-agent-attempt-evidence-v1"] = "m2b-agent-attempt-evidence-v1"
    attempt_id: UUID
    generation_run_id: UUID
    request_id: UUID
    plan_id: UUID
    generator_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    attempt_ordinal: int = Field(ge=0, le=7)
    status: Literal["succeeded", "failed", "timed_out"]
    failure_code: (
        Literal["runner_timeout", "runner_failed", "cleanup_failed", "invalid_output"] | None
    ) = None
    batch: AgentEvidenceRef | None = None
    output_bytes: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    wall_seconds: float = Field(ge=0)
    cleanup: AgentCleanupEvidence
    cleanup_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    started_at: datetime
    finished_at: datetime
    adapter_provenance: AdapterProvenance
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def require_coherent_attempt(self) -> AgentAttemptEvidence:
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("Agent attempt timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("Agent attempt finishes before it starts")
        if self.status == "succeeded":
            if self.batch is None or self.failure_code is not None:
                raise ValueError("successful attempt requires a Batch and no failure")
        elif self.batch is not None or self.failure_code is None:
            raise ValueError("unsuccessful attempt requires a failure and no Batch")
        if self.status == "timed_out" and self.failure_code != "runner_timeout":
            raise ValueError("timed-out attempt requires runner_timeout")
        return self


class AgentProposalVerificationContext(ContractModel):
    task_id: UUID
    target_id: str = Field(min_length=1, max_length=300)
    baseline_epoch_id: UUID
    generation_run_id: UUID
    knowledge: AgentDomainEvidenceRef
    request: AgentDomainEvidenceRef
    plan: AgentDomainEvidenceRef
    attempts: tuple[AgentEvidenceRef, ...] = Field(min_length=1, max_length=64)
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
    status: Literal["succeeded", "failed", "timed_out", "invalid"]
    reason_code: str | None = None
    cleanup_healthy: bool


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
    touched_paths: tuple[str, ...]
    optimization_intent: str
    risk_summary: str


class AgentProposalVerificationResult(ContractModel):
    schema_version: Literal["m2b-proposal-verification-v1"] = "m2b-proposal-verification-v1"
    verifier_version: Literal["m2b-proposal-verifier-v1"] = AGENT_PROPOSAL_VERIFIER_VERSION
    status: Literal["ready_for_review", "no_valid_proposals", "invalid"]
    input_digest: str = Field(pattern=SHA256_PATTERN)
    knowledge_hash: str = Field(pattern=SHA256_PATTERN)
    request_hash: str = Field(pattern=SHA256_PATTERN)
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    budget: AgentBudgetUsage
    attempts: tuple[AgentAttemptDecision, ...]
    proposals: tuple[AgentProposalDecision, ...]
    failure_codes: tuple[str, ...]
    evidence_uris: tuple[str, ...]
    adapter_provenance: tuple[AdapterProvenance, ...] = Field(min_length=1)
    human_review_status: Literal["pending"] = "pending"
    package_promotion_status: Literal["pending"] = "pending"
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
    human_review_status: Literal["pending"] = "pending"
    package_promotion_status: Literal["pending"] = "pending"
    verifier_version: Literal["m2b-proposal-verifier-v1"] = AGENT_PROPOSAL_VERIFIER_VERSION
    environment: Literal["scripted_dev_only"] = "scripted_dev_only"
    performance_conclusion: Literal["not_measured"] = "not_measured"
    formal_intake_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


class AgentAttemptReadModel(ReadModel):
    attempt_id: UUID
    generator_id: str
    status: str
    reason_code: str | None = None
    cleanup_healthy: bool


class AgentProposalReadModel(ReadModel):
    proposal_id: UUID
    generator_id: str
    status: str
    reason_code: str | None = None
    optimization_intent: str
    risk_summary: str
    touched_paths: tuple[str, ...]
    patch_uri: str


class AgentGenerationReadModel(ReadModel):
    status: str
    input_digest: str
    budget: AgentBudgetUsage
    attempts: tuple[AgentAttemptReadModel, ...]
    proposals: tuple[AgentProposalReadModel, ...]
    failure_codes: tuple[str, ...]
    human_review_status: str
    package_promotion_status: str
    synthetic: bool
    environment: str
    performance_conclusion: str
    formal_intake_allowed: bool
    automatic_release_allowed: bool
