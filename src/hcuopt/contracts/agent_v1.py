# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.platform_v1 import SHA256_PATTERN, AdapterProvenance

AGENT_CONTRACT_VERSION = "m2b-agent-v1"


def _normalized_text(value: str) -> str:
    if value.strip() != value:
        raise ValueError("Agent Contract text must not have outer whitespace")
    return value


def _safe_relative_python_path(value: str) -> str:
    parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
        or not value.endswith((".py", ".pyi"))
    ):
        raise ValueError("Agent Proposal path must be normalized Python source")
    return value


class KnowledgeSourceRef(ContractModel):
    knowledge_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._/-]{2,299}$")
    source_kind: Literal["skill", "repository_document", "operator_evidence"]
    version: str = Field(min_length=1, max_length=200)
    source_uri: str = Field(min_length=1, max_length=4000)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    license_id: str = Field(min_length=1, max_length=200)
    runtime_code_import_allowed: Literal[False] = False

    @field_validator("version", "source_uri", "license_id")
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        return _normalized_text(value)


class KnowledgeSnapshot(ContractModel):
    schema_version: Literal["m2b-knowledge-snapshot-v1"] = "m2b-knowledge-snapshot-v1"
    snapshot_id: UUID
    sources: tuple[KnowledgeSourceRef, ...] = Field(min_length=1, max_length=64)
    created_by: str = Field(min_length=1, max_length=200)
    created_at: datetime
    authority: Literal["advisory_only"] = "advisory_only"
    runtime_code_import_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @field_validator("created_by")
    @classmethod
    def require_normalized_actor(cls, value: str) -> str:
        return _normalized_text(value)

    @model_validator(mode="after")
    def require_unique_sources_and_timezone(self) -> KnowledgeSnapshot:
        identities = [(item.knowledge_id, item.version) for item in self.sources]
        if len(identities) != len(set(identities)):
            raise ValueError("Knowledge Snapshot contains a duplicate source version")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Knowledge Snapshot creation time must be timezone-aware")
        return self


class GenerationBudget(ContractModel):
    max_generator_attempts: int = Field(ge=1, le=64)
    max_wall_seconds: int = Field(ge=1, le=86_400)
    max_total_output_bytes: int = Field(ge=1, le=100_000_000)
    max_total_tokens: int = Field(ge=1, le=100_000_000)
    max_proposals: int = Field(ge=1, le=32)


class GenerationBudgetUsage(ContractModel):
    attempts: int = Field(default=0, ge=0, le=64)
    wall_milliseconds: int = Field(default=0, ge=0, le=86_400_000)
    output_bytes: int = Field(default=0, ge=0, le=100_000_000)
    tokens: int = Field(default=0, ge=0, le=100_000_000)
    proposals: int = Field(default=0, ge=0, le=32)

    def is_zero(self) -> bool:
        return all(
            value == 0
            for value in (
                self.attempts,
                self.wall_milliseconds,
                self.output_bytes,
                self.tokens,
                self.proposals,
            )
        )

    def fits(self, budget: GenerationBudget) -> bool:
        return (
            self.attempts <= budget.max_generator_attempts
            and self.wall_milliseconds <= budget.max_wall_seconds * 1000
            and self.output_bytes <= budget.max_total_output_bytes
            and self.tokens <= budget.max_total_tokens
            and self.proposals <= budget.max_proposals
        )

    def plus(self, other: GenerationBudgetUsage) -> GenerationBudgetUsage:
        return GenerationBudgetUsage(
            attempts=self.attempts + other.attempts,
            wall_milliseconds=self.wall_milliseconds + other.wall_milliseconds,
            output_bytes=self.output_bytes + other.output_bytes,
            tokens=self.tokens + other.tokens,
            proposals=self.proposals + other.proposals,
        )


class GeneratorPlanEntry(ContractModel):
    generator_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    adapter_profile: str = Field(min_length=1, max_length=200)
    generator_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    max_attempts: int = Field(ge=1, le=8)
    max_proposals: int = Field(ge=1, le=8)
    timeout_seconds: int = Field(ge=1, le=7_200)
    max_output_bytes_per_attempt: int = Field(ge=1, le=100_000_000)
    max_tokens_per_attempt: int = Field(ge=1, le=100_000_000)

    @field_validator("adapter_profile")
    @classmethod
    def require_normalized_profile(cls, value: str) -> str:
        return _normalized_text(value)


