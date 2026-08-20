from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.measurement.evidence import verify_evidence
from hcuopt.measurement.harness import EvidenceMeasurementHarness
from hcuopt.measurement.stage0 import Stage0MeasurementProbeAdapter
from hcuopt.source_hash import file_uri_to_path
from hcuopt.targets import load_target

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")


class DeviceTimer:
    def read_ticks(self) -> int:
        return 1


class Telemetry:
    def collect(self):
        return {"fixture": True}


def test_fingerprint_probe_writes_independently_verifiable_evidence(tmp_path: Path) -> None:
    harness = EvidenceMeasurementHarness(
        provenance=AdapterProvenance(
            profile="real-stage0-scripted",
            capability="measurement_harness",
            adapter_name="ScriptedHarness",
            adapter_version="1",
            implementation_kind="real",
        ),
        stable_identity=TARGET.model_dump(mode="json"),
        workload_factory=lambda _restart: None,
        telemetry=Telemetry(),
        device_timer=DeviceTimer(),
    )
    adapter = Stage0MeasurementProbeAdapter(
        harness,
        TARGET,
        measurement_plan_factory=lambda _probe, _payload: {},
        known_signal_detector=lambda _run, _payload: True,
        null_signal_detector=lambda _run, _payload: False,
    )

    output = adapter.run_probe(
        {
            "stage0_run_id": str(uuid4()),
            "probe_type": "fingerprint",
            "protocol_version": "s0-measurement-v1",
            "mode": "dry_run",
        },
        tmp_path,
    )
    evidence_path = file_uri_to_path(output.raw_evidence_uri)

    assert output.synthetic is False
    assert output.summary["hardware_fingerprint"].startswith("sha256:")
    assert output.summary["software_fingerprint"].startswith("sha256:")
    assert verify_evidence(evidence_path, output.raw_evidence_hash) is True
