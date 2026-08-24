from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.platform_v1 import AdapterProvenance, ArtifactManifest, MeasurementSeries
from hcuopt.contracts.v1 import ManualPerformanceEvidenceResult
from hcuopt.domain.errors import AdapterUnavailable
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_verifier import Stage0EvidenceError, Stage0EvidenceReader
from hcuopt.measurement.evidence import write_evidence
from hcuopt.measurement.m1_harness import M1TrustedMeasurementHarness
from hcuopt.measurement.m1_models import (
    M1ActivationEvidence,
    M1MeasurementEvidence,
    M1MeasurementPlan,
    M1Stage0Authority,
    M1Stage0ReportReference,
)
from hcuopt.measurement.models import ProcessIdentity
from hcuopt.targets import load_target, target_fingerprint
from hcuopt.workers.handlers import JobHandlers

ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"


class PortableReader(Stage0EvidenceReader):
    def _secure_read(self, path: Path) -> bytes:
        encoded = path.read_bytes()
        if len(encoded) > self.max_bytes:
            raise Stage0EvidenceError("evidence_too_large", "fixture is too large")
        return encoded


def _path_from_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


class FixtureClock:
    def __init__(self) -> None:
        self.value = 10_000

    def now_ns(self) -> int:
        self.value += 100
        return self.value


class FixtureTimer:
    def __init__(self) -> None:
        self.value = 1_000

    def read_ticks(self) -> int:
        self.value += 100
        return self.value

    def sample_resolution_ticks(self, sample_count: int):
        return tuple(1 for _ in range(sample_count))

    def measure_resolution_ns(self, sample_count: int) -> float:
        return 1.0


class FixtureTelemetry:
    def collect(self):
        return {
            "device": {
                "device_index": 7,
                "temperature_c": 40.0,
                "sclk_mhz": 1800.0,
                "mclk_mhz": 1600.0,
                "performance_level": "manual",
                "power_w": 100.0,
            },
            "cache": {
                "state": "flushed",
                "cleared_before_sample": True,
                "identity_hash": "sha256:" + "9" * 64,
            },
            "background_processes": [],
        }


class FixtureCleaner:
    def fence(self, resource_id: str, fencing_token: int):
        return {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "fenced": True,
        }

    def health_check(self, resource_id: str):
        return {"resource_id": resource_id, "healthy": True}


class FixtureLifecycle:
    def record_started(self, workload, *, restart_ordinal: int, captured_monotonic_ns: int):
        identity = workload.process_identity()
        return {
            "event": "started",
            "restart_ordinal": restart_ordinal,
            "observer_process_id": 1,
            "process_id": identity.pid,
            "proc_stat_line": f"{identity.pid} (fixture) S 1 1 1 0",
            "captured_monotonic_ns": captured_monotonic_ns,
        }

    def record_reaped(self, workload, *, restart_ordinal: int, captured_monotonic_ns: int):
        identity = workload.process_identity()
        return {
            "event": "reaped",
            "restart_ordinal": restart_ordinal,
            "observer_process_id": 1,
            "process_id": identity.pid,
            "proc_stat_line": f"{identity.pid} (fixture) Z 1 1 1 0",
            "captured_monotonic_ns": captured_monotonic_ns,
            "waitpid_result_pid": identity.pid,
            "wait_status": 0,
        }


class FixtureWorkload:
    def __init__(
        self,
        arm: str,
        ordinal: int,
        clock: FixtureClock,
        image_digest: str,
        artifact_hash: str,
        candidate_ticks: int,
    ) -> None:
        self.arm = arm
        self.identity = ProcessIdentity(pid=20_000 + ordinal, start_token=f"start-{ordinal}")
        self.clock = clock
        self.image_digest = image_digest
        self.artifact_hash = artifact_hash
        self.candidate_ticks = candidate_ticks
        self.alive = True

    def process_identity(self):
        return self.identity

    def activation_evidence(self):
        candidate = self.arm == "candidate"
        return {
            "arm": self.arm,
            "activation_mode": "startup_overlay" if candidate else "baseline",
            "image_digest": self.image_digest,
            "loaded_artifact_hash": self.artifact_hash if candidate else None,
            "import_attestation": (
                {"uri": "file:///fixture/import.json", "sha256": "sha256:" + "8" * 64}
                if candidate
                else None
            ),
            "cache_namespace_hash": "sha256:" + f"{20_000 + int(self.identity.pid):064x}"[-64:],
        }

    def synchronize(self):
        return None

    def warmup(self):
        return None

    def measure_batch(self, iterations: int):
        started_host = self.clock.now_ns()
        finished_host = self.clock.now_ns()
        base = started_host * 10
        elapsed = self.candidate_ticks if self.arm == "candidate" else 100
        return {
            "process_id": self.identity.pid,
            "process_start_token": self.identity.start_token,
            "batch_iterations": iterations,
            "started_monotonic_ns": started_host,
            "finished_monotonic_ns": finished_host,
            "started_device_ticks": base,
            "finished_device_ticks": base + elapsed,
        }

    def close(self):
        self.alive = False

    def is_alive(self):
        return self.alive


