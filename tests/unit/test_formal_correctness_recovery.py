# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from hcuopt.storage.formal_correctness_recovery import PostgresFormalCorrectnessRecovery


@pytest.mark.parametrize("case,expected", [
    ("empty", "not_invoked"), ("invoking", "invocation_unresolved"),
    ("unknown", "unknown_requires_manual_recovery"),
    ("recorded", "result_ready"), ("released", "settlement_pending"),
    ("completed", "completed"), ("lost_owner", "ownership_or_budget_conflict"),
    ("missing_budget", "inconsistent"), ("broken_completed", "inconsistent"),
])
def test_inspection_is_read_only_and_does_not_expose_credentials(case, expected):
    job_id, lease_id = uuid4(), uuid4()
    resource = dict(state="active", owner_job_id=job_id, lease_id=lease_id, fencing_token=7)
    budget = dict(reservation_id=uuid4(), state="reserved")
    job = dict(state="running", resource_id="fixture-resource", result=None)
    events = {}
    if case != "empty":
        events["formal_correctness_invoking"] = {}
    if case == "unknown":
        events["formal_correctness_unknown"] = {}
    if case in {"recorded", "released", "completed", "lost_owner", "broken_completed"}:
        events["formal_correctness_result"] = {"details": {"private_evidence": "not-exported"}}
    if case in {"released", "completed", "broken_completed"}:
        events["formal_correctness_released"] = {}
        # A non-empty database event row is truthy.
        events["formal_correctness_released"]["details"] = {"lease_held_seconds": 1}
        resource["owner_job_id"] = None
        resource["state"] = "available"
    if case in {"completed", "broken_completed"}:
        job.update(state="succeeded", result=events["formal_correctness_result"]["details"])
        budget["state"] = "settled" if case == "completed" else "reserved"
    if case == "lost_owner":
        resource["fencing_token"] = 8
    conn = Mock()
    conn.execute.side_effect = [
        Mock(fetchone=Mock(return_value=resource)),
        Mock(fetchone=Mock(return_value=None if case == "missing_budget" else budget)),
    ]
    repo = SimpleNamespace(connection=lambda: nullcontext(conn))
    journal = SimpleNamespace(
        job_id=job_id, owner=dict(lease_id=lease_id, fencing_token=7, token="secret"),
        _locked=Mock(return_value=(job, events)),
        lease=SimpleNamespace(jobs=SimpleNamespace(
            claims=SimpleNamespace(dispatcher=SimpleNamespace(repository=repo)),
        )),
    )
    report = PostgresFormalCorrectnessRecovery(journal).inspect("sha256:" + "a" * 64)
    assert report["status"] == expected
    assert report["reconciliation_allowed"] == (
        expected in {"result_ready", "settlement_pending", "completed"}
    )
    assert report["execution_retry_allowed"] is False
    assert "secret" not in str(report) and "not-exported" not in str(report)
    assert all(call.args[0].startswith("SELECT") for call in conn.execute.call_args_list)
