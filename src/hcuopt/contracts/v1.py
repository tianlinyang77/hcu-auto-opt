from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.platform_v1 import (
    SHA256_PATTERN,
    AdapterProvenance,
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    ExecutionAttempt,
    ExecutionRequest,
    ExecutionResult,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.domain.enums import (
    CandidateState,
    FrameworkSmokeDecision,
    GateResult,
    HotPatchCapability,
    JobState,
    JobType,
    LeaseScope,
    ProfilerCapability,
    ProjectMode,
    Stage0ProbeType,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
    WorkerType,
    WorkflowType,
)

CONTRACT_VERSION = "v1"
MEASUREMENT_PROTOCOL_VERSION = "fake-v1-control-flow-only"


class TaskCreate(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    workload_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=200)
    budget: dict[str, Any] = Field(default_factory=dict)
    automatic_release_allowed: bool = False


class TaskView(ReadModel):
    task_id: UUID
    name: str
    workload_id: str
    state: TaskState
    project_mode: ProjectMode | None
    budget: dict[str, Any]
    automatic_release_allowed: bool
    stage0_authority: Literal["none", "synthetic", "formal"] = "none"
    version: int
    created_at: datetime
    updated_at: datetime


class FrameworkSmokeCreate(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    target_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    adapter_profile: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=200)


class FrameworkSmokeTaskView(TaskView):
    workflow_type: WorkflowType
    target_id: str
    target_snapshot_id: UUID
    adapter_profile: str
    retest_count: int = 0


class FrameworkSmokeSignoffRequest(ContractModel):
    decision: FrameworkSmokeDecision
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_bundle_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=300)


class FrameworkSmokeSignoffView(ReadModel):
    signoff_id: UUID
    task_id: UUID
    decision: FrameworkSmokeDecision
    actor: str
    reason: str
    evidence_bundle_id: UUID
    idempotency_key: str
    task_state: TaskState
    created_at: datetime


class Stage0EvidenceRequest(ContractModel):
    """Legacy Fake Demo input; it can never authorize a real Stage 0 run."""

    measurement: GateResult
    profiler: ProfilerCapability
    hot_patch: HotPatchCapability
    hardware_fingerprint: str = Field(min_length=1)
    software_fingerprint: str = Field(min_length=1)
    timer_resolution_ns: float | None = Field(default=None, ge=0)
    noise_sigma_ns: float | None = Field(default=None, ge=0)
    noise_cv: float | None = Field(default=None, ge=0)
    mde_ratio: float | None = Field(default=None, ge=0)
    evidence_uri: str | None = None
    synthetic: Literal[True] = True


class Stage0Budget(ContractModel):
    """Caller-controlled limits; executable probe configuration is not public input."""

    max_wall_seconds: int | None = Field(default=None, ge=1, le=86_400)
    max_samples: int | None = Field(default=None, ge=1, le=1_000_000)


class Stage0RunCreate(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    workload_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    adapter_profile: str = Field(min_length=1, max_length=200)
    mode: Stage0RunMode
    protocol_version: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=300)
    budget: Stage0Budget = Field(default_factory=Stage0Budget)


class Stage0RunView(ReadModel):
    stage0_run_id: UUID
    task_id: UUID
    target_snapshot_id: UUID
    adapter_profile: str
    mode: Stage0RunMode
    state: Stage0RunState
    protocol_version: str
    idempotency_key: str
    report: dict[str, Any] | None = None
    created_at: datetime
    finalized_at: datetime | None = None


class Stage0ProbeResult(ContractModel):
    stage0_run_id: UUID
    target_snapshot_id: UUID
    probe_type: Stage0ProbeType
    protocol_version: str = Field(min_length=1, max_length=200)
    raw_evidence_uri: str | None = None
    raw_evidence_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    summary: dict[str, Any] = Field(default_factory=dict)
    adapter_provenance: list[AdapterProvenance] = Field(min_length=1)
    synthetic: bool = False
    cleanup_evidence: dict[str, Any] | None = None

    @model_validator(mode="after")
    def enforce_evidence_origin(self) -> Stage0ProbeResult:
        if any(
            item.implementation_kind == "fake" for item in self.adapter_provenance
        ) and not self.synthetic:
            raise ValueError("fake Stage 0 probes must be synthetic")
        if not self.synthetic and (
            self.raw_evidence_uri is None or self.raw_evidence_hash is None
        ):
            raise ValueError("real Stage 0 probes require raw evidence URI and SHA256")
        if self.cleanup_evidence is not None:
            if not isinstance(self.cleanup_evidence.get("fence"), dict):
                raise ValueError("Stage 0 cleanup evidence requires fence evidence")
            if not isinstance(self.cleanup_evidence.get("health"), dict):
                raise ValueError("Stage 0 cleanup evidence requires health evidence")
        return self


