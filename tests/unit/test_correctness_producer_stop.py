# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from unittest.mock import Mock

import pytest

from hcuopt.deployment import nmz36_m1_allocator as module
from tests.unit.test_m1_d_verifier import _Suite
from tests.unit.test_m1_worker_adapters import _base_payload


@pytest.mark.parametrize("after_reference", [False, True])
@pytest.mark.parametrize("cleanup_failure", [None, "fence", "health_check"])
def test_producer_stop_keeps_failure_and_runs_scoped_cleanup(
    tmp_path, monkeypatch, after_reference, cleanup_failure,
):
    suite = _Suite(tmp_path / "fixture")
    payload = _base_payload(suite)
    guard = Mock(return_value=None)
    payload["_job_context"]["assert_live_lease"] = guard
    monkeypatch.setattr(module, "_job_context", lambda *args: payload["_job_context"])
    monkeypatch.setattr(module, "_reference_file", lambda: suite.root / "reference.py")
    producer = object.__new__(module.Nmz36M1AllocatorCorrectnessEvidenceProducer)
    producer.deployment_policy = None
    producer.target = suite.target
    producer.baseline_module_hash = suite.baseline_snapshot.source_hash
    producer.cleaner = Mock()
    producer.cleaner.fence.return_value = {"fenced": True}
    producer.cleaner.health_check.return_value = {"healthy": True}
    if cleanup_failure:
        getattr(producer.cleaner, cleanup_failure).side_effect = RuntimeError("cleanup failed")
    stop = RuntimeError("formal stopped")
    producer._run_variant = Mock(side_effect=[None, stop] if after_reference else stop)
    with pytest.raises(RuntimeError, match="formal stopped"):
        producer.produce_manual_correctness_evidence(
            payload, suite.context, suite.hotspot, tmp_path / "output",
        )
    assert producer._run_variant.call_count == (2 if after_reference else 1)
    for call in producer._run_variant.call_args_list:
        assert call.kwargs["assert_live"] is guard
    producer.cleaner.fence.assert_called_once_with(
        suite.context.resource_id, suite.context.fencing_token,
    )
    producer.cleaner.health_check.assert_called_once_with(suite.context.resource_id)
    failure = json.loads((tmp_path / "output" / "m1-correctness" /
                          suite.context.candidate_id.hex / "failure.json").read_text())
    assert failure["error_type"] == "RuntimeError"
    assert failure["cleanup"]["fence"]["fenced"] is (cleanup_failure != "fence")
    assert failure["cleanup"]["health"]["healthy"] is (cleanup_failure != "health_check")
