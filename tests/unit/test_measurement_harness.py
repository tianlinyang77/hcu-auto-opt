from __future__ import annotations

from pathlib import Path

import pytest

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.measurement.harness import EvidenceMeasurementHarness, MeasurementSafetyError


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


class Workload:
    def synchronize(self) -> None:
        return None

    def warmup(self) -> None:
        return None

    def run_batch(self, iterations: int) -> None:
        assert iterations == 2


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
        workload_factory=lambda _restart: Workload(),
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
            "environment_fingerprint": "sha256:" + "a" * 64,
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
