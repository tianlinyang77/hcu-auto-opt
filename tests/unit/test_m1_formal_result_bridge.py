# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real M1 producer to Formal result-consumer seam; no live execution authority."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hcuopt.domain.enums import RoundPhase
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.measurement.m1_failure import M1FailureReport, M1MeasurementFailure
from hcuopt.measurement.m1_models import M1Stage0ReportReference, m1_plan_hash
from hcuopt.measurement.m1_stage0 import load_m1_stage0_authority
from hcuopt.measurement.m2_formal_runner import M2FormalPhaseExecutionAdapter
from tests.unit.test_m1_measurement import _fixture
from tests.unit.test_m2_formal_execution import _authority, _member, _request, _round


def test_m1_stage0_import_is_independent_of_test_collection_order():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", "from hcuopt.measurement.m1_stage0 import load_m1_stage0_authority"],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


class FormalFixtureCleaner:
    def fence(self, resource_id, fencing_token):
        return {
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "fenced": True,
            "failures": [],
            "clocks_restored": True,
        }

    def health_check(self, resource_id):
        return {
            "resource_id": resource_id,
            "healthy": True,
            "residual_owned_containers": [],
            "clocks_restored": True,
            "memory_release_check": "no_adapter_owned_container_remains",
        }


def _freeze_m1_plan(harness, payload):
    reference = M1Stage0ReportReference.model_validate_json(json.dumps(payload["stage0_report"]))
    authority = load_m1_stage0_authority(harness.reader, reference, task_payload=payload)
    plan = harness.plan_factory(payload, authority)
    payload.update(
        mode="formal",
        measurement_plan_hash=m1_plan_hash(plan),
        expected_sample_count=plan.expected_sample_count,
    )


def test_real_m1_result_enters_formal_reference_without_sampling_reimplementation(tmp_path: Path):
    harness, payload = _fixture(tmp_path)
    harness.cleaner = FormalFixtureCleaner()
    _freeze_m1_plan(harness, payload)
    result = harness.run_manual_performance(payload, tmp_path)
    # This tests only the result boundary, not A authorization or B's run lifecycle.
    round_ = _round(RoundPhase.SEARCH)
    authority = _authority(round_)
    member = _member(round_, RoundPhase.SEARCH)
    request = _request(round_, authority, member, RoundPhase.SEARCH)
    round_ = round_.model_copy(update={"adapter_profile": harness.provenance.profile})
    member = member.model_copy(update={"candidate_id": result.candidate_id})
    request = request.model_copy(
        update={
            "binding": request.binding.model_copy(
                update={
                    "measurement_plan_hash": result.measurement.summary["plan_hash"],
                    "fencing_token": payload["_job_context"]["fencing_token"],
                }
            ),
            "reservation": request.reservation.model_copy(
                update={
                    "expected_sample_count": result.measurement.sample_count,
                }
            ),
        }
    )
    adapter = M2FormalPhaseExecutionAdapter
    adapter._validate_harness_result(round_, member, request, result)
    reference = adapter._measurement_ref(round_, member, request, result)
    assert reference.raw_evidence_hash == result.measurement.raw_samples_hash
    assert reference.measurement_id == result.measurement.measurement_id
    for name in (
        "baseline_sample_set_hash",
        "process_identity_set_hash",
        "cache_namespace_set_hash",
    ):
        assert getattr(reference, name) == result.measurement.summary[name]
        # Old producer output is rejected; the guard is not relaxed for compatibility.
        summary = {key: value for key, value in result.measurement.summary.items() if key != name}
        missing = result.model_copy(
            update={
                "measurement": result.measurement.model_copy(update={"summary": summary}),
            }
        )
        with pytest.raises(MeasurementSafetyError, match="mismatched"):
            adapter._validate_harness_result(round_, member, request, missing)


@pytest.mark.parametrize(
    "field,value",
    [
        ("measurement_plan_hash", "sha256:" + "f" * 64),
        ("expected_sample_count", 1),
        ("expected_sample_count", 400.0),
    ],
)
def test_m1_rejects_frozen_plan_drift_before_device_access(tmp_path, field, value):
    harness, payload = _fixture(tmp_path)
    _freeze_m1_plan(harness, payload)
    payload[field] = value
    called = []
    harness.workload_factory = lambda *_: called.append(True)
    with pytest.raises(MeasurementSafetyError, match="frozen measurement plan"):
        harness.run_manual_performance(payload, tmp_path)
    assert called == []
    assert harness.device_timer.value == 1000


def test_m1_consumes_lost_lease_hook_before_device_calibration(tmp_path):
    harness, payload = _fixture(tmp_path)
    _freeze_m1_plan(harness, payload)

    class LostLease:
        def is_set(self):
            return True

    payload["_job_context"]["lease_lost_event"] = LostLease()
    called = []
    harness.workload_factory = lambda *_: called.append(True)
    with pytest.raises(MeasurementSafetyError, match="lease was lost"):
        harness.run_manual_performance(payload, tmp_path)
    assert called == []
    assert harness.device_timer.value == 1000


@pytest.mark.parametrize("publication_fails", [False, True])
def test_m1_failure_preserves_partial_acquisition_and_attempted_usage(
    tmp_path, monkeypatch, publication_fails
):
    harness, payload = _fixture(tmp_path)
    harness.cleaner = FormalFixtureCleaner()
    original_factory = harness.workload_factory
    calls = []

    def factory(*args):
        workload = original_factory(*args)
        measure = workload.measure_batch
        def partial(iterations):
            calls.append(True)
            if len(calls) == 4:
                raise TimeoutError("fixture fourth invocation failed")
            return measure(iterations)
        workload.measure_batch = partial
        return workload

    harness.workload_factory = factory
    if publication_fails:
        import hcuopt.measurement.m1_harness as module
        original_write = module.write_evidence
        def write(path, value):
            if path.name == "failure.json":
                raise OSError("fixture evidence disk unavailable")
            return original_write(path, value)
        monkeypatch.setattr(module, "write_evidence", write)

    with pytest.raises(M1MeasurementFailure) as captured:
        harness.run_manual_performance(payload, tmp_path)
    assert len(calls) == 4
    if publication_fails:
        assert captured.value.evidence is None
    else:
        ref = captured.value.evidence
        report = M1FailureReport.model_validate_json(harness.reader.read_bytes(ref.uri, ref.sha256))
        assert report.attempted_sample_count == 4
        assert len(report.verified_samples) == 3
        assert report.completed_acquisitions == ()
        assert report.cleanup_evidence["health"]["healthy"] is True
        assert report.error_type == "TimeoutError"