class Stage0ProbeView(ReadModel):
    probe_record_id: UUID
    stage0_run_id: UUID
    job_id: UUID
    task_id: UUID
    target_snapshot_id: UUID
    probe_type: Stage0ProbeType
    protocol_version: str
    raw_evidence_uri: str | None
    raw_evidence_hash: str | None
    summary: dict[str, Any]
    adapter_provenance: list[AdapterProvenance]
    synthetic: bool
    lease_id: UUID | None
    resource_id: str | None
    fencing_token: int | None
    cleanup_evidence: dict[str, Any] | None
    created_at: datetime


class Stage0RunSummary(ContractModel):
    run: Stage0RunView
    task: TaskView
    target: TargetSpec
    probes: list[Stage0ProbeView]
    jobs: list[dict[str, Any]]
    events: list[dict[str, Any]]


class Stage0ReportView(ReadModel):
    task_id: UUID
    mode: ProjectMode
    reasons: list[str]
    automatic_release_allowed: bool = False
    evidence_authority: Literal["synthetic_control_flow_only", "formal"]


class BaselineCreate(ContractModel):
    hardware_fingerprint: str = Field(min_length=1)
    software_fingerprint: str = Field(min_length=1)
    workload_id: str = Field(min_length=1)
    configuration_hash: str = Field(min_length=1)


class BaselineView(ReadModel):
    baseline_epoch_id: UUID
    task_id: UUID
    hardware_fingerprint: str
    software_fingerprint: str
    workload_id: str
    configuration_hash: str
    frozen: bool
    created_at: datetime


class WorkerRegister(ContractModel):
    worker_id: str = Field(min_length=1, max_length=200)
    worker_type: WorkerType
    contract_version: str = CONTRACT_VERSION
    adapter_profile: str | None = Field(default=None, min_length=1, max_length=200)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class WorkerView(ReadModel):
    worker_id: str
    worker_type: WorkerType
    contract_version: str
    adapter_profile: str | None = None
    capabilities: dict[str, Any]
    state: str
    registered_at: datetime
    last_heartbeat_at: datetime


class JobCreate(ContractModel):
    task_id: UUID
    job_type: JobType
    accepted_worker_type: WorkerType
    adapter_profile: str | None = Field(default=None, min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=300)
    lease_scope: LeaseScope = LeaseScope.NONE
    priority: int = 0
    max_attempts: int = Field(default=3, ge=1, le=20)


class JobClaim(ReadModel):
    job_id: UUID
    task_id: UUID
    job_type: JobType
    adapter_profile: str | None = None
    state: JobState
    payload: dict[str, Any]
    lease_scope: LeaseScope
    claim_token: UUID
    lease_id: UUID | None = None
    resource_id: str | None = None
    fencing_token: int | None = None
    attempts: int


class JobHeartbeat(ContractModel):
    claim_token: UUID
    fencing_token: int | None = None


class JobComplete(ContractModel):
    claim_token: UUID
    fencing_token: int | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class JobFail(ContractModel):
    claim_token: UUID
    fencing_token: int | None = None
    error_code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = True
    cleanup_evidence: dict[str, Any] | None = None


class ResourceCleanupReport(ContractModel):
    fencing_token: int = Field(ge=1)
    cleanup_evidence: dict[str, Any]

    @model_validator(mode="after")
    def require_fence_and_health(self) -> ResourceCleanupReport:
        if not isinstance(self.cleanup_evidence.get("fence"), dict):
            raise ValueError("cleanup_evidence requires fence evidence")
        if not isinstance(self.cleanup_evidence.get("health"), dict):
            raise ValueError("cleanup_evidence requires health evidence")
        return self


class CandidateView(ReadModel):
    candidate_id: UUID
    task_id: UUID
    round_id: UUID
    baseline_epoch_id: UUID
    source_hash: str
    variant: str
    state: CandidateState
    ordinal: int


class TaskSummary(ContractModel):
    task: TaskView
    baseline: BaselineView | None
    jobs: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    evaluations: list[dict[str, Any]]
    warning: str = "Fake 结果只验证控制流，不构成真实性能证据"


class ReapResult(ContractModel):
    recovered_job_ids: list[UUID]


class AdapterProfileView(ContractModel):
    name: str
    implementation_kind: Literal["real", "fake"]
    required_capabilities: list[str]


