from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import LeaseScope
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.models import (
    ClockCalibrationV2,
    RawEvidenceFileV2,
    Stage0AdapterProvenance,
    Stage0LeaseBinding,
    StrictMeasurementModel,
    TelemetrySnapshotV2,
)

M1Arm = Literal["baseline", "candidate"]


class M1Stage0ReportReference(StrictMeasurementModel):
    uri: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    input_digest: str = Field(pattern=SHA256_PATTERN)
    protocol_version: str = Field(min_length=1, max_length=200)
    protocol_hash: str = Field(pattern=SHA256_PATTERN)


class M1Stage0Authority(StrictMeasurementModel):
    """Detectability authority copied from one independently verified Formal report."""

    report: M1Stage0ReportReference
    metric_name: Literal["kernel_elapsed"]
    unit: Literal["ns"]
    timer_resolution_ns: float = Field(gt=0)
    noise_sigma_ns: float = Field(ge=0)
    noise_cv: float = Field(ge=0)
    mde_ratio: float = Field(ge=0)
    alpha: float = Field(gt=0, lt=1)
    power: float = Field(gt=0, lt=1)
    bootstrap_resamples: int = Field(ge=1000)
    bootstrap_method: Literal["percentile"]
    bootstrap_seed_source: Literal["input_evidence_sha256"]


class M1MeasurementBinding(StrictMeasurementModel):
    task_id: UUID
    candidate_id: UUID
    round_id: UUID
    baseline_epoch_id: UUID
    stage0_run_id: UUID
    target_snapshot_id: UUID
    target_id: str = Field(min_length=1, max_length=128)
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    configuration_hash: str = Field(pattern=SHA256_PATTERN)
    image_digest: str = Field(pattern=SHA256_PATTERN)
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_id: UUID
    artifact_content_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_id: UUID
    lease: Stage0LeaseBinding

    @model_validator(mode="after")
    def require_exclusive_lease(self) -> M1MeasurementBinding:
        if self.lease.lease_scope is not LeaseScope.EXCLUSIVE:
            raise ValueError("M1 performance evidence requires an exclusive lease")
        return self


