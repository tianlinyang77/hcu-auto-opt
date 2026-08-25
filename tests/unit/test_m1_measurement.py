# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import json
import os
from copy import deepcopy
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
    M1DeviceEventRecord,
    M1MeasurementEvidence,
    M1MeasurementPlan,
    M1SampleBudget,
    M1Stage0Authority,
    M1Stage0ReportReference,
    m1_sample_budget_hash,
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


def _proc_stat(process_id: int, start_token: str, *, state: str = "S") -> str:
    start_ticks = start_token.rsplit(":", 1)[-1]
    return f"{process_id} (fixture) {state} " + " ".join(["1"] * 18 + [start_ticks])


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
            "proc_stat_line": _proc_stat(identity.pid, identity.start_token),
            "captured_monotonic_ns": captured_monotonic_ns,
        }

    def record_reaped(self, workload, *, restart_ordinal: int, captured_monotonic_ns: int):
        identity = workload.process_identity()
        return {
            "event": "reaped",
            "restart_ordinal": restart_ordinal,
            "observer_process_id": 1,
            "process_id": identity.pid,
            "proc_stat_line": _proc_stat(identity.pid, identity.start_token, state="Z"),
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
        output_dir: Path,
        provenance: AdapterProvenance,
    ) -> None:
        self.arm = arm
        self.ordinal = ordinal
        self.identity = ProcessIdentity(
            pid=20_000 + ordinal,
            start_token=f"linux-proc-startticks:{30_000 + ordinal}",
        )
        self.clock = clock
        self.image_digest = image_digest
        self.artifact_hash = artifact_hash
        self.candidate_ticks = candidate_ticks
        self.output_dir = output_dir
        self.provenance = provenance
        self.sample_ordinal = 0
        self.alive = True

    def process_identity(self):
        return self.identity

    def activation_evidence(self):
        candidate = self.arm == "candidate"
        namespace_hash = "sha256:" + f"{40_000 + self.ordinal:064x}"[-64:]
        cache_file = write_evidence(
            self.output_dir / "child" / f"a{self.ordinal}-cache.json",
            {
                "schema_version": "m1-performance-cache-v1",
                "acquisition_ordinal": self.ordinal,
                "arm": self.arm,
                "process_id": self.identity.pid,
                "process_start_token": self.identity.start_token,
                "namespace_hash": namespace_hash,
                "empty_before_execution": True,
            },
        )
        import_file = None
        if candidate:
            import_file = write_evidence(
                self.output_dir / "child" / f"a{self.ordinal}-import.json",
                {
                    "schema_version": "m1-overlay-import-v1",
                    "acquisition_ordinal": self.ordinal,
                    "process_id": self.identity.pid,
                    "process_start_token": self.identity.start_token,
                    "image_digest": self.image_digest,
                    "artifact_content_hash": self.artifact_hash,
                    "activation_mode": "startup_overlay",
                },
            )
        return {
            "arm": self.arm,
            "activation_mode": "startup_overlay" if candidate else "baseline",
            "image_digest": self.image_digest,
            "loaded_artifact_hash": self.artifact_hash if candidate else None,
            "import_attestation": (
                {"uri": import_file.uri, "sha256": import_file.sha256}
                if candidate
                else None
            ),
            "cache_namespace_hash": namespace_hash,
            "cache_namespace_evidence": {
                "uri": cache_file.uri,
                "sha256": cache_file.sha256,
            },
            "cache_empty_before_execution": True,
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
        record = M1DeviceEventRecord(
            process_id=self.identity.pid,
            process_start_token=self.identity.start_token,
            batch_iterations=iterations,
            started_monotonic_ns=started_host,
            finished_monotonic_ns=finished_host,
            started_device_ticks=base,
            finished_device_ticks=base + elapsed,
            arm=self.arm,
            acquisition_ordinal=self.ordinal,
            sample_ordinal=self.sample_ordinal,
            timer_provenance=self.provenance.model_dump(mode="python"),
        )
        event_file = write_evidence(
            self.output_dir
            / "child"
            / f"a{self.ordinal}-s{self.sample_ordinal}-event.json",
            record,
        )
        self.sample_ordinal += 1
        return {"uri": event_file.uri, "sha256": event_file.sha256}

    def close(self):
        self.alive = False

    def is_alive(self):
        return self.alive


def _authority(reference: M1Stage0ReportReference) -> M1Stage0Authority:
    sample_budget = M1SampleBudget(
        acquisition_order=("baseline", "candidate", "candidate", "baseline"),
        warmup_count=1,
        samples_per_acquisition=2,
        batch_iterations=10,
    )
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
        sample_budget=sample_budget,
        sample_budget_hash=m1_sample_budget_hash(sample_budget),
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
    provenance = AdapterProvenance(
        profile="m1-real-fixture",
        capability="measurement_harness",
        adapter_name="M1TrustedMeasurementHarness",
        adapter_version="1",
        implementation_kind="real",
    )
    expected_samples = (
        protocol.protocol.sampling.restart_count
        * len(protocol.protocol.sampling.signal_segment_order)
        * protocol.protocol.sampling.signal_samples_per_segment
    )
    payload = {
        "task_id": str(uuid4()),
        "candidate_id": str(artifact.candidate_id),
        "adapter_profile": provenance.profile,
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
        "budget": {"max_samples": expected_samples, "max_wall_seconds": 60},
        "_job_context": {
            "lease_id": str(uuid4()),
            "lease_scope": "exclusive",
            "resource_id": "hcu-7",
            "fencing_token": 7,
        },
    }
    clock = FixtureClock()

    def factory(arm, ordinal, _payload, output_dir):
        return FixtureWorkload(
            arm,
            ordinal,
            clock,
            target.inference_image.registry_digest,
            artifact.content_hash,
            candidate_ticks,
            output_dir,
            provenance,
        )

    harness = M1TrustedMeasurementHarness(
        provenance=provenance,
        evidence_root=tmp_path,
        evidence_reader=PortableReader(tmp_path),
        workload_factory=factory,
        telemetry=FixtureTelemetry(),
        device_timer=FixtureTimer(),
        lifecycle_recorder=FixtureLifecycle(),
        cleaner=FixtureCleaner(),
        clock=clock,
    )
    return harness, payload


@pytest.mark.parametrize("candidate_ticks", [100, 120])
def test_m1_harness_preserves_noop_and_known_signal_as_raw_evidence(
    tmp_path: Path, candidate_ticks: int
) -> None:
    harness, payload = _fixture(tmp_path, candidate_ticks)
    result = harness.run_manual_performance(payload, tmp_path)

    assert result.measurement.status == "measured"
    assert result.measurement.sample_count == 400
    assert result.measurement.summary["verdict_owner"] == "candidate_adjudicator"
    assert "verdict" not in result.measurement.summary
    evidence_path = _path_from_uri(result.measurement.raw_samples_uri)
    evidence = M1MeasurementEvidence.model_validate_json(evidence_path.read_bytes())
    assert evidence.schema_version == "m1-kernel-performance-evidence-v1"
    assert evidence.plan.sample_budget == evidence.plan.stage0_authority.sample_budget
    assert evidence.binding.adapter_profile == "m1-real-fixture"
    assert [item.arm for item in evidence.acquisitions[:4]] == [
        "baseline",
        "candidate",
        "candidate",
        "baseline",
    ]
    assert len({item.process_id for item in evidence.acquisitions}) == 40
    assert len({item.activation.cache_namespace_hash for item in evidence.acquisitions}) == 40
    assert evidence.cleanup_evidence.fence["resource_id"] == "hcu-7"
    assert evidence.cleanup_evidence.fence["fencing_token"] == 7
    candidate_deltas = [
        sample.finished_device_ticks - sample.started_device_ticks
        for item in evidence.acquisitions
        if item.arm == "candidate"
        for sample in item.samples
    ]
    assert set(candidate_deltas) == {candidate_ticks}


def test_m1_evidence_contract_rejects_cache_event_and_cleanup_rebinding(
    tmp_path: Path,
) -> None:
    harness, payload = _fixture(tmp_path)
    result = harness.run_manual_performance(payload, tmp_path)
    evidence_path = _path_from_uri(result.measurement.raw_samples_uri)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    reused_cache = deepcopy(evidence)
    reused_cache["acquisitions"][1]["activation"]["cache_namespace_hash"] = reused_cache[
        "acquisitions"
    ][0]["activation"]["cache_namespace_hash"]
    with pytest.raises(ValidationError, match="fresh cache namespace"):
        M1MeasurementEvidence.model_validate_json(json.dumps(reused_cache))

    reused_event = deepcopy(evidence)
    reused_event["acquisitions"][1]["samples"][0]["device_event_record"] = reused_event[
        "acquisitions"
    ][0]["samples"][0]["device_event_record"]
    with pytest.raises(ValidationError, match="raw device Event"):
        M1MeasurementEvidence.model_validate_json(json.dumps(reused_event))

    detached_cleanup = deepcopy(evidence)
    detached_cleanup["cleanup_evidence"]["fence"].update(
        {"resource_id": "hcu-999", "fencing_token": 999}
    )
    detached_cleanup["cleanup_evidence"]["health"]["resource_id"] = "hcu-999"
    with pytest.raises(ValidationError, match="another lease resource"):
        M1MeasurementEvidence.model_validate_json(json.dumps(detached_cleanup))


def test_m1_harness_rejects_nonzero_process_exit_and_hashes_cleanup_in_failure(
    tmp_path: Path,
) -> None:
    harness, payload = _fixture(tmp_path)
    lifecycle = harness.lifecycle_recorder

    class NonzeroExitLifecycle:
        def record_started(self, *args, **kwargs):
            return lifecycle.record_started(*args, **kwargs)

        def record_reaped(self, *args, **kwargs):
            record = dict(lifecycle.record_reaped(*args, **kwargs))
            record["wait_status"] = 9
            return record

    harness.lifecycle_recorder = NonzeroExitLifecycle()
    with pytest.raises(Exception, match="did not exit successfully"):
        harness.run_manual_performance(payload, tmp_path)

    failure_files = list((tmp_path / "m1").glob("*/failure.json"))
    assert len(failure_files) == 1
    failure = json.loads(failure_files[0].read_text(encoding="utf-8"))
    assert failure["cleanup_evidence"]["fence"]["resource_id"] == "hcu-7"
    assert failure["cleanup_evidence"]["fence"]["fencing_token"] == 7
    assert failure["cleanup_evidence"]["health"]["healthy"] is True


def test_m1_harness_rejects_detached_stage0_and_task_budgets(tmp_path: Path) -> None:
    harness, payload = _fixture(tmp_path)

    def smaller_plan(_payload, authority):
        return M1MeasurementPlan(
            acquisition_order=("baseline", "candidate", "candidate", "baseline"),
            warmup_count=1,
            samples_per_acquisition=2,
            batch_iterations=10,
            stage0_authority=authority,
        )

    harness.plan_factory = smaller_plan
    with pytest.raises(ValidationError, match="sampling budget"):
        harness.run_manual_performance(payload, tmp_path)

    harness, payload = _fixture(tmp_path / "wall-budget")
    payload["budget"]["max_wall_seconds"] = 0
    with pytest.raises(Exception, match="positive integer"):
        harness.run_manual_performance(payload, tmp_path / "wall-budget")

    harness, payload = _fixture(tmp_path / "expired-wall-budget")

    class ExpiringClock:
        def __init__(self) -> None:
            self.value = 0

        def now_ns(self) -> int:
            self.value += 400_000_000
            return self.value

    harness.clock = ExpiringClock()
    payload["budget"]["max_wall_seconds"] = 1
    with pytest.raises(Exception, match="exceeded max_wall_seconds"):
        harness.run_manual_performance(payload, tmp_path / "expired-wall-budget")


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
            cache_namespace_evidence={
                "uri": "file:///cache.json",
                "sha256": "sha256:" + "7" * 64,
            },
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
