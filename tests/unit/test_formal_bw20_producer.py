# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""CPU boundary tests only: no Docker, HCU or performance acceptance."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hcuopt.deployment.bw20_m1_correctness_worker import (
    BW20FormalCorrectnessEvidenceProducer,
    BW20M1AllocatorCorrectnessEvidenceProducer,
)
from hcuopt.deployment.bw20_m1_policy import BW20_M1_POLICY
from hcuopt.domain.errors import ExecutionSafetyError


@pytest.mark.parametrize("failure", [None, "missing_callback", "expired", "busy", "expired_after"])
def test_formal_preflight_precedes_producer(monkeypatch, tmp_path, failure):
    events = []

    def live():
        events.append("lease")
        if failure == "expired" or (failure == "expired_after" and len(events) == 3):
            raise ExecutionSafetyError("expired fixture")

    def guard(resource):
        assert resource == BW20_M1_POLICY.resource_id
        events.append("guard")
        if failure == "busy":
            raise ExecutionSafetyError("busy fixture")

    delegate = Mock(return_value="fixture-evidence")
    monkeypatch.setattr(BW20M1AllocatorCorrectnessEvidenceProducer,
                        "produce_manual_correctness_evidence", delegate)
    producer = object.__new__(BW20FormalCorrectnessEvidenceProducer)
    producer.cleaner = SimpleNamespace(guard=guard)
    payload = {"_job_context": {
        "lease_scope": "shared", "resource_id": BW20_M1_POLICY.resource_id,
        "fencing_token": 1, "lease_id": "fixture-lease",
        "assert_live_lease": None if failure == "missing_callback" else live,
    }}
    if failure:
        with pytest.raises(ExecutionSafetyError):
            producer.produce_manual_correctness_evidence(payload, None, None, tmp_path)
        delegate.assert_not_called()
    else:
        assert producer.produce_manual_correctness_evidence(
            payload, None, None, tmp_path,
        ) == "fixture-evidence"
        assert events == ["lease", "guard", "lease"]
        delegate.assert_called_once_with(payload, None, None, tmp_path)