class SourcePreparationResult(ContractModel):
    source: SourceSnapshot
    adapter_provenance: list[AdapterProvenance] = Field(min_length=1)
    synthetic: bool

    @model_validator(mode="after")
    def validate_synthetic_provenance(self) -> SourcePreparationResult:
        if any(item.implementation_kind == "fake" for item in self.adapter_provenance):
            if not self.synthetic:
                raise ValueError("fake source preparation must be synthetic")
        return self


class NoopBuildResult(ContractModel):
    source: SourceSnapshot
    artifact: ArtifactManifest
    adapter_provenance: list[AdapterProvenance] = Field(min_length=1)
    synthetic: bool

    @model_validator(mode="after")
    def validate_build_relationships(self) -> NoopBuildResult:
        if self.source.kind != "candidate":
            raise ValueError("no-op build requires a candidate source snapshot")
        if self.artifact.source_snapshot_id != self.source.snapshot_id:
            raise ValueError("artifact must reference the candidate source snapshot")
        if self.artifact.synthetic != self.synthetic:
            raise ValueError("artifact and result synthetic flags must match")
        if any(item.implementation_kind == "fake" for item in self.adapter_provenance):
            if not self.synthetic:
                raise ValueError("fake no-op builds must be synthetic")
        return self


class FrameworkSmokeResult(ContractModel):
    result_kind: Literal["single"] = "single"
    execution_request: ExecutionRequest
    execution_result: ExecutionResult
    execution_attempt: ExecutionAttempt
    evaluation: EvaluationRun
    evidence: EvidenceBundle
    cleanup_evidence: dict[str, Any]
    adapter_provenance: list[AdapterProvenance] = Field(min_length=1)
    synthetic: bool
    no_performance_conclusion: Literal[True] = True

    @model_validator(mode="after")
    def validate_framework_relationships(self) -> FrameworkSmokeResult:
        request_id = self.execution_request.request_id
        if self.execution_result.request_id != request_id:
            raise ValueError("execution result must reference execution request")
        if self.execution_attempt.request_id != request_id:
            raise ValueError("execution attempt must reference execution request")
        if self.execution_attempt.evaluation_run_id != self.evaluation.evaluation_run_id:
            raise ValueError("execution attempt must belong to the evaluation run")
        if self.evaluation.phase != "correctness":
            raise ValueError("framework smoke is a correctness evaluation")
        if self.evidence.task_id != self.evaluation.task_id:
            raise ValueError("evidence and evaluation task ids must match")
        if self.evidence.candidate_id != self.evaluation.candidate_id:
            raise ValueError("evidence and evaluation candidate ids must match")
        if self.evidence.baseline_epoch_id != self.evaluation.baseline_epoch_id:
            raise ValueError("evidence and evaluation baseline ids must match")
        values = (
            self.execution_result.synthetic,
            self.execution_attempt.synthetic,
            self.evaluation.synthetic,
            self.evidence.synthetic,
        )
        if any(value != self.synthetic for value in values):
            raise ValueError("all framework smoke synthetic flags must match")
        if any(item.implementation_kind == "fake" for item in self.adapter_provenance):
            if not self.synthetic:
                raise ValueError("fake framework smoke results must be synthetic")
        return self


class FrameworkSmokeVariantExecution(ContractModel):
    variant: Literal["baseline", "noop"]
    execution_request: ExecutionRequest
    execution_result: ExecutionResult
    execution_attempt: ExecutionAttempt
    cleanup_evidence: dict[str, Any]

    @model_validator(mode="after")
    def validate_variant_execution(self) -> FrameworkSmokeVariantExecution:
        request_id = self.execution_request.request_id
        if self.execution_result.request_id != request_id:
            raise ValueError("variant execution result must reference its request")
        if self.execution_attempt.request_id != request_id:
            raise ValueError("variant execution attempt must reference its request")
        if self.execution_attempt.variant != self.variant:
            raise ValueError("execution attempt variant does not match its container role")
        result_fields = (
            "status",
            "exit_code",
            "started_at",
            "finished_at",
            "stdout_uri",
            "stderr_uri",
        )
        if any(
            getattr(self.execution_result, name)
            != getattr(self.execution_attempt, name)
            for name in result_fields
        ):
            raise ValueError("execution attempt must faithfully record its execution result")
        if self.execution_result.synthetic != self.execution_attempt.synthetic:
            raise ValueError("variant execution synthetic flags must match")
        if not isinstance(self.cleanup_evidence.get("fence"), dict):
            raise ValueError("variant execution requires fence evidence")
        if not isinstance(self.cleanup_evidence.get("health"), dict):
            raise ValueError("variant execution requires health evidence")
        return self


