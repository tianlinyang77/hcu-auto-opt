# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.m2 import CandidateSourcePackageRef, RoundBudget
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN

M2A_FORMAL_READINESS_SCHEMA_VERSION = "m2a-formal-readiness-v1"

FORMAL_READINESS_GATE_CODES = (
    "business_candidate_family",
    "formal_authority_persistence",
    "formal_evidence_finalizer",
    "formal_measurement_adapter",
    "formal_operator_profiles",
    "formal_plan_compiler",
    "formal_start_intent",
    "historical_single_candidate_provenance",
    "historical_stage0_authority",
    "owner_window_authorization",
    "round_signoff_outbox",
    "scripted_barrier_fwer_evidence",
    "target_lock_refresh",
)

FormalReadinessOwner = Literal["A", "B", "C", "D", "project_owner"]
FormalReadinessGateStatus = Literal["pass", "hold", "block"]


class FormalReadinessEvidenceRef(ContractModel):
    path: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    digest_mode: Literal["raw_bytes", "text_lf"]
    evidence_type: str = Field(min_length=1, max_length=200)

    @field_validator("path")
    @classmethod
    def require_repository_relative_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("Formal readiness evidence paths must use POSIX separators")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("Formal readiness evidence must stay repository-relative")
        return value


class FormalProfileDraft(ContractModel):
    profile_version: int = Field(ge=1)
    registration_state: Literal[
        "draft_unregistered",
        "implementation_ready_unregistered",
    ] = "draft_unregistered"
    run_mode: Literal["formal"] = "formal"
    project_mode: Literal["degraded_manual_intake"] = "degraded_manual_intake"
    target_profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    workload_profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    measurement_profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    target_id: str = Field(min_length=1, max_length=200)
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_snapshot_id: UUID
    stage0_run_id: UUID
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_epoch_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    workload_id: str = Field(min_length=1, max_length=200)
    image_digest: str = Field(pattern=SHA256_PATTERN)
    adapter_profile: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    candidate_kind: Literal["business"] = "business"
    candidate_family_state: Literal["missing", "frozen"]
    expected_candidate_count: int = Field(ge=2, le=4)
    max_promoted: int = Field(ge=1, le=2)
    family_alpha: float = Field(gt=0, lt=1)
    budget_state: Literal["pending_b_review", "accepted"]
    budget: RoundBudget
    candidate_packages: tuple[CandidateSourcePackageRef, ...] = Field(
        default=(), max_length=4
    )
    candidate_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_candidate_family_shape(self) -> FormalProfileDraft:
        if self.max_promoted > self.expected_candidate_count:
            raise ValueError("max_promoted cannot exceed expected Candidate count")
        if self.budget.max_candidates != self.expected_candidate_count:
            raise ValueError("Formal draft Budget must match expected Candidate count")
        frozen = self.candidate_family_state == "frozen"
        if frozen != (self.candidate_family_hash is not None):
            raise ValueError("frozen Candidate Family requires its Family Hash")
        if frozen and len(self.candidate_packages) != self.expected_candidate_count:
            raise ValueError("frozen Candidate Family must contain every expected member")
        if not frozen and self.candidate_packages:
            raise ValueError("missing Candidate Family cannot carry Candidate Packages")
        return self


class FormalReadinessGate(ContractModel):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,99}$")
    owner: FormalReadinessOwner
    status: FormalReadinessGateStatus
    summary: str = Field(min_length=1, max_length=1000)
    required_action: str = Field(min_length=1, max_length=1000)
    evidence: tuple[FormalReadinessEvidenceRef, ...] = Field(min_length=1, max_length=4)


class FormalReadinessReview(ContractModel):
    owner: Literal["A", "B", "C", "D"]
    decision: Literal["pending", "accepted_for_formal_window", "blocked"]
    reason: str = Field(min_length=1, max_length=1000)
    evidence: FormalReadinessEvidenceRef | None = None

    @model_validator(mode="after")
    def bind_decision_to_evidence(self) -> FormalReadinessReview:
        if self.decision == "pending" and self.evidence is not None:
            raise ValueError("pending DRI review cannot carry acceptance evidence")
        if self.decision != "pending" and self.evidence is None:
            raise ValueError("completed DRI review requires immutable evidence")
        return self


class FormalReadinessManifest(ContractModel):
    schema_version: Literal["m2a-formal-readiness-v1"] = (
        M2A_FORMAL_READINESS_SCHEMA_VERSION
    )
    audit_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    audit_base_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    observed_at: datetime
    profile_draft: FormalProfileDraft
    gates: tuple[FormalReadinessGate, ...] = Field(min_length=1)
    reviews: tuple[FormalReadinessReview, ...] = Field(min_length=4, max_length=4)
    window_authorization: Literal["not_requested"] = "not_requested"
    hcu_accessed: Literal[False] = False
    formal_round_created: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_complete_audit_scope(self) -> FormalReadinessManifest:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Formal readiness observation time must be timezone-aware")
        codes = tuple(item.code for item in self.gates)
        if codes != FORMAL_READINESS_GATE_CODES:
            raise ValueError("Formal readiness gates must be complete and canonically sorted")
        owners = tuple(item.owner for item in self.reviews)
        if owners != ("A", "B", "C", "D"):
            raise ValueError("Formal readiness reviews must contain canonical A/B/C/D owners")
        by_code = {item.code: item for item in self.gates}
        if (
            self.profile_draft.registration_state == "draft_unregistered"
            and by_code["formal_operator_profiles"].status == "pass"
        ):
            raise ValueError("unimplemented Formal Profile registration cannot pass readiness")
        if (
            self.profile_draft.candidate_family_state == "missing"
            and by_code["business_candidate_family"].status == "pass"
        ):
            raise ValueError("missing Candidate Family cannot pass readiness")
        if self.window_authorization == "not_requested" and (
            by_code["owner_window_authorization"].status == "pass"
        ):
            raise ValueError("missing window authorization cannot pass readiness")
        return self


class FormalReadinessGateResult(ReadModel):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,99}$")
    owner: FormalReadinessOwner
    declared_status: FormalReadinessGateStatus
    effective_status: FormalReadinessGateStatus
    evidence_status: Literal["verified", "missing", "hash_mismatch", "invalid_path"]
    summary: str = Field(min_length=1, max_length=1000)
    required_action: str = Field(min_length=1, max_length=1000)
    evidence: tuple[FormalReadinessEvidenceResult, ...] = Field(min_length=1)


class FormalReadinessEvidenceResult(ReadModel):
    path: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    digest_mode: Literal["raw_bytes", "text_lf"]
    evidence_type: str = Field(min_length=1, max_length=200)
    status: Literal["verified", "missing", "hash_mismatch", "invalid_path"]


class FormalReadinessReport(ReadModel):
    schema_version: Literal["m2a-formal-readiness-report-v1"] = (
        "m2a-formal-readiness-report-v1"
    )
    audit_id: str
    audit_base_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    manifest_hash: str = Field(pattern=SHA256_PATTERN)
    generated_at: datetime
    decision: Literal["hold", "ready_for_window_authorization"]
    gate_results: tuple[FormalReadinessGateResult, ...]
    reviews: tuple[FormalReadinessReview, ...]
    blocker_codes: tuple[str, ...]
    verified_evidence_count: int = Field(ge=0)
    profile_registration_allowed: Literal[False] = False
    formal_round_creation_allowed: Literal[False] = False
    window_authorization_required: Literal[True] = True
    hcu_accessed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False