class ApexGenerationPlan(ContractModel):
    schema_version: Literal["m2b-apex-generation-plan-v1"] = (
        "m2b-apex-generation-plan-v1"
    )
    plan_id: UUID
    generation_run_id: UUID
    request_id: UUID
    request_hash: str = Field(pattern=SHA256_PATTERN)
    generators: tuple[GeneratorPlanEntry, ...] = Field(min_length=1, max_length=8)
    max_concurrency: int = Field(ge=1, le=4)
    budget: GenerationBudget
    dedupe_policy: Literal["normalized_patch_v1"] = "normalized_patch_v1"
    retry_policy: Literal["bounded_same_input_v1"] = "bounded_same_input_v1"
    created_by: str = Field(min_length=1, max_length=200)
    created_at: datetime
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @field_validator("created_by")
    @classmethod
    def require_normalized_actor(cls, value: str) -> str:
        return _normalized_text(value)

    @model_validator(mode="after")
    def require_unique_generators_and_bounded_plan(self) -> ApexGenerationPlan:
        generator_ids = [item.generator_id for item in self.generators]
        if len(generator_ids) != len(set(generator_ids)):
            raise ValueError("Apex Plan contains a duplicate generator identity")
        if self.max_concurrency > len(self.generators):
            raise ValueError("Apex Plan concurrency exceeds its generator count")
        if sum(item.max_attempts for item in self.generators) > self.budget.max_generator_attempts:
            raise ValueError("Apex Plan attempts exceed its generation Budget")
        if (
            sum(item.timeout_seconds * item.max_attempts for item in self.generators)
            > self.budget.max_wall_seconds
        ):
            raise ValueError("Apex Plan attempt time exceeds its generation Budget")
        if (
            sum(
                item.max_output_bytes_per_attempt * item.max_attempts
                for item in self.generators
            )
            > self.budget.max_total_output_bytes
        ):
            raise ValueError("Apex Plan output bytes exceed its generation Budget")
        if (
            sum(
                item.max_tokens_per_attempt * item.max_attempts
                for item in self.generators
            )
            > self.budget.max_total_tokens
        ):
            raise ValueError("Apex Plan tokens exceed its generation Budget")
        if (
            sum(item.max_proposals for item in self.generators)
            > self.budget.max_proposals
        ):
            raise ValueError("Apex Plan proposals exceed its generation Budget")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Apex Plan creation time must be timezone-aware")
        return self


class CandidateGenerationRequest(ContractModel):
    schema_version: Literal["m2b-candidate-generation-request-v1"] = (
        "m2b-candidate-generation-request-v1"
    )
    request_id: UUID
    generation_run_id: UUID
    target_snapshot_id: UUID
    stage0_run_id: UUID
    baseline_epoch_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    configuration_hash: str = Field(pattern=SHA256_PATTERN)
    image_digest: str = Field(pattern=SHA256_PATTERN)
    profiler_evidence_uri: str = Field(min_length=1, max_length=4000)
    profiler_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    knowledge_snapshot_id: UUID
    knowledge_snapshot_hash: str = Field(pattern=SHA256_PATTERN)
    max_proposals: int = Field(ge=1, le=8)
    track: Literal["triton"] = "triton"
    release_mode: Literal["overlay"] = "overlay"
    holdout_access_allowed: Literal[False] = False
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @field_validator("replacement_point", "workload_id", "profiler_evidence_uri")
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        return _normalized_text(value)


class CandidateProposal(ContractModel):
    schema_version: Literal["m2b-candidate-proposal-v1"] = "m2b-candidate-proposal-v1"
    proposal_id: UUID
    request_id: UUID
    request_hash: str = Field(pattern=SHA256_PATTERN)
    generation_run_id: UUID
    generator_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    ordinal: int = Field(ge=0, le=31)
    optimization_intent: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=10_000)
    risk_summary: str = Field(min_length=1, max_length=4000)
    patch_uri: str = Field(min_length=1, max_length=4000)
    patch_hash: str = Field(pattern=SHA256_PATTERN)
    normalized_patch_hash: str = Field(pattern=SHA256_PATTERN)
    touched_paths: tuple[str, ...] = Field(min_length=1, max_length=4)
    replacement_point: str = Field(min_length=1, max_length=1000)
    track: Literal["triton"] = "triton"
    release_mode: Literal["overlay"] = "overlay"
    review_required: Literal[True] = True
    formal_intake_allowed: Literal[False] = False
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @field_validator(
        "optimization_intent",
        "rationale",
        "risk_summary",
        "patch_uri",
        "replacement_point",
    )
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        return _normalized_text(value)

    @field_validator("touched_paths")
    @classmethod
    def require_safe_unique_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_safe_relative_python_path(item) for item in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Candidate Proposal contains duplicate touched paths")
        return normalized