def _authority(reference: M1Stage0ReportReference) -> M1Stage0Authority:
    return M1Stage0Authority(
        report=reference,
        metric_name="kernel_elapsed",
        unit="ns",
        timer_resolution_ns=1.0,
        noise_sigma_ns=2.0,
        noise_cv=0.01,
        mde_ratio=0.03,
        alpha=0.05,
        power=0.8,
        bootstrap_resamples=10_000,
        bootstrap_method="percentile",
        bootstrap_seed_source="input_evidence_sha256",
    )


def _fixture(tmp_path: Path, candidate_ticks: int = 100):
    target = load_target(TARGET_PATH)
    protocol = load_registered_stage0_protocol("s0-g0-v2")
    stage0_run_id = uuid4()
    target_snapshot_id = uuid4()
    input_digest = "sha256:" + "4" * 64
    report_file = write_evidence(
        tmp_path / "reports" / "stage0.json",
        {
            "schema_version": "stage0-formal-report-v1",
            "task_id": str(uuid4()),
            "stage0_run_id": str(stage0_run_id),
            "target_snapshot_id": str(target_snapshot_id),
            "target_id": target.target_id,
            "workload_id": "m1-fixture-workload",
            "verification": {
                "measurement": "pass",
                "protocol_version": protocol.protocol.protocol_version,
                "protocol_hash": protocol.protocol_hash,
                "input_digest": input_digest,
                "timer_resolution_ns": 1.0,
                "noise_sigma_ns": 2.0,
                "noise_cv": 0.01,
                "mde_ratio": 0.03,
            },
        },
    )
    artifact = ArtifactManifest(
        candidate_id=uuid4(),
        kind="python_overlay",
        uri="file:///candidate.py",
        content_hash="sha256:" + "6" * 64,
        source_snapshot_id=uuid4(),
    )
    payload = {
        "task_id": str(uuid4()),
        "candidate_id": str(artifact.candidate_id),
        "round_id": str(uuid4()),
        "baseline_epoch_id": str(uuid4()),
        "stage0_run_id": str(stage0_run_id),
        "target_snapshot_id": str(target_snapshot_id),
        "target_fingerprint": target_fingerprint(target),
        "target": target.model_dump(mode="json"),
        "workload_id": "m1-fixture-workload",
        "workload_hash": "sha256:" + "1" * 64,
        "configuration_hash": "sha256:" + "2" * 64,
        "baseline_source": {"source_hash": "sha256:" + "3" * 64},
        "candidate_source": {"source_hash": "sha256:" + "5" * 64},
        "artifact": artifact.model_dump(mode="json"),
        "stage0_report": {
            "uri": report_file.uri,
            "sha256": report_file.sha256,
            "input_digest": input_digest,
            "protocol_version": protocol.protocol.protocol_version,
            "protocol_hash": protocol.protocol_hash,
        },
        "budget": {"max_samples": 8},
        "_job_context": {
            "lease_id": str(uuid4()),
            "lease_scope": "exclusive",
            "resource_id": "hcu-7",
            "fencing_token": 7,
        },
    }
    clock = FixtureClock()

    def factory(arm, ordinal, _payload, _output_dir):
        return FixtureWorkload(
            arm,
            ordinal,
            clock,
            target.inference_image.registry_digest,
            artifact.content_hash,
            candidate_ticks,
        )

    def plan_factory(_payload, authority):
        return M1MeasurementPlan(
            warmup_count=1,
            samples_per_acquisition=2,
            batch_iterations=10,
            stage0_authority=authority,
        )

    harness = M1TrustedMeasurementHarness(
        provenance=AdapterProvenance(
            profile="m1-real-fixture",
            capability="measurement_harness",
            adapter_name="M1TrustedMeasurementHarness",
            adapter_version="1",
            implementation_kind="real",
        ),
        evidence_root=tmp_path,
        evidence_reader=PortableReader(tmp_path),
        workload_factory=factory,
        telemetry=FixtureTelemetry(),
        device_timer=FixtureTimer(),
        lifecycle_recorder=FixtureLifecycle(),
        cleaner=FixtureCleaner(),
        clock=clock,
        plan_factory=plan_factory,
    )
    return harness, payload


