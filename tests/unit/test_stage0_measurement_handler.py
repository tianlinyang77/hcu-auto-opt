from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from hcuopt.adapters.profiles import (
    REAL_STAGE0_MEASUREMENT_PROFILE,
    AdapterProfileCatalog,
)
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.measurement.stage0 import Stage0ProbeOutput
from hcuopt.targets import load_target
from hcuopt.workers.handlers import JobHandlers

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")


class ScriptedStage0Probe:
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
    result = handlers.handle_stage0_probe(
        {
            "stage0_run_id": str(uuid4()),
            "target_snapshot_id": str(uuid4()),
            "target_fingerprint": "sha256:" + "2" * 64,
            "target": TARGET.model_dump(mode="json"),
            "probe_type": "timer",
            "protocol_version": "s0-measurement-v1",
            "mode": "dry_run",
        }
    )

    assert result["probe_type"] == "timer"
    assert result["summary"] == {"timer_resolution_ns": 2.0}
    assert result["raw_evidence_hash"] == "sha256:" + "1" * 64
    assert result["synthetic"] is False
