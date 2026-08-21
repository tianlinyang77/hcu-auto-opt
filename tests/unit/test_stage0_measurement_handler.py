from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.adapters.profiles import (
    REAL_STAGE0_MEASUREMENT_PROFILE,
    AdapterProfileCatalog,
)
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.measurement.stage0 import Stage0MeasurementProbeAdapter, Stage0ProbeOutput
from hcuopt.targets import load_target, target_fingerprint
from hcuopt.workers.handlers import JobHandlers

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")


class ScriptedStage0Probe:
    target_fingerprint = target_fingerprint(TARGET)
    provenance = AdapterProvenance(
        profile=REAL_STAGE0_MEASUREMENT_PROFILE,
        capability="stage0_probe",
        adapter_name="ScriptedStage0Probe",
        adapter_version="1",
        implementation_kind="real",
    )

    def run_probe(self, payload, output_dir):
        assert output_dir == Path(".")
        assert payload["probe_type"] == "timer"
        return Stage0ProbeOutput(
            summary={"timer_resolution_ns": 2.0},
            raw_evidence_uri="file:///stage0/timer.json",
            raw_evidence_hash="sha256:" + "1" * 64,
            cleanup_evidence=None,
        )


class BoundHarness:
    provenance = AdapterProvenance(
        profile=REAL_STAGE0_MEASUREMENT_PROFILE,
        capability="measurement_harness",
        adapter_name="BoundHarness",
        adapter_version="1",
        implementation_kind="real",
    )
    environment_fingerprint = stable_fingerprint(TARGET.model_dump(mode="json"))

    def run_with_evidence(self, plan, output_dir):
        raise AssertionError("formal restart validation must run before the harness")


def _timer_payload(*, fingerprint: str | None = None) -> dict[str, object]:
    return {
        "stage0_run_id": str(uuid4()),
        "target_snapshot_id": str(uuid4()),
        "target_fingerprint": fingerprint or target_fingerprint(TARGET),
        "target": TARGET.model_dump(mode="json"),
        "probe_type": "timer",
        "protocol_version": "s0-measurement-v1",
        "mode": "dry_run",
    }


def test_real_stage0_profile_is_registered_separately_from_f1() -> None:
    profile = AdapterProfileCatalog().require(REAL_STAGE0_MEASUREMENT_PROFILE)

    assert profile.implementation_kind == "real"
    profile.require_stage0()


def test_stage0_handler_binds_adapter_output_to_existing_public_contract() -> None:
    handlers = JobHandlers(
        AdapterRegistry(
            profile=REAL_STAGE0_MEASUREMENT_PROFILE,
            stage0_probe=ScriptedStage0Probe(),
        )
    )
    result = handlers.handle_stage0_probe(_timer_payload())

    assert result["probe_type"] == "timer"
    assert result["summary"] == {"timer_resolution_ns": 2.0}
    assert result["raw_evidence_hash"] == "sha256:" + "1" * 64
    assert result["synthetic"] is False


def test_stage0_handler_rejects_a_target_fingerprint_mismatch() -> None:
    handlers = JobHandlers(
        AdapterRegistry(
            profile=REAL_STAGE0_MEASUREMENT_PROFILE,
            stage0_probe=ScriptedStage0Probe(),
        )
    )

    with pytest.raises(ExecutionSafetyError, match="does not match TargetSpec"):
        handlers.handle_stage0_probe(_timer_payload(fingerprint="sha256:" + "2" * 64))


def test_stage0_handler_rejects_an_adapter_bound_to_another_target() -> None:
    probe = ScriptedStage0Probe()
    probe.target_fingerprint = "sha256:" + "3" * 64
    handlers = JobHandlers(
        AdapterRegistry(
            profile=REAL_STAGE0_MEASUREMENT_PROFILE,
            stage0_probe=probe,
        )
    )

    with pytest.raises(ExecutionSafetyError, match="bound to a different target"):
        handlers.handle_stage0_probe(_timer_payload())


def test_formal_measurement_fails_closed_on_an_incomplete_control_plane_binding() -> None:
    adapter = Stage0MeasurementProbeAdapter(
        BoundHarness(),  # type: ignore[arg-type]
        TARGET,
        measurement_plan_factory=lambda _probe, _payload: {
            "measurement_plan": {
                "protocol_version": "s0-measurement-v1",
                "metric_name": "kernel_elapsed",
                "unit": "ns",
                "warmup_count": 1,
                "repeat_count": 2,
                "process_restart_count": 0,
                "batched_loop_count": 1,
                "environment_fingerprint": BoundHarness.environment_fingerprint,
            }
        },
        known_signal_detector=lambda _run, _payload: True,
        null_signal_detector=lambda _run, _payload: False,
    )

    with pytest.raises(MeasurementSafetyError, match="control-plane binding is incomplete"):
        adapter.run_probe(
            {
                "stage0_run_id": str(uuid4()),
                "probe_type": "noise",
                "protocol_version": "s0-g0-v1",
                "mode": "formal",
            },
            Path("."),
        )
