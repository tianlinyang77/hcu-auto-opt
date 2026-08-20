from __future__ import annotations

from pathlib import Path

import pytest

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.harness import EvidenceMeasurementHarness, MeasurementSafetyError
from hcuopt.measurement.models import ProcessIdentity


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def now_ns(self) -> int:
        self.value += 10
        return self.value


class Timer:
    def __init__(self) -> None:
        self.value = 0

    def read_ticks(self) -> int:
        self.value += 5
        return self.value

    def measure_resolution_ns(self, sample_count: int) -> float:
        assert sample_count >= 2
        return 50.0


class Workload:
    def __init__(self, restart: int) -> None:
        self.identity = ProcessIdentity(pid=100 + restart, start_token=f"fixture-{restart}")
        self.alive = True

    def process_identity(self) -> ProcessIdentity:
        return self.identity

    def synchronize(self) -> None:
        return None

    def warmup(self) -> None:
        return None

    def run_batch(self, iterations: int) -> None:
        assert iterations == 2

    def close(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class Telemetry:
    def collect(self):
        return {"temperature_c": 30.0, "background_processes": []}


class Cleaner:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    def fence(self, resource_id: str, fencing_token: int):
        return {"resource_id": resource_id, "fencing_token": fencing_token, "fenced": True}

    def health_check(self, resource_id: str):
        return {"resource_id": resource_id, "healthy": self.healthy}


def _harness(cleaner: Cleaner | None = None) -> EvidenceMeasurementHarness:
    return EvidenceMeasurementHarness(
        provenance=AdapterProvenance(
            profile="real-stage0-test",
            capability="measurement_harness",
            adapter_name="EvidenceMeasurementHarness",
            adapter_version="1",
            implementation_kind="real",
        ),
        stable_identity={"target": "fixture", "image": "locked"},
        workload_factory=Workload,
        telemetry=Telemetry(),
        device_timer=Timer(),
        clock=Clock(),
        cleaner=cleaner,
    )


def _plan(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "measurement_plan": {
            "protocol_version": "s0-measurement-v1",
            "metric_name": "kernel_elapsed",
            "unit": "ns",
            "warmup_count": 1,
            "repeat_count": 2,
            "process_restart_count": 0,
            "batched_loop_count": 2,
            "environment_fingerprint": stable_fingerprint(
                {"target": "fixture", "image": "locked"}
            ),
        }
    }
    values.update(changes)
    return values


def test_harness_writes_hashed_raw_evidence(tmp_path: Path) -> None:
    result = _harness().run_with_evidence(_plan(), tmp_path)

    assert result.series.status == "measured"
    assert result.series.sample_count == 2
    assert result.series.raw_samples_uri == result.artifact.uri
    assert result.series.raw_samples_hash == result.artifact.sha256
    assert len(result.evidence.raw_samples) == 2
    assert result.evidence.calibration is not None
    assert result.evidence.calibration.timer_resolution_ns == 50.0


def test_harness_rejects_an_unbound_environment_fingerprint(tmp_path: Path) -> None:
    plan = _plan()
    plan["measurement_plan"]["environment_fingerprint"] = "sha256:" + "a" * 64

    with pytest.raises(MeasurementSafetyError, match="environment fingerprint"):
        _harness().run_with_evidence(plan, tmp_path)


def test_formal_harness_requires_lease_and_healthy_cleanup(tmp_path: Path) -> None:
    with pytest.raises(MeasurementSafetyError, match="exclusive lease"):
        _harness(Cleaner()).run_with_evidence(_plan(mode="formal"), tmp_path)

    with pytest.raises(MeasurementSafetyError, match="cleanup is unhealthy"):
        _harness(Cleaner(healthy=False)).run_with_evidence(
            _plan(
                mode="formal",
                _job_context={
                    "lease_id": "lease",
                    "lease_scope": "exclusive",
                    "resource_id": "hcu-7",
                    "fencing_token": 1,
                },
            ),
            tmp_path,
        )
