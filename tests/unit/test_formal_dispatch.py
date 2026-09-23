# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from datetime import timedelta
from uuid import uuid4

import pytest

from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentRequest
from hcuopt.domain.errors import Conflict
from hcuopt.operator.formal_dispatch import prepare_formal_round
from tests.unit.test_formal_operator_plans import NOW
from tests.unit.test_formal_start_management_api import setup_management


def ready(tmp_path):  # type: ignore[no-untyped-def]
    management, repository, payload = setup_management(tmp_path)
    coordinator = management.coordinator
    request = FormalStartIntentRequest(
        **payload, actor_assertion=management.capabilities[0].assertion
    )
    result = coordinator.create(request, repository)
    intent = repository.get_formal_start_intent(result.intent_id)
    return coordinator, repository, intent


def test_round_materialization_is_deterministic_and_nonexecuting(tmp_path):  # type: ignore[no-untyped-def]
    coordinator, repository, intent = ready(tmp_path)
    before = repository.get_formal_start_intent(intent.intent_id)
    first = prepare_formal_round(coordinator, intent, repository)
    second = prepare_formal_round(coordinator, intent, repository)
    assert first == second
    assert repository.get_formal_start_intent(intent.intent_id) == before
    assert first.round.run_mode.value == "formal"
    assert len(first.members) == 2
    assert all(member.candidate_kind.value == "business" for member in first.members)
    assert first.round.round_id == intent.round_id
    assert first.round.task_id == intent.task_id
    assert first.round.search_plan_hash == coordinator.object_store.evaluation.search_plan_hash
    assert first.round.candidate_family_hash != first.preview.resolved_plan.source_family_hash
    assert first.round.artifact_family_hash is None
    assert first.round.holdout_plan_hash is None
    assert first.round.automatic_release_allowed is False
    assert not first.intent.hcu_accessed
    assert not first.intent.round_creation_allowed


@pytest.mark.parametrize("field", ["round_candidate_id", "candidate_input_digest"])
def test_rejects_tampered_member_binding(tmp_path, field):  # type: ignore[no-untyped-def]
    coordinator, repository, intent = ready(tmp_path)
    binding = intent.candidate_bindings[0].model_copy(
        update={field: uuid4() if field.endswith("id") else "sha256:" + "a" * 64}
    )
    intent = intent.model_copy(
        update={"candidate_bindings": (binding, intent.candidate_bindings[1])}
    )
    with pytest.raises(Conflict, match="binding"):
        prepare_formal_round(coordinator, intent, repository)


def test_rechecks_signature_instead_of_trusting_ready(tmp_path):  # type: ignore[no-untyped-def]
    coordinator, repository, intent = ready(tmp_path)
    coordinator.execution_verifier.accepted = False
    with pytest.raises(Conflict):
        prepare_formal_round(coordinator, intent, repository)


def test_rejects_expired_preview(tmp_path):  # type: ignore[no-untyped-def]
    coordinator, repository, intent = ready(tmp_path)
    coordinator.clock = lambda: NOW + timedelta(hours=1)
    with pytest.raises(Conflict):
        prepare_formal_round(coordinator, intent, repository)


def test_rejects_cancelled_intent(tmp_path):  # type: ignore[no-untyped-def]
    coordinator, repository, intent = ready(tmp_path)
    cancelled = repository.cancel_formal_start_intent(intent.intent_id, cancelled_at=NOW)
    with pytest.raises(Conflict):
        prepare_formal_round(coordinator, cancelled, repository)