class M1MeasurementPlan(StrictMeasurementModel):
    protocol_version: Literal["m1-kernel-performance-v1"] = "m1-kernel-performance-v1"
    metric_name: Literal["kernel_elapsed"] = "kernel_elapsed"
    unit: Literal["ns"] = "ns"
    acquisition_order: tuple[M1Arm, ...] = ("baseline", "candidate", "candidate", "baseline")
    warmup_count: int = Field(ge=0, le=1_000_000)
    samples_per_acquisition: int = Field(ge=1, le=1_000_000)
    batch_iterations: int = Field(ge=1, le=1_000_000)
    stage0_authority: M1Stage0Authority

    @field_validator("acquisition_order", mode="before")
    @classmethod
    def freeze_order(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_balanced_abba(self) -> M1MeasurementPlan:
        if len(self.acquisition_order) < 4 or len(self.acquisition_order) % 4:
            raise ValueError("M1 acquisition order must contain complete ABBA groups")
        for offset in range(0, len(self.acquisition_order), 4):
            if self.acquisition_order[offset : offset + 4] != (
                "baseline",
                "candidate",
                "candidate",
                "baseline",
            ):
                raise ValueError(
                    "M1 acquisition groups must use "
                    "Baseline-Candidate-Candidate-Baseline"
                )
        if self.metric_name != self.stage0_authority.metric_name:
            raise ValueError("M1 metric differs from the Formal Stage 0 authority")
        if self.unit != self.stage0_authority.unit:
            raise ValueError("M1 unit differs from the Formal Stage 0 authority")
        return self

    @property
    def expected_sample_count(self) -> int:
        return len(self.acquisition_order) * self.samples_per_acquisition

    @property
    def process_restart_count(self) -> int:
        return len(self.acquisition_order)


class M1ActivationEvidence(StrictMeasurementModel):
    arm: M1Arm
    activation_mode: Literal["baseline", "startup_overlay"]
    image_digest: str = Field(pattern=SHA256_PATTERN)
    loaded_artifact_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    import_attestation: RawEvidenceFileV2 | None = None
    cache_namespace_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def bind_arm(self) -> M1ActivationEvidence:
        if self.arm == "baseline":
            if self.activation_mode != "baseline" or self.loaded_artifact_hash is not None:
                raise ValueError("baseline process cannot load the Candidate Artifact")
        elif (
            self.activation_mode != "startup_overlay"
            or self.loaded_artifact_hash is None
            or self.import_attestation is None
        ):
            raise ValueError("candidate process requires startup Overlay import attestation")
        return self


class M1WorkloadTiming(StrictMeasurementModel):
    process_id: int = Field(ge=1)
    process_start_token: str = Field(min_length=1, max_length=200)
    batch_iterations: int = Field(ge=1, le=1_000_000)
    started_monotonic_ns: int = Field(ge=0)
    finished_monotonic_ns: int = Field(ge=0)
    started_device_ticks: int = Field(ge=0)
    finished_device_ticks: int = Field(ge=0)

    @model_validator(mode="after")
    def require_positive_intervals(self) -> M1WorkloadTiming:
        if self.finished_monotonic_ns <= self.started_monotonic_ns:
            raise ValueError("M1 host timing interval must be positive")
        if self.finished_device_ticks <= self.started_device_ticks:
            raise ValueError("M1 device timing interval must be positive")
        return self


class M1RawSample(M1WorkloadTiming):
    arm: M1Arm
    acquisition_ordinal: int = Field(ge=0, le=1_000_000)
    sample_ordinal: int = Field(ge=0, le=1_000_000)


class M1AcquisitionEvidence(StrictMeasurementModel):
    acquisition_ordinal: int = Field(ge=0, le=1_000_000)
    arm: M1Arm
    process_id: int = Field(ge=1)
    process_start_token: str = Field(min_length=1, max_length=200)
    activation: M1ActivationEvidence
    started_lifecycle: RawEvidenceFileV2
    reaped_lifecycle: RawEvidenceFileV2
    before: TelemetrySnapshotV2
    after: TelemetrySnapshotV2
    samples: tuple[M1RawSample, ...] = Field(min_length=1)

    @field_validator("samples", mode="before")
    @classmethod
    def freeze_samples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def bind_acquisition(self) -> M1AcquisitionEvidence:
        identity = (self.process_id, self.process_start_token)
        if self.activation.arm != self.arm:
            raise ValueError("activation evidence belongs to another arm")
        for ordinal, sample in enumerate(self.samples):
            if (
                sample.arm != self.arm
                or sample.acquisition_ordinal != self.acquisition_ordinal
                or sample.sample_ordinal != ordinal
                or (sample.process_id, sample.process_start_token) != identity
            ):
                raise ValueError("raw sample does not match its acquisition")
        return self


class M1MeasurementEvidence(StrictMeasurementModel):
    schema_version: Literal["m1-measurement-evidence-v1"] = "m1-measurement-evidence-v1"
    binding: M1MeasurementBinding
    plan: M1MeasurementPlan
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    calibration: ClockCalibrationV2
    acquisitions: tuple[M1AcquisitionEvidence, ...] = Field(min_length=4)
    adapter_provenance: tuple[Stage0AdapterProvenance, ...] = Field(min_length=1)
    producer_verdict: Literal[None] = None
    synthetic: Literal[False] = False

    @field_validator("acquisitions", "adapter_provenance", mode="before")
    @classmethod
    def freeze_collections(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_evidence(self) -> M1MeasurementEvidence:
        if self.plan_hash != m1_plan_hash(self.plan):
            raise ValueError("M1 plan hash does not match the canonical plan")
        if any(item.implementation_kind != "real" for item in self.adapter_provenance):
            raise ValueError("M1 evidence requires real Adapter provenance")
        if len(self.acquisitions) != len(self.plan.acquisition_order):
            raise ValueError("M1 acquisitions do not match the hashed plan")
        identities: set[tuple[int, str]] = set()
        sample_count = 0
        previous_finished = -1
        for ordinal, (arm, acquisition) in enumerate(
            zip(self.plan.acquisition_order, self.acquisitions, strict=True)
        ):
            if acquisition.acquisition_ordinal != ordinal or acquisition.arm != arm:
                raise ValueError("M1 acquisition order differs from the hashed plan")
            identity = (acquisition.process_id, acquisition.process_start_token)
            if identity in identities:
                raise ValueError("every M1 acquisition requires a fresh process identity")
            identities.add(identity)
            if len(acquisition.samples) != self.plan.samples_per_acquisition:
                raise ValueError("M1 acquisition sample count differs from the hashed plan")
            if acquisition.activation.image_digest != self.binding.image_digest:
                raise ValueError("M1 process used a different immutable image")
            if arm == "candidate" and (
                acquisition.activation.loaded_artifact_hash
                != self.binding.artifact_content_hash
            ):
                raise ValueError("Candidate process loaded a different Artifact")
            for sample in acquisition.samples:
                if sample.batch_iterations != self.plan.batch_iterations:
                    raise ValueError("M1 sample batch size differs from the hashed plan")
                if sample.started_monotonic_ns < previous_finished:
                    raise ValueError("M1 samples must be ordered and non-overlapping")
                previous_finished = sample.finished_monotonic_ns
                sample_count += 1
        if sample_count != self.plan.expected_sample_count:
            raise ValueError("M1 total sample count differs from the hashed plan")
        return self


def m1_plan_hash(plan: M1MeasurementPlan) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(plan)).hexdigest()
