# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.m2 import CandidateSourcePackageRef, RoundBudget
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN
from hcuopt.domain.enums import SearchRoundRunMode

M2_OPERATOR_CONTRACT_VERSION = "m2-operator-v1"


class OperatorServiceIdentity(ReadModel):
    source_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    control_contract_version: str = Field(min_length=1, max_length=100)
    operator_contract_version: Literal["m2-operator-v1"] = M2_OPERATOR_CONTRACT_VERSION
    profile_catalog_hash: str = Field(pattern=SHA256_PATTERN)
    server_instance_id: UUID


class TargetOperatorProfileRefs(ContractModel):
    target_id: str = Field(min_length=1, max_length=200)
    target_spec_hash: str = Field(pattern=SHA256_PATTERN)
    adapter_profile: str = Field(min_length=1, max_length=200)
    resource_policy_id: str = Field(min_length=1, max_length=200)
    resource_policy_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_package_store_id: str = Field(min_length=1, max_length=200)
    candidate_package_store_version: int = Field(ge=1)
    candidate_package_store_hash: str = Field(pattern=SHA256_PATTERN)
    required_stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)


class WorkloadOperatorProfileRefs(ContractModel):
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    configuration_hash: str = Field(pattern=SHA256_PATTERN)
    dataset_uri: str = Field(min_length=1, max_length=2000)
    dataset_hash: str = Field(pattern=SHA256_PATTERN)
    model_uri: str = Field(min_length=1, max_length=2000)
    model_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_scope_id: str = Field(min_length=1, max_length=200)
    hotspot_scope_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_selection_policy: Literal["latest_frozen_matching"]


class MeasurementOperatorProfileRefs(ContractModel):
    search_protocol_version: str = Field(min_length=1, max_length=200)
    search_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_protocol_version: str = Field(min_length=1, max_length=200)
    holdout_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    budget: RoundBudget
    conclusion_boundary: Literal["explore", "standard", "formal"]


OperatorProfileAuthorityRefs: TypeAlias = (
    TargetOperatorProfileRefs
    | WorkloadOperatorProfileRefs
    | MeasurementOperatorProfileRefs
)


