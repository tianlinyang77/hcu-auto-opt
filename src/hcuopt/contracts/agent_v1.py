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


class GeneratorPlanEntry(ContractModel):
    generator_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    adapter_profile: str = Field(min_length=1, max_length=200)
    max_attempts: int = Field(ge=1, le=8)
    max_proposals: int = Field(ge=1, le=8)
    timeout_seconds: int = Field(ge=1, le=7_200)

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
    attempt_count: int = Field(ge=1, le=8)
    wall_seconds: float = Field(ge=0)
    started_at: datetime
    finished_at: datetime
    synthetic: bool
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
