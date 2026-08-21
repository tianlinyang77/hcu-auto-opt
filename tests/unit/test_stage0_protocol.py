from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from hcuopt.domain.enums import LeaseScope, Stage0ProbeType, Stage0RunMode
from hcuopt.evaluation.stage0_protocol import (
    Stage0ProtocolError,
    load_registered_stage0_protocol,
    load_stage0_protocol,
    protocol_sha256,
)
from hcuopt.measurement.models import (
    CacheTelemetryV2,
    ClockCalibrationPointV2,
    ClockCalibrationV2,
    DeviceTelemetryV2,
    DynamicObservationV2,
    MeasurementEvidenceV2,
    MeasurementPlanV2,
    RawSampleV2,
    Stage0AdapterProvenance,
    Stage0EvidenceBinding,
    Stage0LeaseBinding,
    TelemetrySnapshotV2,
)

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ROOT = ROOT / "config" / "stage0"
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def _telemetry() -> TelemetrySnapshotV2:
    return TelemetrySnapshotV2(
        device=DeviceTelemetryV2(
            device_index=7,
            temperature_c=55.0,
            sclk_mhz=1350.0,
            mclk_mhz=875.0,
            performance_level="manual",
            power_w=210.0,
        ),
        cache=CacheTelemetryV2(
            state="flushed",
            cleared_before_sample=True,
        ),
    )


def _binding(*, lease_scope: LeaseScope = LeaseScope.EXCLUSIVE) -> Stage0EvidenceBinding:
    return Stage0EvidenceBinding(
        task_id=UUID(int=1),
        stage0_run_id=UUID(int=2),
        target_snapshot_id=UUID(int=3),
        target_id="nmz36-sglang-0.5.12",
        target_fingerprint=SHA_A,
        environment_fingerprint=SHA_B,
        workload_id="stage0-kernel-v1",
        probe_type=Stage0ProbeType.NOISE,
        run_mode=Stage0RunMode.FORMAL,
        protocol_version="s0-g0-v1",
        protocol_hash=SHA_A,
        metric_name="kernel_elapsed",
        unit="ns",
        measurement_id=UUID(int=4),
        lease=Stage0LeaseBinding(
            lease_id=UUID(int=5),
            lease_scope=lease_scope,
            resource_id="hcu-7",
            fencing_token=7,
        ),
    )


def _lifecycle_reference(restart: int, event: str) -> dict[str, str]:
    return {
        "uri": f"file:///evidence/restart-{restart}-{event}.json",
        "sha256": SHA_A if event == "started" else SHA_B,
    }


