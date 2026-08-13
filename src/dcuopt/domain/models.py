from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from dcuopt.domain.enums import (
    CandidateState,
    GateResult,
    HotPatchCapability,
    LeaseState,
    OptimizationTrack,
    ProfilerCapability,
    ProjectMode,
    ReleaseMode,
)
from dcuopt.domain.errors import ContractError, StaleFencingToken


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Stage0Evidence:
    measurement: GateResult
    profiler: ProfilerCapability
    hot_patch: HotPatchCapability
    hardware_fingerprint: str
    software_fingerprint: str
    timer_resolution_ns: float | None = None
    noise_sigma_ns: float | None = None
    noise_cv: float | None = None
    mde_ratio: float | None = None
    evidence_uri: str | None = None

    def __post_init__(self) -> None:
        if not self.hardware_fingerprint or not self.software_fingerprint:
            raise ContractError("stage 0 requires hardware and software fingerprints")
        if self.measurement is GateResult.PASS:
            required = {
                "timer_resolution_ns": self.timer_resolution_ns,
                "noise_sigma_ns": self.noise_sigma_ns,
                "noise_cv": self.noise_cv,
                "mde_ratio": self.mde_ratio,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ContractError(f"measurement pass requires: {', '.join(missing)}")
            if any(float(value) < 0 for value in required.values() if value is not None):
                raise ContractError("measurement values must be non-negative")


@dataclass(frozen=True, slots=True)
class Stage0Report:
    mode: ProjectMode
    reasons: tuple[str, ...]
    automatic_release_allowed: bool = False


@dataclass(frozen=True, slots=True)
class BaselineEpoch:
    epoch_id: UUID
    hardware_fingerprint: str
    software_fingerprint: str
    workload_id: str
    configuration_hash: str
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class OptimizationCandidate:
    task_id: UUID
    round_id: UUID
    track: OptimizationTrack
    baseline_epoch_id: UUID
    source_hash: str
    target_hardware: str
    model_id: str
    framework_version: str
    workload_id: str
    build_recipe: dict[str, Any]
    correctness_recipe: dict[str, Any]
    benchmark_recipe: dict[str, Any]
    release_mode: ReleaseMode
    candidate_id: UUID = field(default_factory=uuid4)
    parent_candidate_id: UUID | None = None
    state: CandidateState = CandidateState.PROPOSED

    def __post_init__(self) -> None:
        required = {
            "source_hash": self.source_hash,
            "target_hardware": self.target_hardware,
            "model_id": self.model_id,
            "framework_version": self.framework_version,
            "workload_id": self.workload_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ContractError(f"candidate requires: {', '.join(missing)}")


@dataclass(slots=True)
class ResourceLease:
    resource_id: str
    owner_id: str | None = None
    state: LeaseState = LeaseState.AVAILABLE
    fencing_token: int = 0
    lease_id: UUID = field(default_factory=uuid4)

    def assert_token(self, token: int) -> None:
        if token != self.fencing_token:
            raise StaleFencingToken(
                f"token {token} is stale; current fencing token is {self.fencing_token}"
            )