class CandidateProposalBatch(ContractModel):
    schema_version: Literal["m2b-candidate-proposal-batch-v1"] = (
        "m2b-candidate-proposal-batch-v1"
    )
    batch_id: UUID
    request_id: UUID
    request_hash: str = Field(pattern=SHA256_PATTERN)
    generation_run_id: UUID
    generator_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    adapter_provenance: AdapterProvenance
    status: Literal["succeeded", "partial", "failed"]
    proposals: tuple[CandidateProposal, ...] = Field(default=(), max_length=8)
    raw_output_uri: str = Field(min_length=1, max_length=4000)
    raw_output_hash: str = Field(pattern=SHA256_PATTERN)
    output_bytes: int = Field(ge=0)
    token_count: int = Field(default=0, ge=0, le=100_000_000)
    attempt_count: int = Field(ge=1, le=8)
    wall_seconds: float = Field(ge=0)
    started_at: datetime
    finished_at: datetime
    synthetic: bool
    error_code: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_]{2,99}$"
    )
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_coherent_batch(self) -> CandidateProposalBatch:
        for timestamp in (self.started_at, self.finished_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("Candidate Proposal Batch times must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("Candidate Proposal Batch finishes before it starts")
        if self.status == "succeeded" and not self.proposals:
            raise ValueError("successful Candidate Proposal Batch must contain proposals")
        if self.status == "partial" and not self.proposals:
            raise ValueError("partial Candidate Proposal Batch must contain proposals")
        if self.status == "failed" and self.proposals:
            raise ValueError("failed Candidate Proposal Batch cannot contain proposals")
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("Candidate Proposal Batch error fields must be atomic")
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful Candidate Proposal Batch cannot contain an error")
        if self.status in {"partial", "failed"} and self.error_code is None:
            raise ValueError("partial/failed Candidate Proposal Batch requires one safe error")
        if any(item.request_id != self.request_id for item in self.proposals):
            raise ValueError("Candidate Proposal Batch contains a cross-request proposal")
        if any(item.request_hash != self.request_hash for item in self.proposals):
            raise ValueError("Candidate Proposal Batch contains a cross-request Hash proposal")
        if any(item.generation_run_id != self.generation_run_id for item in self.proposals):
            raise ValueError("Candidate Proposal Batch contains a cross-run proposal")
        if any(item.generator_id != self.generator_id for item in self.proposals):
            raise ValueError("Candidate Proposal Batch contains a cross-generator proposal")
        proposal_ids = [item.proposal_id for item in self.proposals]
        ordinals = [item.ordinal for item in self.proposals]
        if len(proposal_ids) != len(set(proposal_ids)) or len(ordinals) != len(set(ordinals)):
            raise ValueError("Candidate Proposal Batch contains duplicate member identities")
        if self.adapter_provenance.implementation_kind == "fake" and not self.synthetic:
            raise ValueError("fake Candidate Generator output must be synthetic")
        return self


class CandidateProposalReviewRecord(ContractModel):
    """Human decision over one fully bound, independently replayable Proposal."""

    schema_version: Literal["m2b-candidate-proposal-review-v1"] = (
        "m2b-candidate-proposal-review-v1"
    )
    review_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=300)
    proposal_id: UUID
    proposal_hash: str = Field(pattern=SHA256_PATTERN)
    request_id: UUID
    request_hash: str = Field(pattern=SHA256_PATTERN)
    generation_run_id: UUID
    patch_uri: str = Field(min_length=1, max_length=4000)
    patch_hash: str = Field(pattern=SHA256_PATTERN)
    normalized_patch_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_epoch_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)
    review_evidence_uri: str = Field(min_length=1, max_length=4000)
    review_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    reviewed_at: datetime
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    formal_intake_allowed: Literal[False] = False
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @field_validator(
        "idempotency_key",
        "patch_uri",
        "replacement_point",
        "reviewer",
        "reason",
        "review_evidence_uri",
    )
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        return _normalized_text(value)

    @model_validator(mode="after")
    def require_timezone_aware_review(self) -> CandidateProposalReviewRecord:
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("Candidate Proposal review time must be timezone-aware")
        return self


