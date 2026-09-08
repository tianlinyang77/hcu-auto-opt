# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real M1 producer to Formal result-consumer seam; no live execution authority."""

from pathlib import Path

import pytest

from hcuopt.domain.enums import RoundPhase
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.measurement.m2_formal_runner import M2FormalPhaseExecutionAdapter
from tests.unit.test_m1_measurement import _fixture
from tests.unit.test_m2_formal_execution import _authority, _member, _request, _round


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


def test_real_m1_result_enters_formal_reference_without_sampling_reimplementation(tmp_path: Path):
    harness, payload = _fixture(tmp_path)
    harness.cleaner = FormalFixtureCleaner()
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
