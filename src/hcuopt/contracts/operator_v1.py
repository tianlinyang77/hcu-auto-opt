# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.m2 import CandidateSourcePackageRef, RoundBudget
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN
from hcuopt.domain.enums import RoundCandidateState, SearchRoundRunMode, SearchRoundState

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


class OperatorRoundStartRequest(ContractModel):
    preview_id: UUID
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=300)
    acknowledged_warning_codes: tuple[str, ...] = ()
    expected_service_identity: OperatorServiceIdentityAssertion

    @model_validator(mode="after")
    def require_canonical_acknowledgements(self) -> OperatorRoundStartRequest:
        if self.acknowledged_warning_codes != tuple(
            sorted(set(self.acknowledged_warning_codes))
        ):
            raise ValueError("acknowledged warning codes must be unique and sorted")
        return self


class OperatorStartCandidateMember(ReadModel):
    ordinal: int = Field(ge=0, le=3)
    candidate_input_digest: str = Field(pattern=SHA256_PATTERN)
    candidate_id: UUID
    round_candidate_id: UUID
    intake_idempotency_key: str = Field(min_length=8, max_length=300)
    source_package_store_id: str = Field(min_length=1, max_length=200)
    source_package_store_version: int = Field(ge=1)
    source_package_store_hash: str = Field(pattern=SHA256_PATTERN)
    source_package_ref: CandidateSourcePackageRef
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    candidate_kind: Literal["fixture"]
    optimization_intent: str = Field(min_length=1, max_length=2000)
    state: Literal["pending", "round_member_bound", "failed"] = "pending"
    error_code: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_]{2,99}$"
    )

    @model_validator(mode="after")
    def bind_member_error_to_failure(self) -> OperatorStartCandidateMember:
        if (self.state == "failed") != (self.error_code is not None):
            raise ValueError("failed Start member requires exactly one error code")
        return self


class OperatorStartIntentView(ReadModel):
    schema_version: Literal["m2-operator-start-v1"] = "m2-operator-start-v1"
    intent_id: UUID
    preview_id: UUID
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    request_digest: str = Field(pattern=SHA256_PATTERN)
    task_id: UUID
    round_id: UUID
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=300)
    state: Literal[
        "preparing",
        "plans_frozen",
        "round_created",
        "intake_closed",
        "finalized",
        "failed",
    ]
    candidate_members: tuple[OperatorStartCandidateMember, ...] = Field(
        min_length=2, max_length=4
    )
    search_plan_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    holdout_plan_commitment: str | None = Field(default=None, pattern=SHA256_PATTERN)
    holdout_commitment_scheme: Literal["sha256-nonce-v1"] | None = None
    holdout_plan_authority_id: str | None = Field(
        default=None, min_length=1, max_length=300
    )
    holdout_plan_authority_hash: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    family_alpha: float | None = Field(default=None, gt=0.0, lt=1.0)
    candidate_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    error_code: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_]{2,99}$"
    )
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    service_identity: OperatorServiceIdentity
    synthetic: Literal[True] = True
    automatic_release_allowed: Literal[False] = False
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None = None

    @model_validator(mode="after")
    def require_atomic_start_progress(self) -> OperatorStartIntentView:
        ordinals = tuple(member.ordinal for member in self.candidate_members)
        if ordinals != tuple(range(len(self.candidate_members))):
            raise ValueError("Start members must remain canonically ordered")
        plan_values = (
            self.search_plan_hash,
            self.holdout_plan_commitment,
            self.holdout_commitment_scheme,
            self.holdout_plan_authority_id,
            self.holdout_plan_authority_hash,
            self.family_alpha,
        )
        has_plans = all(value is not None for value in plan_values)
        if any(value is not None for value in plan_values) != has_plans:
            raise ValueError("Start Plan Authority fields must resolve atomically")
        if self.state not in {"preparing", "failed"} and not has_plans:
            raise ValueError("advanced StartIntent requires frozen Plan Authority")
        all_bound = all(
            member.state == "round_member_bound" for member in self.candidate_members
        )
        if self.state in {"intake_closed", "finalized"} and (
            not all_bound or self.candidate_family_hash is None
        ):
            raise ValueError("closed StartIntent requires its complete Candidate Family")
        if self.state == "finalized" and self.finalized_at is None:
            raise ValueError("finalized StartIntent requires finalized_at")
        if self.state != "finalized" and self.finalized_at is not None:
            raise ValueError("only finalized StartIntent may set finalized_at")
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("StartIntent error code and message must be written together")
        if (self.state == "failed") != (self.error_code is not None):
            raise ValueError("failed StartIntent requires exactly one safe error")
        return self