@pytest.mark.parametrize("candidate_ticks", [100, 120])
def test_m1_harness_preserves_noop_and_known_signal_as_raw_evidence(
    tmp_path: Path, candidate_ticks: int
) -> None:
    harness, payload = _fixture(tmp_path, candidate_ticks)
    result = harness.run_manual_performance(payload, tmp_path)

    assert result.measurement.status == "measured"
    assert result.measurement.sample_count == 8
    assert result.measurement.summary["verdict_owner"] == "candidate_adjudicator"
    assert "verdict" not in result.measurement.summary
    evidence_path = _path_from_uri(result.measurement.raw_samples_uri)
    evidence = M1MeasurementEvidence.model_validate_json(evidence_path.read_bytes())
    assert [item.arm for item in evidence.acquisitions] == [
        "baseline",
        "candidate",
        "candidate",
        "baseline",
    ]
    assert len({item.process_id for item in evidence.acquisitions}) == 4
    candidate_deltas = [
        sample.finished_device_ticks - sample.started_device_ticks
        for item in evidence.acquisitions
        if item.arm == "candidate"
        for sample in item.samples
    ]
    assert set(candidate_deltas) == {candidate_ticks}


def test_m1_plan_and_activation_fail_closed() -> None:
    reference = M1Stage0ReportReference(
        uri="file:///stage0.json",
        sha256="sha256:" + "1" * 64,
        input_digest="sha256:" + "2" * 64,
        protocol_version="s0-g0-v2",
        protocol_hash="sha256:" + "3" * 64,
    )
    with pytest.raises(ValidationError, match="ABBA"):
        M1MeasurementPlan(
            acquisition_order=("baseline", "candidate"),
            warmup_count=1,
            samples_per_acquisition=2,
            batch_iterations=10,
            stage0_authority=_authority(reference),
        )
    with pytest.raises(ValidationError, match="import attestation"):
        M1ActivationEvidence(
            arm="candidate",
            activation_mode="startup_overlay",
            image_digest="sha256:" + "4" * 64,
            loaded_artifact_hash="sha256:" + "5" * 64,
            cache_namespace_hash="sha256:" + "6" * 64,
        )


def test_m1_harness_rehashes_formal_stage0_report(tmp_path: Path) -> None:
    harness, payload = _fixture(tmp_path)
    report_path = _path_from_uri(payload["stage0_report"]["uri"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["verification"]["mde_ratio"] = 0.99
    report_path.chmod(0o600)
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(Exception, match="hash|SHA256"):
        harness.run_manual_performance(payload, tmp_path)


def test_manual_performance_worker_uses_only_trusted_interface() -> None:
    provenance = AdapterProvenance(
        profile="m1-handler-fixture",
        capability="measurement_harness",
        adapter_name="RecordingM1Harness",
        adapter_version="1",
        implementation_kind="real",
    )
    candidate_id = uuid4()

    class RecordingHarness:
        def __init__(self) -> None:
            self.provenance = provenance
            self.payloads = []

        def run_manual_performance(self, payload, output_dir):
            self.payloads.append((payload, output_dir))
            return ManualPerformanceEvidenceResult(
                candidate_id=candidate_id,
                measurement=MeasurementSeries(
                    status="measured",
                    metric_name="kernel_elapsed",
                    unit="ns",
                    protocol_version="m1-kernel-performance-v1",
                    sample_count=8,
                    raw_samples_uri="file:///m1/raw.json",
                    raw_samples_hash="sha256:" + "7" * 64,
                    environment_fingerprint="sha256:" + "8" * 64,
                    adapter_provenance=provenance,
                ),
                cleanup_evidence={
                    "fence": {"fenced": True},
                    "health": {"healthy": True},
                },
            )

    harness = RecordingHarness()
    handlers = JobHandlers(
        AdapterRegistry(profile=provenance.profile, measurement_harness=harness)
    )
    result = handlers.handle_manual_performance({"candidate_id": str(candidate_id)})
    assert result["candidate_id"] == str(candidate_id)
    assert len(harness.payloads) == 1

    class LegacyHarness:
        def __init__(self) -> None:
            self.provenance = provenance

        def run(self, payload, output_dir):
            raise AssertionError("legacy timing path must not run for M1")

    legacy = JobHandlers(
        AdapterRegistry(profile=provenance.profile, measurement_harness=LegacyHarness())
    )
    with pytest.raises(AdapterUnavailable, match="trusted paired"):
        legacy.handle_manual_performance({"candidate_id": str(candidate_id)})