def _evidence() -> MeasurementEvidenceV2:
    samples = tuple(
        RawSampleV2(
            process_id=100 + restart,
            process_start_token=f"linux-proc-startticks:{1_000 + restart}",
            restart_ordinal=restart,
            arm="single",
            segment="noise",
            acquisition_ordinal=restart * 2 + ordinal,
            segment_sample_ordinal=ordinal,
            started_monotonic_ns=1000 + restart * 100 + ordinal * 10,
            finished_monotonic_ns=1005 + restart * 100 + ordinal * 10,
            started_device_ticks=2000 + restart * 100 + ordinal * 10,
            finished_device_ticks=2005 + restart * 100 + ordinal * 10,
            batch_iterations=100,
        )
        for restart in range(2)
        for ordinal in range(2)
    )
    return MeasurementEvidenceV2(
        binding=_binding(),
        plan=MeasurementPlanV2(
            restart_count=2,
            warmup_count=10,
            batch_iterations=100,
            segment_order=("noise",),
            samples_per_segment=2,
        ),
        calibration=ClockCalibrationV2(
            device_name="hcu-event",
            device_index=7,
            timer_resolution_ns=1.0,
            ns_per_tick=1.0,
            max_residual_ns=0.1,
            points=(
                ClockCalibrationPointV2(
                    point_ordinal=0,
                    device_ticks=10,
                    host_started_monotonic_ns=100,
                    host_finished_monotonic_ns=102,
                ),
                ClockCalibrationPointV2(
                    point_ordinal=1,
                    device_ticks=20,
                    host_started_monotonic_ns=110,
                    host_finished_monotonic_ns=112,
                ),
                ClockCalibrationPointV2(
                    point_ordinal=2,
                    device_ticks=30,
                    host_started_monotonic_ns=120,
                    host_finished_monotonic_ns=122,
                ),
            ),
            resolution_tick_deltas=(1, 1, 1),
        ),
        samples=samples,
        observations=(
            DynamicObservationV2(
                phase="before_run",
                captured_monotonic_ns=900,
                telemetry=_telemetry(),
            ),
            DynamicObservationV2(
                phase="before_restart",
                captured_monotonic_ns=950,
                restart_ordinal=0,
                process_id=100,
                process_start_token="linux-proc-startticks:1000",
                process_lifecycle_record=_lifecycle_reference(0, "started"),
                telemetry=_telemetry(),
            ),
            DynamicObservationV2(
                phase="after_restart",
                captured_monotonic_ns=1020,
                restart_ordinal=0,
                process_id=100,
                process_start_token="linux-proc-startticks:1000",
                process_lifecycle_record=_lifecycle_reference(0, "reaped"),
                telemetry=_telemetry(),
            ),
            DynamicObservationV2(
                phase="before_restart",
                captured_monotonic_ns=1050,
                restart_ordinal=1,
                process_id=101,
                process_start_token="linux-proc-startticks:1001",
                process_lifecycle_record=_lifecycle_reference(1, "started"),
                telemetry=_telemetry(),
            ),
            DynamicObservationV2(
                phase="after_restart",
                captured_monotonic_ns=1120,
                restart_ordinal=1,
                process_id=101,
                process_start_token="linux-proc-startticks:1001",
                process_lifecycle_record=_lifecycle_reference(1, "reaped"),
                telemetry=_telemetry(),
            ),
            DynamicObservationV2(
                phase="after_run",
                captured_monotonic_ns=2_000,
                telemetry=_telemetry(),
            ),
        ),
        adapter_provenance=(
            Stage0AdapterProvenance(
                profile="nmz36-stage0-measurement-v1",
                capability="stage0_probe",
                adapter_name="EvidenceMeasurementHarness",
                adapter_version="2",
                implementation_kind="real",
                source_commit="1" * 40,
            ),
        ),
    )


def test_registered_protocol_has_locked_stage0_defaults() -> None:
    loaded = load_registered_stage0_protocol("s0-g0-v1", config_root=PROTOCOL_ROOT)
    packaged = load_registered_stage0_protocol("s0-g0-v1")

    assert loaded.protocol.sampling.restart_count == 10
    assert loaded.protocol.sampling.noise_samples_per_restart == 30
    assert loaded.protocol.sampling.signal_segment_order == ("A1", "B1", "B2", "A2")
    assert loaded.protocol.alpha == 0.05
    assert loaded.protocol.power == 0.80
    assert loaded.protocol.bootstrap_resamples == 10_000
    assert loaded.protocol.timing_metric_name == "kernel_elapsed"
    assert loaded.protocol.timing_unit == "ns"
    assert loaded.protocol.max_cv_ratio == 0.02
    assert loaded.protocol.max_mde_ratio == 0.03
    assert loaded.protocol.environment_gates.power_gate_enabled is False
    assert loaded.protocol_hash == protocol_sha256(loaded.protocol)
    assert packaged.protocol_hash == loaded.protocol_hash
    assert packaged.canonical_bytes == loaded.canonical_bytes
    assert loaded.canonical_bytes.endswith(b"\n")


def test_registered_v2_amortizes_timer_error_over_a_protocol_bound_batch() -> None:
    loaded = load_registered_stage0_protocol("s0-g0-v2", config_root=PROTOCOL_ROOT)
    packaged = load_registered_stage0_protocol("s0-g0-v2")

    assert loaded.protocol.sampling.batch_iterations == 5000
    assert loaded.protocol.timer_gates.comparison_basis == "raw_batch_interval"
    assert packaged.protocol_hash == loaded.protocol_hash
    assert packaged.canonical_bytes == loaded.canonical_bytes


