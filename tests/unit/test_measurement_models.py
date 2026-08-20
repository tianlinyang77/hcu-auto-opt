from __future__ import annotations

import pytest
from pydantic import ValidationError

from hcuopt.measurement.models import (
    ClockCalibration,
    DynamicObservation,
    MeasurementEvidence,
    MeasurementPlan,
    RawSample,
)


def _plan(**changes: object) -> MeasurementPlan:
    values: dict[str, object] = {
        "protocol_version": "s0-measurement-v1",
        "metric_name": "kernel_elapsed",
        "unit": "ns",
        "warmup_count": 3,
        "repeat_count": 5,
        "process_restart_count": 1,
        "batched_loop_count": 16,
        "environment_fingerprint": "sha256:" + "a" * 64,
    }
    values.update(changes)
    return MeasurementPlan(**values)


def test_measurement_plan_requires_explicit_reproducibility_controls() -> None:
    plan = _plan()

    assert plan.warmup_count == 3
    assert plan.repeat_count == 5
    assert plan.process_restart_count == 1
    assert plan.batched_loop_count == 16

    with pytest.raises(ValidationError, match="repeat_count"):
        _plan(repeat_count=0)


def test_raw_sample_rejects_negative_duration() -> None:
    with pytest.raises(ValidationError, match="finished_monotonic_ns"):
        RawSample(
            restart_ordinal=0,
            sample_ordinal=0,
            process_id=101,
            process_start_token="fixture-101",
            started_monotonic_ns=20,
            finished_monotonic_ns=19,
            batch_iterations=1,
        )


def test_evidence_keeps_raw_samples_and_rejects_synthetic_timing_claims() -> None:
    sample = RawSample(
        restart_ordinal=0,
        sample_ordinal=0,
        process_id=101,
        process_start_token="fixture-101",
        started_monotonic_ns=10,
        finished_monotonic_ns=20,
        batch_iterations=1,
    )
    calibration = ClockCalibration(
        device_name="fixture",
        device_origin_ticks=1,
        host_origin_ns=10,
        ns_per_tick=2.0,
        timer_resolution_ns=100.0,
        max_residual_ns=0.0,
        point_count=2,
    )
    evidence = MeasurementEvidence(
        protocol_version="s0-measurement-v1",
        stable_fingerprint="sha256:" + "b" * 64,
        environment_fingerprint="sha256:" + "a" * 64,
        observations=[DynamicObservation(captured_monotonic_ns=10)],
        calibration=calibration,
        raw_samples=[sample],
    )

    assert evidence.raw_samples == [sample]

    with pytest.raises(ValidationError, match="synthetic"):
        MeasurementEvidence(
            protocol_version="s0-measurement-v1",
            stable_fingerprint="sha256:" + "b" * 64,
            environment_fingerprint="sha256:" + "a" * 64,
            synthetic=True,
            raw_samples=[sample],
        )


def test_legacy_measurement_evidence_remains_readable() -> None:
    sample = RawSample(
        restart_ordinal=0,
        sample_ordinal=0,
        started_monotonic_ns=10,
        finished_monotonic_ns=20,
        batch_iterations=1,
    )
    calibration = ClockCalibration(
        device_name="legacy-fixture",
        device_origin_ticks=1,
        host_origin_ns=10,
        ns_per_tick=2.0,
        max_residual_ns=0.0,
        point_count=2,
    )

    assert sample.process_id is None
    assert calibration.timer_resolution_ns is None
