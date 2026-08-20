from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hcuopt.adapters.profiles import REAL_STAGE0_PROFILE
from hcuopt.adapters.real_profile import compose_nmz36_stage0_registry
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.stage0_router import RoutedStage0ProbeAdapter
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.stage0 import Stage0ProbeOutput
from hcuopt.targets import load_target, target_fingerprint
from hcuopt.workers.handlers import JobHandlers

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")
TARGET_FINGERPRINT = target_fingerprint(TARGET)


class RecordingProbe:
    def __init__(self, profile: str, name: str) -> None:
        self.calls: list[Stage0ProbeType] = []
        self.target_fingerprint = TARGET_FINGERPRINT
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="stage0_probe",
            adapter_name=name,
            adapter_version="1",
            implementation_kind="real",
        )

    def run_probe(self, payload: dict[str, Any], output_dir: Path) -> Stage0ProbeOutput:
        del output_dir
        probe_type = Stage0ProbeType(payload["probe_type"])
        self.calls.append(probe_type)
        return Stage0ProbeOutput(
            summary={"handled_by": self.provenance.adapter_name},
            raw_evidence_uri="file:///evidence.json",
            raw_evidence_hash="sha256:" + "0" * 64,
            cleanup_evidence=None,
            adapter_provenance=(self.provenance,),
        )


class RecordingCleaner:
    provenance = AdapterProvenance(
        profile=REAL_STAGE0_PROFILE,
        capability="resource_cleaner",
        adapter_name="RecordingCleaner",
        adapter_version="1",
        implementation_kind="real",
    )

    def fence(self, resource_id: str, fencing_token: int) -> dict[str, Any]:
        return {"resource_id": resource_id, "fencing_token": fencing_token}

    def health_check(self, resource_id: str) -> dict[str, Any]:
        return {"resource_id": resource_id, "healthy": True}


def _routes(
    measurement: RecordingProbe, runtime: RecordingProbe
) -> dict[Stage0ProbeType, RecordingProbe]:
    return {
        probe_type: runtime
        if probe_type in {Stage0ProbeType.PROFILER, Stage0ProbeType.HOTPATCH}
        else measurement
        for probe_type in Stage0ProbeType
    }


def test_router_dispatches_b_and_c_probes_under_one_public_profile(tmp_path: Path) -> None:
    measurement = RecordingProbe("nmz36-stage0-measurement-v1", "MeasurementProbe")
    runtime = RecordingProbe(REAL_STAGE0_PROFILE, "RuntimeProbe")
    router = RoutedStage0ProbeAdapter(
        _routes(measurement, runtime), profile=REAL_STAGE0_PROFILE
    )

    for probe_type in Stage0ProbeType:
        result = router.run_probe({"probe_type": probe_type.value}, tmp_path)
        expected = "RuntimeProbe" if probe_type in runtime.calls else "MeasurementProbe"
        assert result.summary["handled_by"] == expected
        assert result.summary["probe_adapter_provenance"]["adapter_name"] == expected
        assert result.adapter_provenance[0].adapter_name == expected

    assert set(measurement.calls) == {
        Stage0ProbeType.FINGERPRINT,
        Stage0ProbeType.TIMER,
        Stage0ProbeType.NOISE,
        Stage0ProbeType.KNOWN_SIGNAL,
        Stage0ProbeType.NULL_SIGNAL,
    }
    assert set(runtime.calls) == {Stage0ProbeType.PROFILER, Stage0ProbeType.HOTPATCH}
    assert router.provenance.profile == REAL_STAGE0_PROFILE


def test_handler_records_delegate_provenance_in_the_public_result(tmp_path: Path) -> None:
    measurement = RecordingProbe("nmz36-stage0-measurement-v1", "MeasurementProbe")
    runtime = RecordingProbe(REAL_STAGE0_PROFILE, "RuntimeProbe")
    router = RoutedStage0ProbeAdapter(
        _routes(measurement, runtime), profile=REAL_STAGE0_PROFILE
    )
    handlers = JobHandlers(
        AdapterRegistry(profile=REAL_STAGE0_PROFILE, stage0_probe=router),
        output_dir=tmp_path,
    )

    result = handlers.handle_stage0_probe(
        {
            "stage0_run_id": str(uuid4()),
            "target_snapshot_id": str(uuid4()),
            "target_fingerprint": TARGET_FINGERPRINT,
            "target": TARGET.model_dump(mode="json"),
            "probe_type": Stage0ProbeType.PROFILER.value,
            "protocol_version": "fixture-v1",
            "mode": "dry_run",
        }
    )

    assert result["adapter_provenance"][0]["adapter_name"] == "RuntimeProbe"


def test_router_rejects_incomplete_public_contract() -> None:
    measurement = RecordingProbe("nmz36-stage0-measurement-v1", "MeasurementProbe")
    routes = _routes(measurement, measurement)
    routes.pop(Stage0ProbeType.HOTPATCH)

    with pytest.raises(ValueError, match=r"missing=\['hotpatch'\]"):
        RoutedStage0ProbeAdapter(routes, profile=REAL_STAGE0_PROFILE)


def test_composed_registry_exposes_one_profile_and_preserves_delegates(
    tmp_path: Path,
) -> None:
    del tmp_path
    measurement = RecordingProbe("nmz36-stage0-measurement-v1", "MeasurementProbe")
    runtime = RecordingProbe(REAL_STAGE0_PROFILE, "RuntimeProbe")
    measurement_registry = AdapterRegistry(
        profile="nmz36-stage0-measurement-v1", stage0_probe=measurement
    )
    runtime_registry = AdapterRegistry(
        profile=REAL_STAGE0_PROFILE,
        stage0_probe=runtime,
        resource_cleaner=RecordingCleaner(),
    )

    registry = compose_nmz36_stage0_registry(measurement_registry, runtime_registry)

    assert registry.profile == REAL_STAGE0_PROFILE
    assert registry.require("stage0_probe").provenance.profile == REAL_STAGE0_PROFILE
