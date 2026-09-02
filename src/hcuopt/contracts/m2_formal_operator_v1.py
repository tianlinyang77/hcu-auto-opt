# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileWindowAuthorization,
)
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.contracts.operator_v1 import (
    OperatorHotspotRef,
    OperatorProfileRef,
    OperatorServiceIdentity,
    OperatorServiceIdentityAssertion,
    PreflightCheckResult,
    ResolvedOperatorAuthority,
    ResolvedRoundPlan,
)
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import ProjectMode, SearchRoundRunMode

M2A_FORMAL_OPERATOR_PLAN_SCHEMA_VERSION = "m2a-formal-operator-plan-v1"


class FormalRoundPlanPreviewRequest(ContractModel):
    """Non-executing request to compile one already authorized Formal Family."""

    name: str = Field(min_length=1, max_length=200)
    run_mode: Literal[SearchRoundRunMode.FORMAL] = SearchRoundRunMode.FORMAL
    target_profile: OperatorProfileRef
    workload_profile: OperatorProfileRef
    measurement_profile: OperatorProfileRef
    candidate_family: BusinessCandidateFamilyManifest
    max_promoted: int = Field(ge=1, le=2)
    idempotency_key: str = Field(min_length=8, max_length=300)
    expected_service_identity: OperatorServiceIdentityAssertion
    expected_formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_canonical_formal_request(self) -> FormalRoundPlanPreviewRequest:
        expected_kinds = (
            (self.target_profile, "target"),
            (self.workload_profile, "workload"),
            (self.measurement_profile, "measurement"),
        )
        if any(reference.profile_kind != kind for reference, kind in expected_kinds):
            raise ValueError("Formal Operator Profile refs must use their canonical kinds")
        if self.max_promoted > len(self.candidate_family.members):
            raise ValueError("max_promoted cannot exceed the Formal Candidate Family size")
        return self


class FormalOperatorAuthoritySnapshot(ReadModel):
    """Repository-reread authority plus the complete immutable Hotspot reference."""

    authority: ResolvedOperatorAuthority
    hotspot: OperatorHotspotRef

    @model_validator(mode="after")
    def require_formal_authority(self) -> FormalOperatorAuthoritySnapshot:
        if self.authority.synthetic:
            raise ValueError("Formal Operator Authority cannot be synthetic")
        if self.hotspot.hotspot_id != self.authority.hotspot_id:
            raise ValueError("Formal Hotspot and resolved Authority must match")
        return self


class FormalResolvedRoundPlan(ResolvedRoundPlan):
    """Content-addressed, non-executing Formal Plan produced before StartIntent."""

    schema_version: Literal["m2a-formal-operator-plan-v1"] = (
        M2A_FORMAL_OPERATOR_PLAN_SCHEMA_VERSION
    )
    run_mode: Literal[SearchRoundRunMode.FORMAL] = SearchRoundRunMode.FORMAL
    hotspot: OperatorHotspotRef | None = None
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    authorized_host_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,199}$")
    authorized_resource_id: str = Field(min_length=1, max_length=200)
    authorization_window_starts_at: datetime
    authorization_window_expires_at: datetime
    candidate_family: BusinessCandidateFamilyManifest
    source_family_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    source_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    project_mode: Literal[ProjectMode.DEGRADED_MANUAL_INTAKE] = (
        ProjectMode.DEGRADED_MANUAL_INTAKE
    )
    conclusion_boundary: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_atomic_formal_resolution(self) -> FormalResolvedRoundPlan:
        timestamps = (
            self.authorization_window_starts_at,
            self.authorization_window_expires_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("Formal authorization window must be timezone-aware")
        if self.authorization_window_expires_at <= self.authorization_window_starts_at:
            raise ValueError("Formal authorization window is invalid")
        if self.source_family_id != self.candidate_family.family_id:
            raise ValueError("Formal source Family identity must match its Manifest")
        resolved = bool(self.candidates)
        if resolved != (self.source_family_hash is not None):
            raise ValueError("Formal Candidate mapping and source Family Hash resolve atomically")
        if resolved and self.hotspot is None:
            raise ValueError("resolved Formal Candidates require the authoritative Hotspot")
        if any(item.candidate_kind != "business" for item in self.candidates):
            raise ValueError("Formal resolved Plan may contain only business Candidates")
        return self


class FormalRoundPlanPreviewView(ReadModel):
    preview_id: UUID
    preview_request_digest: str = Field(pattern=SHA256_PATTERN)
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    resolved_plan: FormalResolvedRoundPlan
    checks: tuple[PreflightCheckResult, ...] = Field(min_length=1)
    start_allowed: bool
    required_ack_codes: tuple[str, ...] = ()
    expires_at: datetime
    service_identity: OperatorServiceIdentity
    formal_authorization: FormalProfileWindowAuthorization
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False
    created_at: datetime

    @model_validator(mode="after")
    def require_consistent_formal_preview(self) -> FormalRoundPlanPreviewView:
        blocked = any(item.status == "block" for item in self.checks)
        check_codes = tuple(item.code for item in self.checks)
        warning_codes = tuple(sorted(item.code for item in self.checks if item.status == "warn"))
        if self.start_allowed == blocked:
            raise ValueError("start_allowed must be false exactly when Formal Preflight is blocked")
        if len(check_codes) != len(set(check_codes)):
            raise ValueError("Formal Preflight check codes must be unique")
        if self.required_ack_codes != warning_codes:
            raise ValueError("required_ack_codes must equal canonical warning codes")
        if self.expires_at <= self.created_at:
            raise ValueError("Formal Operator Preview must expire after it is created")
        if (
            self.resolved_plan.formal_authorization_hash
            != self.formal_authorization.authorization_hash
        ):
            raise ValueError("Formal Preview and authorization Hash must match")
        plan_window = (
            self.resolved_plan.authorized_host_id,
            self.resolved_plan.authorized_resource_id,
            self.resolved_plan.authorization_window_starts_at,
            self.resolved_plan.authorization_window_expires_at,
        )
        authorized_window = (
            self.formal_authorization.host_id,
            self.formal_authorization.resource_id,
            self.formal_authorization.window_starts_at,
            self.formal_authorization.window_expires_at,
        )
        if plan_window != authorized_window:
            raise ValueError("Formal Plan window metadata must match its authorization")
        if not (
            self.formal_authorization.window_starts_at
            <= self.created_at
            < self.expires_at
            <= self.formal_authorization.window_expires_at
        ):
            raise ValueError("Formal Preview lifetime must stay inside its authorization window")
        if self.start_allowed and (
            self.resolved_plan.authority is None
            or self.resolved_plan.hotspot is None
            or not self.resolved_plan.candidates
            or self.resolved_plan.candidate_input_set_hash is None
            or self.resolved_plan.source_family_hash is None
        ):
            raise ValueError("startable Formal Preview requires fully resolved immutable inputs")
        if self.start_allowed and (
            self.resolved_plan.source_family_hash
            != self.formal_authorization.source_family_hash
            or self.resolved_plan.budget != self.formal_authorization.budget
        ):
            raise ValueError("startable Formal Preview must match authorized Family and Budget")
        return self