class OperatorProfileContent(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    profile_version: int = Field(ge=1)
    profile_kind: Literal["target", "workload", "measurement"]
    state: Literal["active", "deprecated", "revoked"]
    allowed_run_modes: tuple[SearchRoundRunMode, ...] = Field(min_length=1, max_length=2)
    display_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    authority_refs: OperatorProfileAuthorityRefs
    synthetic: bool
    created_at: datetime

    @model_validator(mode="after")
    def require_canonical_kind_and_mode(self) -> OperatorProfileContent:
        expected = {
            "target": TargetOperatorProfileRefs,
            "workload": WorkloadOperatorProfileRefs,
            "measurement": MeasurementOperatorProfileRefs,
        }[self.profile_kind]
        if not isinstance(self.authority_refs, expected):
            raise ValueError("profile_kind does not match authority_refs")
        mode_values = tuple(mode.value for mode in self.allowed_run_modes)
        if len(set(mode_values)) != len(mode_values) or mode_values != tuple(
            sorted(mode_values)
        ):
            raise ValueError("allowed_run_modes must be unique and canonically sorted")
        if self.synthetic and self.allowed_run_modes != (SearchRoundRunMode.SCRIPTED,):
            raise ValueError("synthetic Operator Profile may allow only scripted")
        if not self.synthetic and SearchRoundRunMode.SCRIPTED in self.allowed_run_modes:
            raise ValueError("scripted mode requires a synthetic Operator Profile")
        return self


class OperatorProfileDescriptor(OperatorProfileContent, ReadModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    profile_hash: str = Field(pattern=SHA256_PATTERN)


class OperatorProfileRef(ContractModel):
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    profile_version: int = Field(ge=1)
    profile_kind: Literal["target", "workload", "measurement"]
    profile_hash: str = Field(pattern=SHA256_PATTERN)


class OperatorServiceIdentityAssertion(ContractModel):
    source_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    control_contract_version: str = Field(min_length=1, max_length=100)
    operator_contract_version: Literal["m2-operator-v1"] = M2_OPERATOR_CONTRACT_VERSION
    profile_catalog_hash: str = Field(pattern=SHA256_PATTERN)
    server_instance_id: UUID


class OperatorHotspotBase(ContractModel):
    hotspot_id: UUID
    hotspot_intake_hash: str = Field(pattern=SHA256_PATTERN)
    profiler_evidence_uri: str = Field(min_length=1, max_length=2000)
    profiler_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    correctness_evidence_uri: str = Field(min_length=1, max_length=2000)
    correctness_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    replacement_point: str = Field(min_length=1, max_length=1000)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    shape: tuple[int | str, ...] = Field(min_length=1, max_length=32)
    dtype: str = Field(min_length=1, max_length=100)


class ProfilerOperatorHotspotRef(OperatorHotspotBase):
    source: Literal["profiler"]


class ManualOperatorHotspotRef(OperatorHotspotBase):
    source: Literal["manual"]
    manual_intake_uri: str = Field(min_length=1, max_length=2000)
    manual_intake_hash: str = Field(pattern=SHA256_PATTERN)


OperatorHotspotRef: TypeAlias = Annotated[
    ProfilerOperatorHotspotRef | ManualOperatorHotspotRef,
    Field(discriminator="source"),
]


class OperatorCandidateInput(ContractModel):
    ordinal: int = Field(ge=0, le=3)
    source_package_ref: CandidateSourcePackageRef
    optimization_intent: str = Field(min_length=1, max_length=2000)


class RoundPlanPreviewRequest(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    run_mode: SearchRoundRunMode
    target_profile: OperatorProfileRef
    workload_profile: OperatorProfileRef
    measurement_profile: OperatorProfileRef
    hotspot: OperatorHotspotRef
    candidates: tuple[OperatorCandidateInput, ...] = Field(min_length=2, max_length=4)
    max_promoted: int = Field(ge=1, le=2)
    idempotency_key: str = Field(min_length=8, max_length=300)
    expected_service_identity: OperatorServiceIdentityAssertion

    @model_validator(mode="after")
    def require_canonical_family_and_profile_kinds(self) -> RoundPlanPreviewRequest:
        expected_kinds = (
            (self.target_profile, "target"),
            (self.workload_profile, "workload"),
            (self.measurement_profile, "measurement"),
        )
        if any(reference.profile_kind != kind for reference, kind in expected_kinds):
            raise ValueError("Operator Profile refs must use their canonical kinds")
        ordinals = tuple(item.ordinal for item in self.candidates)
        if ordinals != tuple(range(len(self.candidates))):
            raise ValueError("Candidate ordinals must be contiguous and canonically sorted")
        package_refs = tuple(item.source_package_ref for item in self.candidates)
        for field in ("candidate_source_hash", "source_package_hash", "manifest_hash"):
            values = [getattr(reference, field) for reference in package_refs]
            if len(values) != len(set(values)):
                raise ValueError(f"Candidate {field} values must be unique")
        if self.max_promoted > len(self.candidates):
            raise ValueError("max_promoted cannot exceed Candidate count")
        return self


class PreflightCheckResult(ReadModel):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,99}$")
    scope: str = Field(min_length=1, max_length=100)
    status: Literal["pass", "warn", "block"]
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool
    action_code: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,99}$")
    evidence_uri: str | None = Field(default=None, min_length=1, max_length=2000)
    evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_evidence_pair(self) -> PreflightCheckResult:
        if (self.evidence_uri is None) != (self.evidence_hash is None):
            raise ValueError("Preflight Evidence URI and Hash must be written together")
        return self


class ResolvedOperatorAuthority(ReadModel):
    target_snapshot_id: UUID
    stage0_run_id: UUID
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_epoch_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    configuration_hash: str = Field(pattern=SHA256_PATTERN)
    image_digest: str = Field(pattern=SHA256_PATTERN)
    adapter_profile: str = Field(min_length=1, max_length=200)
    profiler_evidence_uri: str = Field(min_length=1, max_length=2000)
    profiler_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    synthetic: bool


class ResolvedOperatorCandidate(ReadModel):
    ordinal: int = Field(ge=0, le=3)
    candidate_id: UUID
    source_package_store_id: str = Field(min_length=1, max_length=200)
    source_package_store_version: int = Field(ge=1)
    source_package_store_hash: str = Field(pattern=SHA256_PATTERN)
    source_package_ref: CandidateSourcePackageRef
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    candidate_kind: Literal["fixture", "business"]
    optimization_intent: str = Field(min_length=1, max_length=2000)


class ResolvedRoundPlan(ReadModel):
    run_mode: SearchRoundRunMode
    target_profile: OperatorProfileRef
    workload_profile: OperatorProfileRef
    measurement_profile: OperatorProfileRef
    hotspot: OperatorHotspotRef
    authority: ResolvedOperatorAuthority | None = None
    candidates: tuple[ResolvedOperatorCandidate, ...] = ()
    candidate_input_set_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    search_protocol_version: str = Field(min_length=1, max_length=200)
    search_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_protocol_version: str = Field(min_length=1, max_length=200)
    holdout_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_commitment_scheme: Literal["sha256-nonce-v1"] = "sha256-nonce-v1"
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    budget: RoundBudget
    max_promoted: int = Field(ge=1, le=2)
    conclusion_boundary: Literal["explore", "standard", "formal"]
    synthetic: bool
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_atomic_authority_and_candidate_resolution(self) -> ResolvedRoundPlan:
        if bool(self.candidates) != (self.candidate_input_set_hash is not None):
            raise ValueError("Candidate family and input Hash resolve atomically")
        if self.candidates and self.authority is None:
            raise ValueError("resolved Candidates require resolved Authority")
        if self.candidates and len(self.candidates) != self.budget.max_candidates:
            raise ValueError("resolved Candidate count must equal the operation budget")
        if self.run_mode is SearchRoundRunMode.SCRIPTED and not self.synthetic:
            raise ValueError("Scripted resolved Plan must remain synthetic")
        if self.run_mode is SearchRoundRunMode.FORMAL and self.synthetic:
            raise ValueError("Formal resolved Plan cannot be synthetic")
        if self.authority is not None and self.authority.synthetic != self.synthetic:
            raise ValueError("resolved Authority and Plan synthetic flags must match")
        if self.run_mode is SearchRoundRunMode.SCRIPTED and any(
            item.candidate_kind != "fixture" for item in self.candidates
        ):
            raise ValueError("Scripted resolved Plan may contain only fixture Candidates")
        return self


class RoundPlanPreviewView(ReadModel):
    preview_id: UUID
    preview_request_digest: str = Field(pattern=SHA256_PATTERN)
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    resolved_plan: ResolvedRoundPlan
    checks: tuple[PreflightCheckResult, ...] = Field(min_length=1)
    start_allowed: bool
    required_ack_codes: tuple[str, ...] = ()
    expires_at: datetime
    service_identity: OperatorServiceIdentity
    synthetic: bool
    automatic_release_allowed: Literal[False] = False
    created_at: datetime

    @model_validator(mode="after")
    def require_start_and_warning_consistency(self) -> RoundPlanPreviewView:
        blocked = any(item.status == "block" for item in self.checks)
        check_codes = tuple(item.code for item in self.checks)
        warning_codes = tuple(sorted(item.code for item in self.checks if item.status == "warn"))
        if self.start_allowed == blocked:
            raise ValueError("start_allowed must be false exactly when Preflight is blocked")
        if len(check_codes) != len(set(check_codes)):
            raise ValueError("Preflight check codes must be unique")
        if self.required_ack_codes != warning_codes:
            raise ValueError("required_ack_codes must equal canonical warning codes")
        if self.synthetic != self.resolved_plan.synthetic:
            raise ValueError("Preview and resolved Plan synthetic flags must match")
        if self.start_allowed and (
            self.resolved_plan.authority is None
            or not self.resolved_plan.candidates
            or self.resolved_plan.candidate_input_set_hash is None
        ):
            raise ValueError("startable Preview requires fully resolved Authority and Candidates")
        if self.expires_at <= self.created_at:
            raise ValueError("Operator Preview must expire after it is created")
        return self
