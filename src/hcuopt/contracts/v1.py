from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hcuopt.domain.enums import (
    CandidateState,
    GateResult,
    HotPatchCapability,
    JobState,
    JobType,
    LeaseScope,
    ProfilerCapability,
    ProjectMode,
    TaskState,
    WorkerType,
)

CONTRACT_VERSION = "v1"
MEASUREMENT_PROTOCOL_VERSION = "fake-v1-control-flow-only"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadModel(ContractModel):
    model_config = ConfigDict(extra="ignore")


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
    version: int
    created_at: datetime
    updated_at: datetime


class Stage0EvidenceRequest(ContractModel):
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


class Stage0ReportView(ReadModel):
    task_id: UUID
    mode: ProjectMode
    reasons: list[str]
    automatic_release_allowed: bool = False


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
    capabilities: dict[str, Any] = Field(default_factory=dict)


class WorkerView(ReadModel):
    worker_id: str
    worker_type: WorkerType
    contract_version: str
    capabilities: dict[str, Any]
    state: str
    registered_at: datetime
    last_heartbeat_at: datetime


class JobCreate(ContractModel):
    task_id: UUID
    job_type: JobType
    accepted_worker_type: WorkerType
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=300)
    lease_scope: LeaseScope = LeaseScope.NONE
    priority: int = 0
    max_attempts: int = Field(default=3, ge=1, le=20)


class JobClaim(ReadModel):
    job_id: UUID
    task_id: UUID
    job_type: JobType
    state: JobState
    payload: dict[str, Any]
    lease_scope: LeaseScope
    claim_token: UUID
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