class CandidateProposalPromotionReceipt(ContractModel):
    """Receipt linking an approved Proposal to existing M2a package authority."""

    schema_version: Literal["m2b-candidate-proposal-promotion-v1"] = (
        "m2b-candidate-proposal-promotion-v1"
    )
    promotion_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=300)
    proposal_id: UUID
    proposal_hash: str = Field(pattern=SHA256_PATTERN)
    request_id: UUID
    request_hash: str = Field(pattern=SHA256_PATTERN)
    generation_run_id: UUID
    patch_uri: str = Field(min_length=1, max_length=4000)
    patch_hash: str = Field(pattern=SHA256_PATTERN)
    normalized_patch_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_epoch_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    review: CandidateProposalReviewRecord
    review_record_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_id: UUID
    source_package_ref: CandidateSourcePackageRef
    source_family_hash: str = Field(pattern=SHA256_PATTERN)
    source_family_verification_evidence_uri: str = Field(min_length=1, max_length=4000)
    source_family_verification_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    source_family_verifier_provenance: AdapterProvenance
    promoted_by: str = Field(min_length=1, max_length=200)
    promoted_at: datetime
    promotion_outcome: Literal["source_package_and_family_verified"] = (
        "source_package_and_family_verified"
    )
    candidate_authority: Literal["existing_m2a_source_package_and_family_only"] = (
        "existing_m2a_source_package_and_family_only"
    )
    synthetic: bool
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    formal_intake_allowed: Literal[False] = False
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @field_validator(
        "idempotency_key",
        "patch_uri",
        "replacement_point",
        "source_family_verification_evidence_uri",
        "promoted_by",
    )
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        return _normalized_text(value)

    @model_validator(mode="after")
    def require_approved_bound_review(self) -> CandidateProposalPromotionReceipt:
        if self.promoted_at.tzinfo is None or self.promoted_at.utcoffset() is None:
            raise ValueError("Candidate Proposal promotion time must be timezone-aware")
        if self.review.decision != "approved":
            raise ValueError("Candidate Proposal promotion requires an approved review")
        expected = (
            self.proposal_id,
            self.proposal_hash,
            self.request_id,
            self.request_hash,
            self.generation_run_id,
            self.patch_uri,
            self.patch_hash,
            self.normalized_patch_hash,
            self.baseline_epoch_id,
            self.baseline_source_hash,
            self.hotspot_id,
            self.replacement_point,
        )
        actual = (
            self.review.proposal_id,
            self.review.proposal_hash,
            self.review.request_id,
            self.review.request_hash,
            self.review.generation_run_id,
            self.review.patch_uri,
            self.review.patch_hash,
            self.review.normalized_patch_hash,
            self.review.baseline_epoch_id,
            self.review.baseline_source_hash,
            self.review.hotspot_id,
            self.review.replacement_point,
        )
        if actual != expected:
            raise ValueError("Candidate Proposal promotion drifted from its approved review")
        if (
            self.source_family_verifier_provenance.implementation_kind == "fake"
            and not self.synthetic
        ):
            raise ValueError("fake source Family verification must remain synthetic")
        return self


class GenerationBudgetLedgerEntry(ContractModel):
    schema_version: Literal["m2b-generation-budget-ledger-v1"] = (
        "m2b-generation-budget-ledger-v1"
    )
    ledger_entry_id: UUID
    generation_run_id: UUID
    attempt_id: UUID
    entry_type: Literal["reserve", "settle", "release"]
    reserved: GenerationBudgetUsage
    actual: GenerationBudgetUsage = Field(default_factory=GenerationBudgetUsage)
    idempotency_key: str = Field(min_length=8, max_length=300)
    created_at: datetime
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_budget_entry_semantics(self) -> GenerationBudgetLedgerEntry:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("generation Budget Ledger time must be timezone-aware")
        if self.reserved.attempts != 1:
            raise ValueError("generation Budget reservation must cover one attempt")
        if self.entry_type in {"reserve", "release"} and not self.actual.is_zero():
            raise ValueError("generation Budget reserve/release cannot report actual usage")
        if self.entry_type == "settle":
            if self.actual.attempts != 1:
                raise ValueError("generation Budget settlement must consume one attempt")
            if any(
                actual > reserved
                for actual, reserved in (
                    (self.actual.wall_milliseconds, self.reserved.wall_milliseconds),
                    (self.actual.output_bytes, self.reserved.output_bytes),
                    (self.actual.tokens, self.reserved.tokens),
                    (self.actual.proposals, self.reserved.proposals),
                )
            ):
                raise ValueError("generation Budget actual usage exceeds its reservation")
        return self