class OperatorStartView(OperatorStartIntentView):
    replayed: bool
    executable: bool

    @model_validator(mode="after")
    def bind_executable_to_finalized_intent(self) -> OperatorStartView:
        if self.executable != (self.state == "finalized"):
            raise ValueError("only finalized StartIntent is executable")
        return self


class OperatorProfileSelector(ContractModel):
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    profile_version: int = Field(ge=1)


class OperatorWorkloadView(ReadModel):
    """One discoverable workload backed by an exact immutable Profile."""

    profile: OperatorProfileRef
    state: Literal["active", "deprecated", "revoked"]
    display_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    authority_refs: WorkloadOperatorProfileRefs
    synthetic: Literal[True] = True


class OperatorCandidatePackageView(ReadModel):
    """A verified deployment-owned Candidate Package safe for Plan drafting."""

    candidate_id: UUID
    source_package_ref: CandidateSourcePackageRef
    candidate_kind: Literal["fixture"] = "fixture"
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    replacement_path: str = Field(min_length=1, max_length=2000)
    suggested_optimization_intent: str = Field(min_length=1, max_length=2000)


class OperatorHotspotAuthorityView(ReadModel):
    """Typed storage result used to assemble the public discovery view."""

    hotspot: OperatorHotspotRef
    authority: ResolvedOperatorAuthority
    symbol: str = Field(min_length=1, max_length=1000)
    share_ratio: float = Field(ge=0.0, le=1.0)
    opportunity_score: float = Field(ge=0.0)
    patchability: str = Field(min_length=1, max_length=200)


class OperatorHotspotView(ReadModel):
    """A frozen Hotspot Authority plus its currently verified Candidate inputs."""

    hotspot: OperatorHotspotRef
    target_profile: OperatorProfileRef
    workload_profile: OperatorProfileRef
    baseline_epoch_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    symbol: str = Field(min_length=1, max_length=1000)
    share_ratio: float = Field(ge=0.0, le=1.0)
    opportunity_score: float = Field(ge=0.0)
    patchability: str = Field(min_length=1, max_length=200)
    candidate_packages: tuple[OperatorCandidatePackageView, ...] = ()
    synthetic: Literal[True] = True
    automatic_release_allowed: Literal[False] = False


class OperatorRoundPlanSpec(ContractModel):
    """Human-authored CLI input; exact Profile hashes come from the live service."""

    name: str = Field(min_length=1, max_length=200)
    target_profile: OperatorProfileSelector
    workload_profile: OperatorProfileSelector
    measurement_profile: OperatorProfileSelector
    hotspot: OperatorHotspotRef
    candidates: tuple[OperatorCandidateInput, ...] = Field(min_length=2, max_length=4)
    max_promoted: int = Field(ge=1, le=2)
    idempotency_key: str = Field(min_length=8, max_length=300)

    @model_validator(mode="after")
    def require_candidate_and_promotion_bounds(self) -> OperatorRoundPlanSpec:
        if self.max_promoted > len(self.candidates):
            raise ValueError("max_promoted cannot exceed Candidate count")
        return self


class OperatorRunMetrics(ReadModel):
    """Non-performance OX-1 usability metrics captured from one real CLI run."""

    schema_version: Literal["m2-operator-run-metrics-v1"] = (
        "m2-operator-run-metrics-v1"
    )
    outcome: Literal["succeeded", "blocked", "failed"]
    started_at: datetime
    completed_at: datetime
    operator_active_seconds: float = Field(ge=0.0)
    manual_intervention_count: int = Field(ge=0)
    preflight_blocked_before_hcu_count: int = Field(ge=0, le=1)
    report_generation_seconds: float = Field(ge=0.0)
    error_code: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_]{2,99}$"
    )
    synthetic: Literal[True] = True
    performance_evidence: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_run_metric_consistency(self) -> OperatorRunMetrics:
        if self.completed_at < self.started_at:
            raise ValueError("Operator run metrics cannot complete before they start")
        if (self.outcome == "succeeded") == (self.error_code is not None):
            raise ValueError("only non-success Operator runs carry an error code")
        if (self.outcome == "blocked") != (
            self.preflight_blocked_before_hcu_count == 1
        ):
            raise ValueError(
                "exactly a blocked Operator run records one Preflight block"
            )
        return self