def test_protocol_hash_is_canonical_and_unknown_fields_fail(tmp_path: Path) -> None:
    original = yaml.safe_load((PROTOCOL_ROOT / "s0-g0-v1.yaml").read_text(encoding="utf-8"))
    reordered = dict(reversed(list(original.items())))
    reordered_path = tmp_path / "reordered.yaml"
    reordered_path.write_text(yaml.safe_dump(reordered, sort_keys=False), encoding="utf-8")

    expected = load_registered_stage0_protocol("s0-g0-v1", config_root=PROTOCOL_ROOT)
    actual = load_stage0_protocol(reordered_path, expected_version="s0-g0-v1")
    assert actual.protocol_hash == expected.protocol_hash

    original["client_override"] = {"max_cv_ratio": 1.0}
    invalid_path = tmp_path / "invalid.yaml"
    invalid_path.write_text(yaml.safe_dump(original), encoding="utf-8")
    with pytest.raises(Stage0ProtocolError, match="client_override"):
        load_stage0_protocol(invalid_path)


def test_only_registered_protocol_versions_can_be_selected() -> None:
    with pytest.raises(Stage0ProtocolError, match="unregistered"):
        load_registered_stage0_protocol("caller-controlled-v1", config_root=PROTOCOL_ROOT)
    with pytest.raises(Stage0ProtocolError, match="invalid"):
        load_registered_stage0_protocol("../s0-g0-v1", config_root=PROTOCOL_ROOT)


def test_raw_v2_binds_identity_processes_abba_axes_and_typed_telemetry() -> None:
    evidence = _evidence()
    restored = MeasurementEvidenceV2.model_validate_json(evidence.model_dump_json())

    assert evidence.schema_version == "measurement-evidence-v2"
    assert evidence.binding.probe_type is Stage0ProbeType.NOISE
    assert evidence.binding.run_mode is Stage0RunMode.FORMAL
    assert evidence.binding.lease.resource_id == "hcu-7"
    assert {sample.process_id for sample in evidence.samples} == {100, 101}
    assert evidence.observations[0].telemetry.device.performance_level == "manual"
    assert evidence.observations[0].telemetry.device.sclk_mhz == 1350.0
    assert restored == evidence


def test_raw_v2_is_strict_and_formal_evidence_fails_closed() -> None:
    with pytest.raises(ValidationError, match="valid integer"):
        MeasurementPlanV2.model_validate(
            {
                "restart_count": "2",
                "warmup_count": 10,
                "batch_iterations": 100,
                "segment_order": ["noise"],
                "samples_per_segment": 2,
            }
        )
    with pytest.raises(ValidationError, match="exclusive"):
        _binding(lease_scope=LeaseScope.SHARED)

    evidence = _evidence()
    duplicate_process = evidence.model_dump(mode="python")
    for sample in duplicate_process["samples"]:
        if sample["restart_ordinal"] == 1:
            sample["process_id"] = 100
            sample["process_start_token"] = "linux-proc-startticks:1000"
    for observation in duplicate_process["observations"]:
        if observation["restart_ordinal"] == 1:
            observation["process_id"] = 100
            observation["process_start_token"] = "linux-proc-startticks:1000"
    with pytest.raises(ValidationError, match="distinct process identities"):
        MeasurementEvidenceV2.model_validate(duplicate_process)

    huge_tick = evidence.model_dump(mode="python")
    huge_tick["calibration"]["points"][2]["device_ticks"] = 10**1000
    with pytest.raises(ValidationError, match="less than or equal"):
        MeasurementEvidenceV2.model_validate(huge_tick)


