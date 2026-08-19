from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN


class MeasurementModel(ContractModel):
    """Immutable internal protocol records retained in raw evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MeasurementPlan(MeasurementModel):
    protocol_version: str = Field(min_length=1, max_length=200)
    metric_name: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=50)
    warmup_count: int = Field(ge=0)
    repeat_count: int = Field(ge=1)
    process_restart_count: int = Field(ge=0)
    batched_loop_count: int = Field(ge=1)
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    synthetic: bool = False


class ClockCalibration(MeasurementModel):
    device_name: str = Field(min_length=1, max_length=200)
    device_origin_ticks: int = Field(ge=0)
    host_origin_ns: int = Field(ge=0)
    ns_per_tick: float = Field(gt=0)
    max_residual_ns: float = Field(ge=0)
    point_count: int = Field(ge=2)


class RawSample(MeasurementModel):
    restart_ordinal: int = Field(ge=0)
    sample_ordinal: int = Field(ge=0)
    started_monotonic_ns: int = Field(ge=0)
    finished_monotonic_ns: int = Field(ge=0)
    batch_iterations: int = Field(ge=1)
    started_device_ticks: int | None = Field(default=None, ge=0)
    finished_device_ticks: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_time_order(self) -> RawSample:
        if self.finished_monotonic_ns < self.started_monotonic_ns:
            raise ValueError("finished_monotonic_ns must not precede started_monotonic_ns")
        if (
            self.started_device_ticks is not None
            and self.finished_device_ticks is not None
            and self.finished_device_ticks < self.started_device_ticks
        ):
            raise ValueError("finished_device_ticks must not precede started_device_ticks")
        return self

    @property
    def elapsed_ns(self) -> int:
        return self.finished_monotonic_ns - self.started_monotonic_ns


class DynamicObservation(MeasurementModel):
    captured_monotonic_ns: int = Field(ge=0)
    telemetry: dict[str, Any] = Field(default_factory=dict)
    background_processes: list[dict[str, Any]] = Field(default_factory=list)
    cache_state: dict[str, Any] = Field(default_factory=dict)
    lease: dict[str, Any] = Field(default_factory=dict)


class MeasurementEvidence(MeasurementModel):
    protocol_version: str = Field(min_length=1, max_length=200)
    stable_fingerprint: str = Field(pattern=SHA256_PATTERN)
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    observations: list[DynamicObservation] = Field(default_factory=list)
    calibration: ClockCalibration | None = None
    raw_samples: list[RawSample] = Field(default_factory=list)
    synthetic: bool = False

    @model_validator(mode="after")
    def reject_synthetic_measurements(self) -> MeasurementEvidence:
        if self.synthetic and (self.raw_samples or self.calibration is not None):
            raise ValueError("synthetic measurement evidence cannot contain timing samples")
        return self
