# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.domain.errors import Conflict
from hcuopt.workers.formal_phase_consumer import FormalPhaseConsumer
from tests.unit.test_formal_execution_checkpoint import setup


class MemoryJournal:
    """Only a consumer fixture; concurrency is covered by PostgreSQL tests."""

    def __init__(self, adapter, request):  # type: ignore[no-untyped-def]
        self.intent_id, self.claim_token, self.worker_id = uuid4(), uuid4(), "worker"
        self.receipt_store = adapter.receipt_store
        self.row = None
        self.checks = []
        binding = request.binding
        intent = SimpleNamespace(
            task_id=binding.task_id,
            round_id=binding.round_id,
            resolved_plan_hash=binding.resolved_plan_hash,
            formal_authorization_hash=binding.formal_authorization_hash,
            candidate_bindings=[
                SimpleNamespace(
                    candidate_id=binding.candidate_id, round_candidate_id=binding.round_candidate_id
                )
            ],
        )
        self.claims = SimpleNamespace(
            dispatcher=SimpleNamespace(
                repository=SimpleNamespace(get_formal_start_intent=lambda _: intent)
            ),
            assert_active=lambda *args: self.checks.append(args),
        )

    def begin(self, request):  # type: ignore[no-untyped-def]
        if self.row is not None:
            return self.row, False
        self.row = {"state": "invoking"}
        return self.row, True

    def record_receipt(self, request, reference):  # type: ignore[no-untyped-def]
        self.receipt_store.load_for_request(reference, request)
        self.row = {"state": "receipt_recorded", "receipt_ref": reference.model_dump(mode="json")}

    def mark_unknown(self, request):  # type: ignore[no-untyped-def]
        if self.row["state"] == "invoking":
            self.row = {"state": "recovery_required"}


@pytest.mark.parametrize("harness_fails", [False, True])
def test_terminal_replay_calls_existing_adapter_only_once(tmp_path, harness_fails):  # type: ignore[no-untyped-def]
    adapter, harness, _, kwargs = setup(tmp_path)
    if harness_fails:
        harness.error = RuntimeError("fixture harness failed")
    journal = MemoryJournal(adapter, kwargs["request"])
    first = FormalPhaseConsumer(journal, adapter, enabled=True).execute_once(**kwargs)
    second = FormalPhaseConsumer(journal, adapter, enabled=True).execute_once(**kwargs)
    assert first == second
    assert len(harness.payloads) == 1
    assert len(journal.checks) == (2 if harness_fails else 3)
    assert (first.execution.status == "succeeded") is not harness_fails


def test_disabled_consumer_does_not_even_journal(tmp_path):  # type: ignore[no-untyped-def]
    adapter, harness, _, kwargs = setup(tmp_path)
    journal = MemoryJournal(adapter, kwargs["request"])
    with pytest.raises(Conflict, match="disabled"):
        FormalPhaseConsumer(journal, adapter).execute_once(**kwargs)
    assert journal.row is None
    assert not harness.payloads


@pytest.mark.parametrize("where", ["adapter", "receipt_write"])
def test_uncertain_outcome_never_calls_adapter_again(tmp_path, where):  # type: ignore[no-untyped-def]
    adapter, harness, _, kwargs = setup(tmp_path)
    journal = MemoryJournal(adapter, kwargs["request"])
    calls = []

    def crash(*args, **kw):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise RuntimeError("injected crash")

    if where == "adapter":
        adapter.run = crash
    else:
        journal.record_receipt = crash
    with pytest.raises(RuntimeError, match="injected"):
        FormalPhaseConsumer(journal, adapter, enabled=True).execute_once(**kwargs)
    with pytest.raises(Conflict, match="no retry"):
        FormalPhaseConsumer(journal, adapter, enabled=True).execute_once(**kwargs)
    assert len(calls) == 1
    assert journal.row["state"] == "recovery_required"
    assert len(harness.payloads) == (1 if where == "receipt_write" else 0)
