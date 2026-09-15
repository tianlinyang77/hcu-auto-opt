from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

from hcuopt.adapters.profiles import REAL_STAGE0_PROFILE
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.evaluation.stage0_statistics import recompute_clock_calibration
from hcuopt.evaluation.stage0_verifier import FingerprintEvidenceV2
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.harness import EvidenceMeasurementHarness
from hcuopt.measurement.models import (
    FormalWorkloadTimingV2,
    MeasurementEvidenceV2,
    ProcessIdentity,
    ProcessLifecycleRecordV2,
)
from hcuopt.measurement.stage0 import Stage0MeasurementProbeAdapter
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target, target_fingerprint

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")


class TickingClock:
    def __init__(self) -> None:
        self.value = 0

    def now_ns(self) -> int:
        self.value += 10
        return self.value


class RawTickTimer:
    def __init__(self) -> None:
        self.value = 0

    def read_ticks(self) -> int:
        self.value += 5
        return self.value

    def measure_resolution_ns(self, sample_count: int) -> float:
        return float(min(self.sample_resolution_ticks(sample_count)))

    def sample_resolution_ticks(self, sample_count: int) -> tuple[int, ...]:
        assert sample_count >= 3
        return tuple(1 for _ in range(sample_count))


class TypedTelemetry:
    def collect(self):
        return {
            "device": {
                "device_index": 7,
                "temperature_c": 55.0,
                "hotspot_temperature_c": 55.0,
                "sclk_mhz": 1350.0,
                "mclk_mhz": 875.0,
                "performance_level": "manual",
                "power_w": 210.0,
            },
            "cache": {
                "state": "flushed",
                "cleared_before_sample": True,
                "identity_hash": None,
            },
            "background_processes": [],
        }


class Cleaner:
    def fence(self, resource_id: str, fencing_token: int):
        return {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "fenced": True,
        }

    def health_check(self, resource_id: str):
        return {"resource_id": resource_id, "healthy": True}