class PairedFrameworkSmokeResult(ContractModel):
    result_kind: Literal["paired"] = "paired"
    executions: list[FrameworkSmokeVariantExecution] = Field(min_length=2, max_length=2)
    evaluation: EvaluationRun
    evidence: EvidenceBundle
    cleanup_evidence: dict[str, Any]
    adapter_provenance: list[AdapterProvenance] = Field(min_length=1)
    synthetic: bool
    no_performance_conclusion: Literal[True] = True

    @model_validator(mode="after")
    def validate_paired_relationships(self) -> PairedFrameworkSmokeResult:
        by_variant = {item.variant: item for item in self.executions}
        if set(by_variant) != {"baseline", "noop"} or len(by_variant) != 2:
            raise ValueError("paired framework smoke requires baseline and noop executions")
        if len({item.execution_request.request_id for item in self.executions}) != 2:
            raise ValueError("baseline and noop require distinct execution request IDs")
        if len({item.execution_attempt.execution_attempt_id for item in self.executions}) != 2:
            raise ValueError("baseline and noop require distinct execution attempt IDs")
        for item in by_variant.values():
            if item.execution_attempt.evaluation_run_id != self.evaluation.evaluation_run_id:
                raise ValueError("variant execution must belong to the evaluation run")
            if item.execution_result.synthetic != self.synthetic:
                raise ValueError("paired framework smoke synthetic flags must match")
        if self.evaluation.phase != "correctness":
            raise ValueError("framework smoke is a correctness evaluation")
        if self.evidence.task_id != self.evaluation.task_id:
            raise ValueError("evidence and evaluation task ids must match")
        if self.evidence.candidate_id != self.evaluation.candidate_id:
            raise ValueError("evidence and evaluation candidate ids must match")
        if self.evidence.baseline_epoch_id != self.evaluation.baseline_epoch_id:
            raise ValueError("evidence and evaluation baseline ids must match")
        values = (self.evaluation.synthetic, self.evidence.synthetic)
        if any(value != self.synthetic for value in values):
            raise ValueError("paired evaluation and evidence synthetic flags must match")
        if any(item.implementation_kind == "fake" for item in self.adapter_provenance):
            if not self.synthetic:
                raise ValueError("fake paired framework smoke results must be synthetic")
        attempt_ids = self.evidence.summary.get("execution_attempt_ids")
        expected_attempt_ids = {
            name: str(item.execution_attempt.execution_attempt_id)
            for name, item in by_variant.items()
        }
        if attempt_ids != expected_attempt_ids:
            raise ValueError("evidence must bind both variant execution attempts")
        cleanup_variants = self.cleanup_evidence.get("variants")
        expected_cleanup = {
            name: item.cleanup_evidence for name, item in by_variant.items()
        }
        if cleanup_variants != expected_cleanup:
            raise ValueError("top-level cleanup evidence must bind both variants")
        final_cleanup = by_variant["noop"].cleanup_evidence
        for name in ("fence", "health"):
            if self.cleanup_evidence.get(name) != final_cleanup.get(name):
                raise ValueError("top-level cleanup state must match final noop cleanup")
        if self.evaluation.passed is True:
            for name, item in by_variant.items():
                if item.execution_result.status != "succeeded":
                    raise ValueError(
                        f"passed framework smoke requires successful {name} execution"
                    )
                if item.cleanup_evidence["fence"].get("fenced") is not True:
                    raise ValueError(
                        f"passed framework smoke requires successful {name} fencing"
                    )
                if item.cleanup_evidence["health"].get("healthy") is not True:
                    raise ValueError(
                        f"passed framework smoke requires healthy {name} cleanup"
                    )
        return self


class FrameworkSmokeAction(ContractModel):
    reason: str = Field(min_length=1, max_length=500)


class FrameworkSmokeSummary(ContractModel):
    task: FrameworkSmokeTaskView
    target: TargetSpec
    baseline: BaselineView | None
    source_snapshots: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    execution_requests: list[dict[str, Any]]
    execution_attempts: list[dict[str, Any]]
    evaluations: list[dict[str, Any]]
    evidence_bundles: list[dict[str, Any]]
    events: list[dict[str, Any]]
    adapter_mode: Literal["real", "fake"]
    performance_conclusion: Literal["not_measured"] = "not_measured"
    warning: str = "Framework Smoke 只验证框架链路和输出一致性，不构成性能收益证据"
