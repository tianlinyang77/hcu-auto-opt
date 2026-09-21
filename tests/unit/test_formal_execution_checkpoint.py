# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.domain.enums import RoundPhase
from hcuopt.domain.errors import Conflict, SourceArtifactError
from hcuopt.measurement.m2_formal_runner import M2FormalExecutionFailure
from hcuopt.workers.formal_checkpoint import FormalClaimCheckpoint
from tests.unit import test_m2_formal_execution as fixture


def setup(tmp_path):  # type: ignore[no-untyped-def]
    round_ = fixture._round(RoundPhase.SEARCH)
    authority = fixture._authority(round_)
    member = fixture._member(round_, RoundPhase.SEARCH)
    request = fixture._request(round_, authority, member, RoundPhase.SEARCH)
    harness = fixture.Harness(fixture._result(tmp_path, request))
    budget = fixture.RecordingBudget()
    adapter = fixture._adapter(tmp_path, harness, budget)
    kwargs = dict(
        round_authority=round_,
        formal_authority=authority,
        member=member,
        request=request,
        output_dir=tmp_path / "execution",
    )
    return adapter, harness, budget, kwargs


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_stop_at_each_boundary_never_publishes_success(tmp_path, fail_at):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    calls = []

    def checkpoint(request):  # type: ignore[no-untyped-def]
        calls.append(request)
        if len(calls) == fail_at:
            raise Conflict("Formal stop requested")

    exception = Conflict if fail_at == 1 else M2FormalExecutionFailure
    with pytest.raises(exception) as error:
        adapter.run(**kwargs, execution_checkpoint=checkpoint)
    assert len(calls) == fail_at
    assert len(harness.payloads) == (1 if fail_at == 3 else 0)
    assert len(budget.reserves) == (0 if fail_at == 1 else 1)
    assert len(budget.finalizes) == (0 if fail_at == 1 else 1)
    if fail_at != 1:
        receipt = adapter.receipt_store.load_for_request(error.value.receipt_ref, kwargs["request"])
        assert receipt.execution.status != "succeeded"
        assert receipt.execution.measurement_ref is None
        assert receipt.execution.cleanup_status == "verified"
        assert receipt.execution.sample_count == (8 if fail_at == 3 else 0)
        if fail_at == 2:
            assert receipt.execution.harness_active_seconds == 0


def test_cleanup_failure_is_not_relabelled_as_safe_stop(tmp_path):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    adapter.recover_resource = lambda binding: {"fence": {"fenced": False}, "health": {}}
    calls = []

    def checkpoint(request):  # type: ignore[no-untyped-def]
        calls.append(request)
        if len(calls) == 2:
            raise Conflict("stop")

    with pytest.raises(M2FormalExecutionFailure) as error:
        adapter.run(**kwargs, execution_checkpoint=checkpoint)
    receipt = adapter.receipt_store.load_for_request(error.value.receipt_ref, kwargs["request"])
    assert receipt.execution.status == "cleanup_failed"
    assert receipt.execution.cleanup_status == "failed"
    assert not harness.payloads
    assert len(budget.finalizes) == 1


def test_success_checks_three_times_and_receipt_binds_entire_request(tmp_path):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    calls = []
    result = adapter.run(**kwargs, execution_checkpoint=calls.append)
    assert len(calls) == 3
    request = kwargs["request"]
    assert adapter.receipt_store.load_for_request(result.receipt_ref, request).execution.status == (
        "succeeded"
    )
    for field, value in {
        "lease_id": uuid4(),
        "fencing_token": 999,
        "job_id": uuid4(),
        "attempt": 2,
        "round_candidate_id": uuid4(),
    }.items():
        changed = request.model_copy(
            update={"binding": request.binding.model_copy(update={field: value})}
        )
        with pytest.raises(SourceArtifactError, match="expected request"):
            adapter.receipt_store.load_for_request(result.receipt_ref, changed)


@pytest.mark.parametrize(
    "change",
    [
        None,
        "task_id",
        "round_id",
        "resolved_plan_hash",
        "formal_authorization_hash",
        "candidate_id",
    ],
)
def test_claim_checkpoint_binds_phase_before_checking_liveness(tmp_path, change):  # type: ignore[no-untyped-def]
    _, _, _, kwargs = setup(tmp_path)
    request = kwargs["request"]
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
    calls = []
    claims = SimpleNamespace(
        dispatcher=SimpleNamespace(
            repository=SimpleNamespace(get_formal_start_intent=lambda _: intent)
        ),
        assert_active=lambda *args: calls.append(args),
    )
    checkpoint = FormalClaimCheckpoint(claims, uuid4(), "worker", uuid4())
    if change:
        value = "sha256:" + "f" * 64 if change.endswith("hash") else uuid4()
        changed = request.model_copy(update={"binding": binding.model_copy(update={change: value})})
        with pytest.raises(Conflict, match="claimed Intent"):
            checkpoint(changed)
        assert not calls
    else:
        checkpoint(request)
        assert calls == [(checkpoint.intent_id, "worker", checkpoint.claim_token)]