class SegmentedWorkload:
    def __init__(self, restart_ordinal: int, clock: TickingClock) -> None:
        self.restart_ordinal = restart_ordinal
        self.clock = clock
        self.identity = ProcessIdentity(
            pid=20_000 + restart_ordinal,
            start_token=f"linux-proc-startticks:{60_000 + restart_ordinal}",
        )
        self.alive = True
        self.segments: list[str] = []
        self.device_ticks = restart_ordinal * 100_000 + 1_000

    def process_identity(self) -> ProcessIdentity:
        return self.identity

    def synchronize(self) -> None:
        return None

    def warmup_segment(self, segment: str, iterations: int) -> None:
        self.segments.append(f"warmup:{segment}:{iterations}")

    def measure_segment_batch(
        self,
        segment: str,
        iterations: int,
    ) -> FormalWorkloadTimingV2:
        assert iterations == 100
        self.segments.append(segment)
        started_host = self.clock.now_ns()
        started_device = self.device_ticks
        self.device_ticks += 50
        finished_host = self.clock.now_ns()
        return FormalWorkloadTimingV2(
            process_id=self.identity.pid,
            process_start_token=self.identity.start_token,
            segment=segment,
            batch_iterations=iterations,
            started_monotonic_ns=started_host,
            finished_monotonic_ns=finished_host,
            started_device_ticks=started_device,
            finished_device_ticks=self.device_ticks,
        )

    def close(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class LifecycleRecorder:
    @staticmethod
    def _proc_stat_line(process_id: int, start_ticks: int) -> str:
        fields_4_through_21 = " ".join(str(value) for value in range(4, 22))
        return f"{process_id} (stage0-fixture) S {fields_4_through_21} {start_ticks}"

    def record_started(
        self,
        workload: SegmentedWorkload,
        *,
        restart_ordinal: int,
        captured_monotonic_ns: int,
    ) -> ProcessLifecycleRecordV2:
        return self._record(
            workload,
            event="started",
            restart_ordinal=restart_ordinal,
            captured_monotonic_ns=captured_monotonic_ns,
        )

    def record_reaped(
        self,
        workload: SegmentedWorkload,
        *,
        restart_ordinal: int,
        captured_monotonic_ns: int,
    ) -> ProcessLifecycleRecordV2:
        return self._record(
            workload,
            event="reaped",
            restart_ordinal=restart_ordinal,
            captured_monotonic_ns=captured_monotonic_ns,
        )

    def _record(
        self,
        workload: SegmentedWorkload,
        *,
        event: str,
        restart_ordinal: int,
        captured_monotonic_ns: int,
    ) -> ProcessLifecycleRecordV2:
        process_id = workload.identity.pid
        start_ticks = 60_000 + restart_ordinal
        return ProcessLifecycleRecordV2(
            event=event,
            restart_ordinal=restart_ordinal,
            observer_process_id=4_242,
            process_id=process_id,
            proc_stat_line=self._proc_stat_line(process_id, start_ticks),
            captured_monotonic_ns=captured_monotonic_ns,
            waitpid_result_pid=process_id if event == "reaped" else None,
            wait_status=0 if event == "reaped" else None,
        )


def _adapter() -> Stage0MeasurementProbeAdapter:
    provenance = AdapterProvenance(
        profile=REAL_STAGE0_PROFILE,
        capability="measurement_harness",
        adapter_name="EvidenceMeasurementHarness",
        adapter_version="2",
        implementation_kind="real",
    )
    clock = TickingClock()
    harness = EvidenceMeasurementHarness(
        provenance=provenance,
        stable_identity=TARGET.model_dump(mode="json"),
        workload_factory=lambda restart: SegmentedWorkload(restart, clock),
        telemetry=TypedTelemetry(),
        device_timer=RawTickTimer(),
        clock=clock,
        cleaner=Cleaner(),
        formal_workload_factory=lambda _probe, restart: SegmentedWorkload(restart, clock),
        lifecycle_recorder=LifecycleRecorder(),
    )
    return Stage0MeasurementProbeAdapter(
        harness,
        TARGET,
        measurement_plan_factory=lambda _probe, _payload: {},
        known_signal_detector=lambda _run, _payload: (_ for _ in ()).throw(
            AssertionError("Formal verdicts belong to D")
        ),
        null_signal_detector=lambda _run, _payload: (_ for _ in ()).throw(
            AssertionError("Formal verdicts belong to D")
        ),
    )


def _payload(probe_type: str) -> dict[str, object]:
    return {
        "task_id": str(uuid4()),
        "stage0_run_id": str(uuid4()),
        "target_snapshot_id": str(uuid4()),
        "target_fingerprint": target_fingerprint(TARGET),
        "target": TARGET.model_dump(mode="json"),
        "workload_id": "stage0-kernel-v1",
        "adapter_profile": REAL_STAGE0_PROFILE,
        "probe_type": probe_type,
        "protocol_version": "s0-g0-v1",
        "mode": "formal",
        "_job_context": {
            "lease_id": str(uuid4()),
            "lease_scope": "exclusive",
            "resource_id": "hcu-7",
            "fencing_token": 9,
        },
    }


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_formal_noise_publishes_strict_v2_samples_and_lifecycle(tmp_path: Path) -> None:
    output = _adapter().run_probe(_payload("noise"), tmp_path)
    evidence_path = file_uri_to_path(output.raw_evidence_uri)
    evidence = MeasurementEvidenceV2.model_validate_json(evidence_path.read_bytes())

    assert output.raw_evidence_hash == _sha256(evidence_path)
    assert evidence.binding.probe_type.value == "noise"
    assert evidence.binding.workload_id == "stage0-kernel-v1"
    assert evidence.binding.lease.resource_id == "hcu-7"
    assert evidence.plan.restart_count == 10
    assert evidence.plan.segment_order == ("noise",)
    assert evidence.plan.samples_per_segment == 30
    assert len(evidence.samples) == 300
    assert len({sample.process_id for sample in evidence.samples}) == 10
    assert [item.phase for item in evidence.observations].count("before_restart") == 10
    assert [item.phase for item in evidence.observations].count("after_restart") == 10
    lifecycle = [
        item.process_lifecycle_record
        for item in evidence.observations
        if item.process_lifecycle_record is not None
    ]
    assert len(lifecycle) == 20
    assert all(_sha256(file_uri_to_path(item.uri)) == item.sha256 for item in lifecycle)
    lifecycle_records = [
        ProcessLifecycleRecordV2.model_validate_json(file_uri_to_path(item.uri).read_bytes())
        for item in lifecycle
    ]
    assert [record.event for record in lifecycle_records[:2]] == ["started", "reaped"]
    assert lifecycle_records[1].waitpid_result_pid == lifecycle_records[1].process_id
    recomputed = recompute_clock_calibration(
        [point.model_dump(mode="python") for point in evidence.calibration.points],
        resolution_tick_deltas=evidence.calibration.resolution_tick_deltas,
    )
    assert recomputed.timer_resolution_ns == evidence.calibration.timer_resolution_ns
    assert recomputed.ns_per_tick == evidence.calibration.ns_per_tick
    assert recomputed.max_residual_ns == evidence.calibration.max_residual_ns
    assert output.cleanup_evidence == {
        "fence": {"resource_id": "hcu-7", "fencing_token": 9, "fenced": True},
        "health": {"resource_id": "hcu-7", "healthy": True},
    }


def test_formal_signal_uses_registered_abba_plan_without_producer_verdict(
    tmp_path: Path,
) -> None:
    output = _adapter().run_probe(_payload("known_signal"), tmp_path)
    evidence = MeasurementEvidenceV2.model_validate_json(
        file_uri_to_path(output.raw_evidence_uri).read_bytes()
    )

    assert evidence.plan.segment_order == ("A1", "B1", "B2", "A2")
    assert evidence.plan.samples_per_segment == 10
    assert len(evidence.samples) == 400
    assert output.summary["verdict_owner"] == "stage0-d-verifier"
    assert "detected" not in output.summary


def test_formal_fingerprint_uses_bound_v2_envelope_and_typed_telemetry(
    tmp_path: Path,
) -> None:
    output = _adapter().run_probe(_payload("fingerprint"), tmp_path)
    evidence = FingerprintEvidenceV2.model_validate_json(
        file_uri_to_path(output.raw_evidence_uri).read_bytes()
    )

    hardware = {
        "execution_host": TARGET.execution_host.name,
        "address": TARGET.execution_host.address,
        "accelerator": TARGET.execution_host.accelerator.model_dump(),
        "host_environment": TARGET.execution_host.observed_host_environment.model_dump(),
    }
    assert evidence.hardware_fingerprint == stable_fingerprint(hardware)
    assert evidence.binding.protocol_version == "s0-g0-v1"
    assert evidence.observations[0].telemetry.device.device_index == 7
    assert evidence.observations[1].captured_monotonic_ns > (
        evidence.observations[0].captured_monotonic_ns
    )
    assert output.cleanup_evidence is not None


def test_every_formal_probe_gets_a_distinct_measurement_identity(tmp_path: Path) -> None:
    adapter = _adapter()
    measurement_ids: set[UUID] = set()
    for probe_type in ("fingerprint", "timer", "noise", "known_signal", "null_signal"):
        output = adapter.run_probe(_payload(probe_type), tmp_path)
        if probe_type == "fingerprint":
            evidence = FingerprintEvidenceV2.model_validate_json(
                file_uri_to_path(output.raw_evidence_uri).read_bytes()
            )
        else:
            evidence = MeasurementEvidenceV2.model_validate_json(
                file_uri_to_path(output.raw_evidence_uri).read_bytes()
            )
        measurement_ids.add(evidence.binding.measurement_id)

    assert len(measurement_ids) == 5