class CandidateProposalRef(ContractModel):
    schema_version: Literal["m2b-candidate-proposal-ref-v1"] = (
        "m2b-candidate-proposal-ref-v1"
    )
    proposal_id: UUID
    proposal_hash: str = Field(pattern=SHA256_PATTERN)
    generation_run_id: UUID
    request_id: UUID
    attempt_id: UUID
    batch_id: UUID
    generator_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    generator_ordinal: int = Field(ge=0, le=7)
    proposal_ordinal: int = Field(ge=0, le=31)
    patch_uri: str = Field(min_length=1, max_length=4000)
    patch_hash: str = Field(pattern=SHA256_PATTERN)
    normalized_patch_hash: str = Field(pattern=SHA256_PATTERN)
    disposition: Literal["pending", "retained", "duplicate"]
    duplicate_of_proposal_id: UUID | None = None
    review_required: Literal[True] = True
    formal_intake_allowed: Literal[False] = False
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_dedupe_binding(self) -> CandidateProposalRef:
        if (self.disposition == "duplicate") != (self.duplicate_of_proposal_id is not None):
            raise ValueError("duplicate Proposal Ref requires its retained identity")
        if self.duplicate_of_proposal_id == self.proposal_id:
            raise ValueError("Proposal Ref cannot duplicate itself")
        return self