def test_formal_observations_are_unique_ordered_and_plan_bound() -> None:
    evidence = _evidence()
    duplicate_after = evidence.model_copy(
        update={"observations": (*evidence.observations, evidence.observations[-1])}
    )
    with pytest.raises(ValidationError, match="exactly one"):
        MeasurementEvidenceV2.model_validate(duplicate_after.model_dump(mode="python"))

    reversed_time = evidence.model_copy(
        update={
            "observations": (
                evidence.observations[0].model_copy(update={"captured_monotonic_ns": 3_000}),
                *evidence.observations[1:],
            )
        }
    )
    with pytest.raises(ValidationError, match="strictly increasing"):
        MeasurementEvidenceV2.model_validate(reversed_time.model_dump(mode="python"))

    late_before = evidence.model_copy(
        update={
            "observations": (
                evidence.observations[0].model_copy(update={"captured_monotonic_ns": 1_001}),
                evidence.observations[1].model_copy(update={"captured_monotonic_ns": 1_002}),
                *evidence.observations[2:],
            )
        }
    )
    with pytest.raises(ValidationError, match="precede all timing samples"):
        MeasurementEvidenceV2.model_validate(late_before.model_dump(mode="python"))

    empty_dry_run = evidence.model_dump(mode="python")
    empty_dry_run["binding"]["run_mode"] = Stage0RunMode.DRY_RUN
    empty_dry_run["synthetic"] = True
    empty_dry_run["observations"] = []
    with pytest.raises(ValidationError, match="at least 2 items"):
        MeasurementEvidenceV2.model_validate(empty_dry_run)

    unbounded_dry_run = evidence.model_dump(mode="python")
    unbounded_dry_run["binding"]["run_mode"] = Stage0RunMode.DRY_RUN
    unbounded_dry_run["synthetic"] = True
    unbounded_dry_run["observations"] = unbounded_dry_run["observations"][1:3]
    with pytest.raises(ValidationError, match="exactly one before_run and after_run"):
        MeasurementEvidenceV2.model_validate(unbounded_dry_run)


def test_sample_chronology_allows_device_epoch_reset_only_between_restarts() -> None:
    evidence = _evidence()
    reset_samples = tuple(
        sample.model_copy(
            update={
                "started_device_ticks": sample.started_device_ticks - 2_000
                if sample.restart_ordinal == 1
                else sample.started_device_ticks,
                "finished_device_ticks": sample.finished_device_ticks - 2_000
                if sample.restart_ordinal == 1
                else sample.finished_device_ticks,
            }
        )
        for sample in evidence.samples
    )
    reset = evidence.model_copy(update={"samples": reset_samples})
    MeasurementEvidenceV2.model_validate(reset.model_dump(mode="python"))

    overlapping_samples = list(evidence.samples)
    overlapping_samples[1] = overlapping_samples[1].model_copy(
        update={"started_device_ticks": overlapping_samples[0].finished_device_ticks - 1}
    )
    overlapping = evidence.model_copy(update={"samples": tuple(overlapping_samples)})
    with pytest.raises(ValidationError, match="device sample ticks"):
        MeasurementEvidenceV2.model_validate(overlapping.model_dump(mode="python"))


def test_formal_telemetry_requires_recorded_power() -> None:
    evidence = _evidence()
    payload = evidence.model_dump(mode="python")
    payload["observations"][0]["telemetry"]["device"]["power_w"] = None

    with pytest.raises(ValidationError, match="power_w"):
        MeasurementEvidenceV2.model_validate(payload)


def test_calibration_and_sample_axes_reject_malformed_order() -> None:
    with pytest.raises(ValidationError, match="strictly increase"):
        ClockCalibrationV2(
            device_name="hcu-event",
            device_index=7,
            timer_resolution_ns=1.0,
            ns_per_tick=1.0,
            max_residual_ns=0.0,
            points=(
                ClockCalibrationPointV2(
                    point_ordinal=0,
                    device_ticks=10,
                    host_started_monotonic_ns=100,
                    host_finished_monotonic_ns=101,
                ),
                ClockCalibrationPointV2(
                    point_ordinal=1,
                    device_ticks=10,
                    host_started_monotonic_ns=110,
                    host_finished_monotonic_ns=111,
                ),
                ClockCalibrationPointV2(
                    point_ordinal=2,
                    device_ticks=20,
                    host_started_monotonic_ns=120,
                    host_finished_monotonic_ns=121,
                ),
            ),
            resolution_tick_deltas=(1, 1, 1),
        )

    with pytest.raises(ValidationError, match="requires arm=baseline"):
        RawSampleV2(
            process_id=100,
            process_start_token="linux-proc-startticks:1000",
            restart_ordinal=0,
            arm="comparison",
            segment="A1",
            acquisition_ordinal=0,
            segment_sample_ordinal=0,
            started_monotonic_ns=1,
            finished_monotonic_ns=2,
            started_device_ticks=1,
            finished_device_ticks=2,
            batch_iterations=100,
        )
