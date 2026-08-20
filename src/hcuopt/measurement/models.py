from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN
from hcuopt.domain.enums import LeaseScope, Stage0ProbeType, Stage0RunMode


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


# ``MeasurementEvidence`` above is the Stage 0 B-line v1 wire shape.  Keep it
# unchanged for existing producers while D and B migrate to the independently
# verifiable v2 shape below.
class StrictMeasurementModel(ContractModel):
    """Deeply typed, immutable records used by measurement-evidence-v2."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


INT64_MAX = (1 << 63) - 1
UINT64_MAX = (1 << 64) - 1
MAX_PLAN_COUNT = 1_000_000


Stage0Arm = Literal["single", "baseline", "comparison"]
Stage0Segment = Literal["timer", "noise", "A1", "B1", "B2", "A2"]
ObservationPhase = Literal[
    "calibration",
    "before_run",
    "before_restart",
    "before_sample",
    "after_sample",
    "after_restart",
    "after_run",
]


class Stage0LeaseBinding(StrictMeasurementModel):
    lease_id: UUID
    lease_scope: LeaseScope
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1, le=INT64_MAX)


class Stage0AdapterProvenance(StrictMeasurementModel):
    profile: str = Field(min_length=1, max_length=200)
    capability: str = Field(min_length=1, max_length=200)
    adapter_name: str = Field(min_length=1, max_length=200)
    adapter_version: str = Field(min_length=1, max_length=200)
    implementation_kind: Literal["real", "fake"]
    source_commit: str | None = Field(default=None, pattern=GIT_COMMIT_PATTERN)


class Stage0EvidenceBinding(StrictMeasurementModel):
    """Immutable control-plane identity copied into every raw evidence file."""

    task_id: UUID
    stage0_run_id: UUID
    target_snapshot_id: UUID
    target_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    target_fingerprint: str = Field(pattern=SHA256_PATTERN)
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    workload_id: str = Field(min_length=1, max_length=200)
    probe_type: Stage0ProbeType
    run_mode: Stage0RunMode
    protocol_version: str = Field(min_length=1, max_length=200)
    protocol_hash: str = Field(pattern=SHA256_PATTERN)
    metric_name: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=50)
    measurement_id: UUID
    lease: Stage0LeaseBinding

    @model_validator(mode="after")
    def require_exclusive_formal_lease(self) -> Stage0EvidenceBinding:
        if (
            self.run_mode is Stage0RunMode.FORMAL
            and self.lease.lease_scope is not LeaseScope.EXCLUSIVE
        ):
            raise ValueError("formal measurement evidence requires an exclusive lease")
        return self


class MeasurementPlanV2(StrictMeasurementModel):
    restart_count: int = Field(ge=1, le=MAX_PLAN_COUNT)
    warmup_count: int = Field(ge=0, le=MAX_PLAN_COUNT)
    batch_iterations: int = Field(ge=1, le=MAX_PLAN_COUNT)
    segment_order: tuple[Stage0Segment, ...] = Field(min_length=1)
    samples_per_segment: int = Field(ge=1, le=MAX_PLAN_COUNT)

    @field_validator("segment_order", mode="before")
    @classmethod
    def freeze_segment_order(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_segment_order(self) -> MeasurementPlanV2:
        if len(set(self.segment_order)) != len(self.segment_order):
            raise ValueError("segment_order cannot contain duplicate segments")
        paired = {"A1", "B1", "B2", "A2"}
        present = paired.intersection(self.segment_order)
        if present and self.segment_order != ("A1", "B1", "B2", "A2"):
            raise ValueError("paired signal segment_order must be A1,B1,B2,A2")
        if not present and len(self.segment_order) != 1:
            raise ValueError("single-arm measurement plans require exactly one segment")
        return self

    @property
    def expected_sample_count(self) -> int:
        return self.restart_count * len(self.segment_order) * self.samples_per_segment


class ClockCalibrationPointV2(StrictMeasurementModel):
    point_ordinal: int = Field(ge=0, le=MAX_PLAN_COUNT)
    device_ticks: int = Field(ge=0, le=UINT64_MAX)
    host_started_monotonic_ns: int = Field(ge=0, le=INT64_MAX)
    host_finished_monotonic_ns: int = Field(ge=0, le=INT64_MAX)

    @model_validator(mode="after")
    def validate_host_interval(self) -> ClockCalibrationPointV2:
        if self.host_finished_monotonic_ns < self.host_started_monotonic_ns:
            raise ValueError(
                "host_finished_monotonic_ns must not precede host_started_monotonic_ns"
            )
        return self

    @property
    def host_midpoint_ns(self) -> int:
        return (
            self.host_started_monotonic_ns
            + (self.host_finished_monotonic_ns - self.host_started_monotonic_ns) // 2
        )


class ClockCalibrationV2(StrictMeasurementModel):
    device_name: str = Field(min_length=1, max_length=200)
    device_index: int = Field(ge=0, le=MAX_PLAN_COUNT)
    timer_resolution_ns: float = Field(gt=0)
    ns_per_tick: float = Field(gt=0)
    max_residual_ns: float = Field(ge=0)
    points: tuple[ClockCalibrationPointV2, ...] = Field(min_length=3)
    resolution_tick_deltas: tuple[int, ...] = Field(min_length=3)

    @field_validator("points", "resolution_tick_deltas", mode="before")
    @classmethod
    def freeze_calibration_sequences(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_calibration_points(self) -> ClockCalibrationV2:
        if any(
            isinstance(value, bool) or value <= 0 or value > UINT64_MAX
            for value in self.resolution_tick_deltas
        ):
            raise ValueError("resolution_tick_deltas must contain positive integers")
        if [point.point_ordinal for point in self.points] != list(range(len(self.points))):
            raise ValueError("calibration point ordinals must be contiguous and ordered")
        for previous, current in zip(self.points, self.points[1:], strict=False):
            if current.device_ticks <= previous.device_ticks:
                raise ValueError("calibration device ticks must strictly increase")
            if current.host_midpoint_ns <= previous.host_midpoint_ns:
                raise ValueError("calibration host time must strictly increase")
        return self


class DeviceTelemetryV2(StrictMeasurementModel):
    device_index: int = Field(ge=0, le=MAX_PLAN_COUNT)
    temperature_c: float = Field(ge=-273.15)
    hotspot_temperature_c: float | None = Field(default=None, ge=-273.15)
    sclk_mhz: float = Field(gt=0)
    mclk_mhz: float = Field(gt=0)
    performance_level: str = Field(min_length=1, max_length=100)
    power_w: float = Field(ge=0)


class CacheTelemetryV2(StrictMeasurementModel):
    state: Literal["cold", "warm", "flushed", "unknown"]
    cleared_before_sample: bool
    identity_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)


class BackgroundProcessV2(StrictMeasurementModel):
    process_id: int = Field(ge=1, le=INT64_MAX)
    executable: str = Field(min_length=1, max_length=1000)
    command_line: str = Field(min_length=1, max_length=4000)
    uses_accelerator: bool
    device_memory_bytes: int = Field(ge=0, le=INT64_MAX)
    managed_by_stage0: bool


class TelemetrySnapshotV2(StrictMeasurementModel):
    device: DeviceTelemetryV2
    cache: CacheTelemetryV2
    background_processes: tuple[BackgroundProcessV2, ...] = ()

    @field_validator("background_processes", mode="before")
    @classmethod
    def freeze_background_processes(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def reject_duplicate_processes(self) -> TelemetrySnapshotV2:
        process_ids = [process.process_id for process in self.background_processes]
        if len(process_ids) != len(set(process_ids)):
            raise ValueError("background process IDs must be unique within a snapshot")
        return self


class DynamicObservationV2(StrictMeasurementModel):
    phase: ObservationPhase
    captured_monotonic_ns: int = Field(ge=0, le=INT64_MAX)
    restart_ordinal: int | None = Field(default=None, ge=0, le=MAX_PLAN_COUNT)
    acquisition_ordinal: int | None = Field(default=None, ge=0, le=MAX_PLAN_COUNT)
    telemetry: TelemetrySnapshotV2

    @model_validator(mode="after")
    def validate_phase_scope(self) -> DynamicObservationV2:
        restart_phases = {
            "before_restart",
            "before_sample",
            "after_sample",
            "after_restart",
        }
        sample_phases = {"before_sample", "after_sample"}
        if self.phase in restart_phases and self.restart_ordinal is None:
            raise ValueError(f"{self.phase} observation requires restart_ordinal")
        if self.phase in sample_phases and self.acquisition_ordinal is None:
            raise ValueError(f"{self.phase} observation requires acquisition_ordinal")
        if self.phase not in restart_phases and self.restart_ordinal is not None:
            raise ValueError(f"{self.phase} observation cannot bind a restart")
        if self.phase not in sample_phases and self.acquisition_ordinal is not None:
            raise ValueError(f"{self.phase} observation cannot bind an acquisition")
        return self


class RawSampleV2(StrictMeasurementModel):
    process_id: int = Field(ge=1, le=INT64_MAX)
    restart_ordinal: int = Field(ge=0, le=MAX_PLAN_COUNT)
    arm: Stage0Arm
    segment: Stage0Segment
    acquisition_ordinal: int = Field(ge=0, le=MAX_PLAN_COUNT)
    segment_sample_ordinal: int = Field(ge=0, le=MAX_PLAN_COUNT)
    started_monotonic_ns: int = Field(ge=0, le=INT64_MAX)
    finished_monotonic_ns: int = Field(ge=0, le=INT64_MAX)
    started_device_ticks: int = Field(ge=0, le=UINT64_MAX)
    finished_device_ticks: int = Field(ge=0, le=UINT64_MAX)
    batch_iterations: int = Field(ge=1, le=MAX_PLAN_COUNT)

    @model_validator(mode="after")
    def validate_sample(self) -> RawSampleV2:
        if self.finished_monotonic_ns <= self.started_monotonic_ns:
            raise ValueError("finished_monotonic_ns must follow started_monotonic_ns")
        if self.finished_device_ticks <= self.started_device_ticks:
            raise ValueError("finished_device_ticks must follow started_device_ticks")
        expected_arm: Stage0Arm
        if self.segment in {"A1", "A2"}:
            expected_arm = "baseline"
        elif self.segment in {"B1", "B2"}:
            expected_arm = "comparison"
        else:
            expected_arm = "single"
        if self.arm != expected_arm:
            raise ValueError(f"segment {self.segment} requires arm={expected_arm}")
        return self


class MeasurementEvidenceV2(StrictMeasurementModel):
    """Canonical raw timing evidence consumed by the Stage 0 D verifier."""

    schema_version: Literal["measurement-evidence-v2"] = "measurement-evidence-v2"
    binding: Stage0EvidenceBinding
    plan: MeasurementPlanV2
    calibration: ClockCalibrationV2
    samples: tuple[RawSampleV2, ...]
    observations: tuple[DynamicObservationV2, ...] = Field(min_length=2)
    adapter_provenance: tuple[Stage0AdapterProvenance, ...] = Field(min_length=1)
    synthetic: bool = False

    @field_validator("samples", "observations", "adapter_provenance", mode="before")
    @classmethod
    def freeze_collections(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> MeasurementEvidenceV2:
        phases = [observation.phase for observation in self.observations]
        if phases.count("before_run") != 1 or phases.count("after_run") != 1:
            raise ValueError(
                "measurement evidence requires exactly one before_run and after_run"
            )
        if self.binding.run_mode is Stage0RunMode.FORMAL:
            if self.synthetic:
                raise ValueError("formal measurement evidence cannot be synthetic")
            if any(item.implementation_kind != "real" for item in self.adapter_provenance):
                raise ValueError("formal measurement evidence requires real provenance")

        captured = [observation.captured_monotonic_ns for observation in self.observations]
        if any(
            current <= previous
            for previous, current in zip(captured, captured[1:], strict=False)
        ):
            raise ValueError("observation timestamps must be strictly increasing")
        if self.observations[0].phase != "before_run" or self.observations[-1].phase != "after_run":
            raise ValueError("before_run and after_run must bound all observations")

        if len(self.samples) != self.plan.expected_sample_count:
            raise ValueError(
                "raw sample count does not match restart/segment/sample measurement plan"
            )
        if [sample.acquisition_ordinal for sample in self.samples] != list(
            range(len(self.samples))
        ):
            raise ValueError("sample acquisition ordinals must be contiguous and ordered")
        for previous, current in zip(self.samples, self.samples[1:], strict=False):
            if current.started_monotonic_ns < previous.finished_monotonic_ns:
                raise ValueError("host sample timestamps must be ordered and non-overlapping")
            if (
                current.restart_ordinal == previous.restart_ordinal
                and current.started_device_ticks < previous.finished_device_ticks
            ):
                raise ValueError("device sample ticks must be ordered and non-overlapping")

        process_by_restart: dict[int, int] = {}
        for sample in self.samples:
            if sample.restart_ordinal >= self.plan.restart_count:
                raise ValueError("sample restart_ordinal is outside the measurement plan")
            known_process = process_by_restart.setdefault(sample.restart_ordinal, sample.process_id)
            if known_process != sample.process_id:
                raise ValueError("one restart group cannot contain multiple process IDs")
            if sample.batch_iterations != self.plan.batch_iterations:
                raise ValueError("sample batch_iterations does not match the measurement plan")

        if set(process_by_restart) != set(range(self.plan.restart_count)):
            raise ValueError("every planned restart must have samples")
        if len(set(process_by_restart.values())) != self.plan.restart_count:
            raise ValueError("independent restarts require distinct process IDs")

        observation_keys: set[tuple[ObservationPhase, int | None, int | None]] = set()
        for observation in self.observations:
            if (
                observation.restart_ordinal is not None
                and observation.restart_ordinal >= self.plan.restart_count
            ):
                raise ValueError("observation restart_ordinal is outside the measurement plan")
            if observation.acquisition_ordinal is not None:
                if observation.acquisition_ordinal >= len(self.samples):
                    raise ValueError(
                        "observation acquisition_ordinal is outside the measurement plan"
                    )
                sample = self.samples[observation.acquisition_ordinal]
                if sample.restart_ordinal != observation.restart_ordinal:
                    raise ValueError("observation acquisition does not match its restart")
            key = (
                observation.phase,
                observation.restart_ordinal,
                observation.acquisition_ordinal,
            )
            if key in observation_keys:
                raise ValueError("duplicate observation scope")
            observation_keys.add(key)

        before_run = next(
            observation for observation in self.observations if observation.phase == "before_run"
        )
        after_run = next(
            observation for observation in self.observations if observation.phase == "after_run"
        )
        if before_run.captured_monotonic_ns > self.samples[0].started_monotonic_ns:
            raise ValueError("before_run telemetry must precede all timing samples")
        if max(point.host_finished_monotonic_ns for point in self.calibration.points) > (
            before_run.captured_monotonic_ns
        ):
            raise ValueError("clock calibration must finish before before_run telemetry")
        if after_run.captured_monotonic_ns < self.samples[-1].finished_monotonic_ns:
            raise ValueError("after_run telemetry must follow all timing samples")
        samples_by_restart = {
            restart: tuple(
                sample for sample in self.samples if sample.restart_ordinal == restart
            )
            for restart in range(self.plan.restart_count)
        }
        for observation in self.observations:
            if observation.phase in {"before_sample", "after_sample"}:
                sample = self.samples[observation.acquisition_ordinal]  # type: ignore[index]
                if (
                    observation.phase == "before_sample"
                    and observation.captured_monotonic_ns > sample.started_monotonic_ns
                ):
                    raise ValueError("before_sample telemetry must precede its timing sample")
                if (
                    observation.phase == "after_sample"
                    and observation.captured_monotonic_ns < sample.finished_monotonic_ns
                ):
                    raise ValueError("after_sample telemetry must follow its timing sample")
            elif observation.phase in {"before_restart", "after_restart"}:
                restart_samples = samples_by_restart[observation.restart_ordinal]  # type: ignore[index]
                if (
                    observation.phase == "before_restart"
                    and observation.captured_monotonic_ns
                    > restart_samples[0].started_monotonic_ns
                ):
                    raise ValueError("before_restart telemetry must precede its restart")
                if (
                    observation.phase == "after_restart"
                    and observation.captured_monotonic_ns
                    < restart_samples[-1].finished_monotonic_ns
                ):
                    raise ValueError("after_restart telemetry must follow its restart")

        for restart_ordinal in range(self.plan.restart_count):
            restart_samples = [
                sample for sample in self.samples if sample.restart_ordinal == restart_ordinal
            ]
            expected_segments = [
                segment
                for segment in self.plan.segment_order
                for _ in range(self.plan.samples_per_segment)
            ]
            if [sample.segment for sample in restart_samples] != expected_segments:
                raise ValueError("sample segments do not match the planned acquisition order")
            for segment in self.plan.segment_order:
                ordinals = [
                    sample.segment_sample_ordinal
                    for sample in restart_samples
                    if sample.segment == segment
                ]
                if ordinals != list(range(self.plan.samples_per_segment)):
                    raise ValueError("segment sample ordinals must be contiguous and ordered")
        return self