class GeneratorAttempt(ContractModel):
    schema_version: Literal["m2b-generator-attempt-v1"] = "m2b-generator-attempt-v1"
    attempt_id: UUID
    generation_run_id: UUID
    plan_id: UUID
    request_id: UUID
    generator_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    generator_ordinal: int = Field(ge=0, le=7)
    attempt_number: int = Field(ge=1, le=8)
    adapter_profile: str = Field(min_length=1, max_length=200)
    state: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    reserved: GenerationBudgetUsage
    actual: GenerationBudgetUsage = Field(default_factory=GenerationBudgetUsage)
    worker_id: str | None = Field(default=None, min_length=1, max_length=200)
    claim_token: UUID | None = None
    lease_expires_at: datetime | None = None
    batch_id: UUID | None = None
    batch_uri: str | None = Field(default=None, min_length=1, max_length=4000)
    batch_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    batch_status: Literal["succeeded", "partial", "failed"] | None = None
    raw_output_uri: str | None = Field(default=None, min_length=1, max_length=4000)
    raw_output_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    adapter_provenance: AdapterProvenance | None = None
    runner_receipt_id: UUID | None = None
    runner_receipt_uri: str | None = Field(default=None, min_length=1, max_length=4000)
    runner_receipt_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    runner_receipt_schema_version: Literal["m2b-runner-execution-receipt-v1"] | None = None
    runner_provenance: AdapterProvenance | None = None
    error_code: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_]{2,99}$"
    )
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    dev_only: Literal[True] = True
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def require_attempt_state_binding(self) -> GeneratorAttempt:
        for timestamp in (
            self.created_at,
            self.updated_at,
            self.lease_expires_at,
            self.started_at,
            self.finished_at,
        ):
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError("Generator Attempt times must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Generator Attempt update precedes creation")
        claimed = all(
            value is not None
            for value in (
                self.worker_id,
                self.claim_token,
                self.lease_expires_at,
                self.started_at,
            )
        )
        if any(
            value is not None
            for value in (
                self.worker_id,
                self.claim_token,
                self.lease_expires_at,
                self.started_at,
            )
        ) != claimed:
            raise ValueError("Generator Attempt claim fields must resolve atomically")
        terminal = self.state in {"succeeded", "failed", "cancelled"}
        if self.state == "pending" and (claimed or not self.actual.is_zero()):
            raise ValueError("pending Generator Attempt cannot contain execution state")
        if self.state == "running" and (not claimed or self.finished_at is not None):
            raise ValueError("running Generator Attempt requires one live claim")
        if terminal != (self.finished_at is not None):
            raise ValueError("terminal Generator Attempt requires finished_at")
        if self.state == "succeeded" and (
            not claimed or self.batch_id is None or self.batch_hash is None
        ):
            raise ValueError("successful Generator Attempt requires its Proposal Batch")
        batch_fields = (
            self.batch_id,
            self.batch_hash,
            self.batch_status,
            self.raw_output_uri,
            self.raw_output_hash,
            self.adapter_provenance,
        )
        if any(value is not None for value in batch_fields) != all(
            value is not None for value in batch_fields
        ):
            raise ValueError("Generator Attempt Proposal Batch fields must be atomic")
        if self.state in {"pending", "running", "cancelled"} and self.batch_id is not None:
            raise ValueError("unfinished/cancelled Generator Attempt cannot bind a Proposal Batch")
        if self.adapter_provenance is not None and (
            self.adapter_provenance.profile != self.adapter_profile
        ):
            raise ValueError("Generator Attempt Adapter Provenance differs from its Plan")
        receipt_fields = (
            self.runner_receipt_id,
            self.runner_receipt_uri,
            self.runner_receipt_hash,
            self.runner_receipt_schema_version,
            self.runner_provenance,
        )
        if any(value is not None for value in receipt_fields) != all(
            value is not None for value in receipt_fields
        ):
            raise ValueError("Generator Attempt Runner Receipt fields must be atomic")
        if self.batch_uri is not None and self.batch_id is None:
            raise ValueError("Generator Attempt Batch URI requires a Proposal Batch")
        if self.state == "succeeded" and self.runner_receipt_id is not None:
            if self.batch_uri is None:
                raise ValueError("Receipt-backed Generator Attempt requires its Batch URI")
        if self.runner_provenance is not None and (
            self.runner_provenance.capability != "agent_runner"
        ):
            raise ValueError("Generator Attempt Runner Provenance is not an Agent Runner")
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("Generator Attempt error fields must be atomic")
        if (self.state == "failed") != (self.error_code is not None):
            raise ValueError("failed Generator Attempt requires one safe error")
        if self.state in {"succeeded", "failed"} and self.actual.attempts != 1:
            raise ValueError("executed Generator Attempt must consume one attempt")
        if self.finished_at is not None and self.started_at is not None:
            if self.finished_at < self.started_at:
                raise ValueError("Generator Attempt finishes before it starts")
        return self


