from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from dcuopt.contracts.v1 import ContractModel
from dcuopt.domain.enums import LeaseScope

PLATFORM_CONTRACT_VERSION = "platform-v1"
SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
GIT_COMMIT_PATTERN = r"^[0-9a-f]{40}$"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TargetBlocker(ContractModel):
    id: str = Field(min_length=1, max_length=100)
    status: Literal["open", "resolved", "accepted"]
    detail: str = Field(min_length=1)


class InferenceImageSpec(ContractModel):
    source_host: str = Field(min_length=1)
    source_address: str = Field(min_length=1)
    registry: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    tag: str = Field(min_length=1)
    image_id: str = Field(pattern=SHA256_PATTERN)
    registry_digest: str = Field(pattern=SHA256_PATTERN)
    immutable_reference: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    dtk_version: str = Field(min_length=1)
    sglang_package_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_immutable_reference(self) -> InferenceImageSpec:
        expected = f"{self.registry}/{self.repository}@{self.registry_digest}"
        if self.immutable_reference != expected:
            raise ValueError(
                "immutable_reference must be registry/repository@registry_digest; "
                f"expected {expected}"
            )
        return self


class SourceBaselineSpec(ContractModel):
    repository: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    branch_head_observed_at_lock: str | None = Field(default=None, pattern=GIT_COMMIT_PATTERN)
    resolution_rule: Literal["commit_wins_over_moving_branch"]
    clean_checkout: str = Field(min_length=1)
    candidate_strategy: Literal["separate_worktree"]

    @field_validator("clean_checkout")
    @classmethod
    def validate_checkout_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("clean_checkout must be an absolute target-host path")
        return value


class AcceleratorBinding(ContractModel):
    device_index: int = Field(ge=0)
    model: str = Field(min_length=1)
    architecture: str = Field(min_length=1)
    numa_node: int = Field(ge=0)
    cpu_affinity: str = Field(min_length=1)
    expected_sclk_mhz: int = Field(gt=0)
    expected_mclk_mhz: int = Field(gt=0)
    expected_performance_level: str = Field(min_length=1)


class HostEnvironmentSpec(ContractModel):
    dtk_version: str = Field(min_length=1)
    driver_version: str = Field(min_length=1)


class ExecutionHostSpec(ContractModel):
    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    work_root: str = Field(min_length=1)
    prohibited_work_root: str = Field(min_length=1)
    accelerator: AcceleratorBinding
    observed_host_environment: HostEnvironmentSpec

    @field_validator("work_root", "prohibited_work_root")
    @classmethod
    def validate_host_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("target-host paths must be absolute")
        return value

    @model_validator(mode="after")
    def validate_work_roots(self) -> ExecutionHostSpec:
        if self.work_root == self.prohibited_work_root:
            raise ValueError("work_root cannot equal prohibited_work_root")
        return self


class TargetSpec(ContractModel):
    schema_version: Literal[1]
    target_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    stage0_status: Literal["pending", "passed", "degraded", "failed"]
    automatic_release_allowed: bool = False
    inference_image: InferenceImageSpec
    source_baseline: SourceBaselineSpec
    execution_host: ExecutionHostSpec
    measurement_constraints: list[str] = Field(default_factory=list)
    blockers: list[TargetBlocker] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_mvp_release_policy(self) -> TargetSpec:
        if self.automatic_release_allowed:
            raise ValueError("MVP target specs cannot enable automatic release")
        return self


class MountSpec(ContractModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    read_only: bool = True

    @field_validator("source", "target")
    @classmethod
    def validate_mount_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("mount paths must be absolute")
        return value


class ExecutionRequest(ContractModel):
    request_id: UUID = Field(default_factory=uuid4)
    target_id: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    working_directory: str = Field(min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    lease_scope: LeaseScope = LeaseScope.NONE
    resource_id: str | None = None
    fencing_token: int | None = Field(default=None, ge=1)
    container_image: str | None = Field(default=None, pattern=r"^.+@sha256:[0-9a-f]{64}$")
    mounts: list[MountSpec] = Field(default_factory=list)

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("working_directory must be an absolute target-host path")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("argv entries cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_lease_fencing(self) -> ExecutionRequest:
        if self.lease_scope is not LeaseScope.NONE and (
            self.resource_id is None or self.fencing_token is None
        ):
            raise ValueError("leased execution requires resource_id and fencing_token")
        return self


class ExecutionResult(ContractModel):
    request_id: UUID
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    exit_code: int | None
    started_at: datetime
    finished_at: datetime
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    synthetic: bool = False

    @model_validator(mode="after")
    def validate_result(self) -> ExecutionResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be earlier than started_at")
        if self.status == "succeeded" and self.exit_code != 0:
            raise ValueError("successful execution requires exit_code=0")
        return self


class SourceSnapshot(ContractModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    kind: Literal["baseline", "candidate"]
    repository: str = Field(min_length=1)
    commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    tree_hash: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_hash: str = Field(pattern=SHA256_PATTERN)
    worktree_uri: str = Field(min_length=1)
    clean: bool
    parent_snapshot_id: UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_parent(self) -> SourceSnapshot:
        if self.kind == "baseline" and self.parent_snapshot_id is not None:
            raise ValueError("baseline snapshots cannot have a parent")
        if self.kind == "candidate" and self.parent_snapshot_id is None:
            raise ValueError("candidate snapshots require a baseline parent")
        return self


class ArtifactManifest(ContractModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID | None = None
    kind: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    source_snapshot_id: UUID | None = None
    build_recipe: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    sbom_uri: str | None = None
    signature_uri: str | None = None
    synthetic: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class EvidenceBundle(ContractModel):
    evidence_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    candidate_id: UUID | None = None
    baseline_epoch_id: UUID | None = None
    target_id: str = Field(min_length=1)
    evidence_type: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    artifact_ids: list[UUID] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    raw_uris: list[str] = Field(default_factory=list)
    synthetic: bool = False
    created_at: datetime = Field(default_factory=utcnow)