class OperatorCandidateSummary(ReadModel):
    ordinal: int = Field(ge=0, le=3)
    round_candidate_id: UUID
    candidate_id: UUID
    state: RoundCandidateState
    artifact_id: UUID | None = None
    artifact_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    terminal_failure_code: str | None = Field(default=None, min_length=1, max_length=200)
    failure_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_candidate_terminal_pairs(self) -> OperatorCandidateSummary:
        if (self.artifact_id is None) != (self.artifact_hash is None):
            raise ValueError("Operator Candidate Artifact identity must be paired")
        if (self.terminal_failure_code is None) != (
            self.failure_evidence_hash is None
        ):
            raise ValueError("Operator Candidate failure evidence must be paired")
        return self


class OperatorRoundSummary(ReadModel):
    schema_version: Literal["m2-operator-read-model-v1"] = (
        "m2-operator-read-model-v1"
    )
    generated_at: datetime
    intent_id: UUID
    task_id: UUID
    round_id: UUID
    round_version: int = Field(ge=1)
    state: SearchRoundState
    run_mode: Literal["scripted"] = "scripted"
    next_action: Literal[
        "await_candidate_intake",
        "close_intake",
        "await_build_terminals",
        "freeze_artifact_family",
        "close_search_barrier",
        "record_holdout_reveal",
        "close_holdout_barrier",
        "record_multiple_comparison",
        "finalize_scripted_round",
        "none",
    ]
    reason: str = Field(min_length=1, max_length=500)
    candidates: tuple[OperatorCandidateSummary, ...] = Field(min_length=2, max_length=4)
    candidate_count: int = Field(ge=2, le=4)
    build_terminal_count: int = Field(ge=0, le=4)
    settled_budget_entry_count: int = Field(ge=0)
    evidence_status: Literal["not_available", "available"]
    terminal: bool
    synthetic: Literal[True] = True
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_summary_counts(self) -> OperatorRoundSummary:
        if self.candidate_count != len(self.candidates):
            raise ValueError("Operator candidate_count must match Candidate summaries")
        if self.build_terminal_count > self.candidate_count:
            raise ValueError("Operator build_terminal_count exceeds Candidate count")
        terminal_state = self.state in {
            SearchRoundState.SCRIPTED_COMPLETED,
            SearchRoundState.CANCELLED,
        }
        if self.terminal != terminal_state:
            raise ValueError("Operator terminal flag must follow Round Authority")
        return self


class OperatorRoundReport(ReadModel):
    schema_version: Literal["m2-operator-report-v1"] = "m2-operator-report-v1"
    generated_at: datetime
    report_status: Literal["interim", "final"]
    summary: OperatorRoundSummary
    round_authority: dict
    evidence_bundle: dict | None = None
    conclusion_boundary: Literal["synthetic_only_no_real_performance_claim"] = (
        "synthetic_only_no_real_performance_claim"
    )
    formal_signoff_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_report_finality(self) -> OperatorRoundReport:
        expected = "final" if self.summary.terminal else "interim"
        if self.report_status != expected:
            raise ValueError("Operator Report status must follow terminal Round Authority")
        if (
            str(self.round_authority.get("round_id")) != str(self.summary.round_id)
            or self.round_authority.get("version") != self.summary.round_version
            or self.round_authority.get("state") != self.summary.state.value
            or self.round_authority.get("run_mode") != "scripted"
            or self.round_authority.get("automatic_release_allowed") is not False
        ):
            raise ValueError("Operator Report Round Authority does not match its Summary")
        if (self.summary.evidence_status == "available") != (
            self.evidence_bundle is not None
        ):
            raise ValueError("Operator Evidence status must match the Report Bundle")
        return self
