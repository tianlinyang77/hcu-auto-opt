# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from uuid import uuid4

import pytest
from pydantic import ValidationError

from hcuopt.domain.enums import RoundCandidateState, RoundPhase
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.workers.formal_phase_consumer import FormalPhaseConsumer
from hcuopt.workers.formal_phase_prepare import (
    DEPLOYMENT_PHASE_FIELDS,
    prepare_formal_phase_request,
)
from tests.unit import test_m2_formal_execution as fixture
from tests.unit.test_formal_execution_checkpoint import setup
from tests.unit.test_formal_phase_consumer import MemoryJournal


def materials(kwargs):  # type: ignore[no-untyped-def]
    request = kwargs["request"]
    return dict(
        round_authority=kwargs["round_authority"],
        formal_authority=kwargs["formal_authority"],
        member=kwargs["member"],
        authorization_id=request.binding.formal_authorization_id,
        resolved_plan_hash=request.binding.resolved_plan_hash,
        reservation=request.reservation,
        deployment={name: getattr(request.binding, name) for name in DEPLOYMENT_PHASE_FIELDS},
        harness_payload=request.harness_payload,
    )


def test_prepare_reproduces_request_without_execution(tmp_path):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    result = prepare_formal_phase_request(adapter=adapter, **materials(kwargs))
    assert result == kwargs["request"]
    assert not harness.payloads
    assert not budget.reserves


@pytest.mark.parametrize("field", sorted(DEPLOYMENT_PHASE_FIELDS))
def test_missing_deployment_material_is_not_defaulted(tmp_path, field):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    inputs = materials(kwargs)
    del inputs["deployment"][field]
    with pytest.raises(Conflict, match="missing="):
        prepare_formal_phase_request(adapter=adapter, **inputs)
    assert not harness.payloads and not budget.reserves


@pytest.mark.parametrize("field", ["artifact_hash", "host_id", "window", "phase", "job_id"])
def test_deployment_cannot_override_authoritative_fields(tmp_path, field):  # type: ignore[no-untyped-def]
    adapter, _, _, kwargs = setup(tmp_path)
    inputs = materials(kwargs)
    inputs["deployment"][field] = getattr(kwargs["request"].binding, field)
    with pytest.raises(Conflict, match="unexpected="):
        prepare_formal_phase_request(adapter=adapter, **inputs)


@pytest.mark.parametrize("fault", ["unbuilt", "uncorrected", "wrong_round", "bad_budget"])
def test_invalid_materials_do_not_take_journal_slot(tmp_path, fault):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    inputs = materials(kwargs)
    if fault == "unbuilt":
        inputs["member"] = inputs["member"].model_copy(update={"artifact_id": None})
    elif fault == "uncorrected":
        inputs["member"] = inputs["member"].model_copy(
            update={"state": RoundCandidateState.INTAKE_ACCEPTED}
        )
    elif fault == "wrong_round":
        inputs["round_authority"] = inputs["round_authority"].model_copy(
            update={"round_id": uuid4()}
        )
    else:
        inputs["reservation"] = inputs["reservation"].model_copy(
            update={"candidate_id": uuid4()}
        )
    journal = MemoryJournal(adapter, kwargs["request"])
    consumer = FormalPhaseConsumer(journal, adapter, enabled=True)
    with pytest.raises((Conflict, MeasurementSafetyError, ValidationError)):
        consumer.prepare_and_execute_once(**inputs, output_dir=kwargs["output_dir"])
    assert journal.row is None
    assert not harness.payloads and not budget.reserves


def test_prepared_request_runs_existing_consumer_and_replays(tmp_path):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    journal = MemoryJournal(adapter, kwargs["request"])
    consumer = FormalPhaseConsumer(journal, adapter, enabled=True)
    first = consumer.prepare_and_execute_once(
        **materials(kwargs), output_dir=kwargs["output_dir"]
    )
    second = consumer.execute_once(**kwargs)
    assert first == second
    assert len(harness.payloads) == len(budget.reserves) == 1


def test_disabled_preparation_does_not_read_authority(tmp_path):  # type: ignore[no-untyped-def]
    adapter, _, _, kwargs = setup(tmp_path)
    def forbidden(_):  # type: ignore[no-untyped-def]
        pytest.fail("disabled consumer must not read deployment authority")
    adapter.authority_reader.load_authorization = forbidden
    consumer = FormalPhaseConsumer(MemoryJournal(adapter, kwargs["request"]), adapter)
    with pytest.raises(Conflict, match="disabled"):
        consumer.prepare_and_execute_once(**materials(kwargs), output_dir=kwargs["output_dir"])


def test_holdout_uses_revealed_plan_and_family(tmp_path):  # type: ignore[no-untyped-def]
    round_ = fixture._round(RoundPhase.HOLDOUT)
    authority = fixture._authority(round_)
    member = fixture._member(round_, RoundPhase.HOLDOUT)
    request = fixture._request(round_, authority, member, RoundPhase.HOLDOUT)
    adapter = fixture._adapter(tmp_path, fixture.Harness(), fixture.RecordingBudget())
    inputs = materials(dict(
        round_authority=round_, formal_authority=authority, member=member, request=request
    ))
    prepared = prepare_formal_phase_request(adapter=adapter, **inputs)
    assert prepared == request
    assert prepared.binding.holdout_reveal_evidence_hash is not None


def test_expired_authorization_rejected_before_journal(tmp_path):  # type: ignore[no-untyped-def]
    adapter, harness, budget, kwargs = setup(tmp_path)
    adapter.clock = lambda: kwargs["request"].binding.window.expires_at
    journal = MemoryJournal(adapter, kwargs["request"])
    with pytest.raises(MeasurementSafetyError, match="not active"):
        FormalPhaseConsumer(journal, adapter, enabled=True).prepare_and_execute_once(
            **materials(kwargs), output_dir=kwargs["output_dir"]
        )
    assert journal.row is None
    assert not harness.payloads and not budget.reserves