class GenerationRun(ContractModel):
    schema_version: Literal["m2b-generation-run-v1"] = "m2b-generation-run-v1"
    generation_run_id: UUID
    request: CandidateGenerationRequest
    request_hash: str = Field(pattern=SHA256_PATTERN)
    plan: ApexGenerationPlan
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=300)
    state: Literal[
        "created",
        "running",
        "awaiting_review",
        "completed",
        "failed",
        "cancelled",
    ]
    planned_generator_count: int = Field(ge=1, le=8)
    attempt_count: int = Field(ge=0, le=64)
    terminal_attempt_count: int = Field(ge=0, le=64)
    terminal_generator_count: int = Field(ge=0, le=8)
    proposal_count: int = Field(ge=0, le=32)
    retained_proposal_count: int = Field(ge=0, le=32)
    budget_reserved: GenerationBudgetUsage = Field(default_factory=GenerationBudgetUsage)
    budget_consumed: GenerationBudgetUsage = Field(default_factory=GenerationBudgetUsage)
    review_evidence_uri: str | None = Field(default=None, min_length=1, max_length=4000)
    review_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    error_code: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_]{2,99}$"
    )
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    dev_only: Literal[True] = True
    formal_intake_allowed: Literal[False] = False
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def require_generation_run_binding(self) -> GenerationRun:
        for timestamp in (self.created_at, self.updated_at, self.finished_at):
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError("Generation Run times must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Generation Run update precedes creation")
        if self.generation_run_id != self.request.generation_run_id:
            raise ValueError("Generation Run and Request identities differ")
        if self.generation_run_id != self.plan.generation_run_id:
            raise ValueError("Generation Run and Plan identities differ")
        if self.plan.request_id != self.request.request_id:
            raise ValueError("Generation Plan and Request identities differ")
        if self.plan.request_hash != self.request_hash:
            raise ValueError("Generation Plan does not bind the Request Hash")
        if self.planned_generator_count != len(self.plan.generators):
            raise ValueError("Generation Run generator count differs from its Plan")
        if self.terminal_attempt_count > self.attempt_count:
            raise ValueError("Generation Run terminal attempts exceed all attempts")
        if self.terminal_generator_count > self.planned_generator_count:
            raise ValueError("Generation Run terminal generators exceed its Plan")
        if self.retained_proposal_count > self.proposal_count:
            raise ValueError("Generation Run retained Proposals exceed all Proposals")
        if not self.budget_reserved.fits(self.plan.budget):
            raise ValueError("Generation Run reservations exceed its generation Budget")
        if not self.budget_consumed.fits(self.plan.budget):
            raise ValueError("Generation Run consumption exceeds its generation Budget")
        if not self.budget_reserved.plus(self.budget_consumed).fits(self.plan.budget):
            raise ValueError("Generation Run active and consumed Budget exceeds its limit")
        if (self.review_evidence_uri is None) != (self.review_evidence_hash is None):
            raise ValueError("Generation Run review evidence fields must be atomic")
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("Generation Run error fields must be atomic")
        if (self.state == "failed") != (self.error_code is not None):
            raise ValueError("failed Generation Run requires one safe error")
        terminal = self.state in {"completed", "failed", "cancelled"}
        if terminal != (self.finished_at is not None):
            raise ValueError("terminal Generation Run requires finished_at")
        if self.state in {"awaiting_review", "completed"} and (
            self.terminal_generator_count != self.planned_generator_count
            or self.retained_proposal_count < 1
        ):
            raise ValueError("reviewable Generation Run requires retained Proposals")
        if self.state == "completed" and self.review_evidence_uri is None:
            raise ValueError("completed Generation Run requires review evidence")
        if self.state != "completed" and self.review_evidence_uri is not None:
            raise ValueError("only completed Generation Run can bind review evidence")
        return self


class GenerationRunStartRequest(ContractModel):
    schema_version: Literal["m2b-generation-run-start-request-v1"] = (
        "m2b-generation-run-start-request-v1"
    )
    request: CandidateGenerationRequest
    plan: ApexGenerationPlan
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=300)
    dev_only: Literal[True] = True
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


class GenerationRunStartView(GenerationRun):
    replayed: bool


class GenerationAttemptClaim(ContractModel):
    schema_version: Literal["m2b-generation-attempt-claim-v1"] = (
        "m2b-generation-attempt-claim-v1"
    )
    run: GenerationRun
    attempt: GeneratorAttempt
    generator: GeneratorPlanEntry
    request: CandidateGenerationRequest
    dev_only: Literal[True] = True
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_claim_binding(self) -> GenerationAttemptClaim:
        if self.attempt.state != "running":
            raise ValueError("Generation Attempt Claim requires a running attempt")
        if self.run.generation_run_id != self.attempt.generation_run_id:
            raise ValueError("Generation Attempt Claim crosses Runs")
        if self.attempt.plan_id != self.run.plan.plan_id:
            raise ValueError("Generation Attempt Claim crosses Plans")
        if self.attempt.request_id != self.run.request.request_id:
            raise ValueError("Generation Attempt Claim crosses Requests")
        if self.request != self.run.request:
            raise ValueError("Generation Attempt Claim Request differs from its Run")
        if self.generator.generator_id != self.attempt.generator_id:
            raise ValueError("Generation Attempt Claim generator differs from its Attempt")
        if self.run.plan.generators[self.attempt.generator_ordinal] != self.generator:
            raise ValueError("Generation Attempt Claim generator ordinal is invalid")
        return self


class GenerationRunStatusView(ContractModel):
    schema_version: Literal["m2b-generation-run-status-v1"] = (
        "m2b-generation-run-status-v1"
    )
    run: GenerationRun
    attempts: tuple[GeneratorAttempt, ...]
    proposals: tuple[CandidateProposalRef, ...]
    budget_ledger: tuple[GenerationBudgetLedgerEntry, ...] = ()
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_status_binding(self) -> GenerationRunStatusView:
        if any(
            item.generation_run_id != self.run.generation_run_id
            for item in (*self.attempts, *self.proposals, *self.budget_ledger)
        ):
            raise ValueError("Generation Run Status contains a cross-run member")
        return self
