# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from uuid import uuid4

import pytest

from hcuopt.domain.enums import (
    ManualCandidateKind,
    RoundCandidateState,
    RoundPhase,
    SearchRoundState,
)
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.formal_correctness import verify_formal_correctness
from hcuopt.evaluation.m1_verifier import M1CorrectnessVerifier
from hcuopt.orchestrator.search_round import artifact_family_hash
from tests.unit import test_m1_d_verifier as evidence
from tests.unit import test_m2_formal_execution as formal


def inputs(tmp_path, delta=0.0):
    suite = evidence._Suite(tmp_path / "evidence", candidate_delta=delta)
    return from_suite(suite)


def from_suite(suite):
    context = suite.context
    round_ = formal._round(RoundPhase.SEARCH)
    fields = ("task_id", "baseline_epoch_id", "target_snapshot_id", "stage0_run_id",
              "stage0_protocol_hash", "workload_id", "workload_hash")
    round_ = round_.model_copy(update={
        **{f: getattr(context, f) for f in fields}, "state": SearchRoundState.CORRECTNESS,
    })
    member = formal._member(round_, RoundPhase.SEARCH).model_copy(update={
        **{f: getattr(context, f) for f in (
            "candidate_id", "baseline_source_hash", "candidate_source_hash",
            "artifact_id", "artifact_hash",
        )}, "state": RoundCandidateState.BUILT, "candidate_kind": ManualCandidateKind.BUSINESS,
    })
    second = member.model_copy(update={
        "candidate_id": uuid4(), "round_candidate_id": uuid4(), "ordinal": 1,
    })
    members = [member, second]
    round_ = round_.model_copy(update={"artifact_family_hash": artifact_family_hash(
        round_.model_dump(mode="json"), [m.model_dump(mode="json") for m in members],
    )})
    return dict(round_authority=round_, members=members, context=context,
                hotspot=suite.hotspot, reference=suite.reference,
                verifier=M1CorrectnessVerifier(
                    suite.protocol, evidence._PortableReader(suite.root)))


@pytest.mark.parametrize("delta,expected", [(0.0, "correct"), (0.25, "incorrect")])
def test_existing_verifier_reads_raw_outputs(tmp_path, delta, expected):
    assert verify_formal_correctness(**inputs(tmp_path, delta)).verdict == expected


def test_tampered_raw_evidence_is_invalid(tmp_path):
    kwargs = inputs(tmp_path)
    kwargs["reference"] = kwargs["reference"].model_copy(update={"sha256": evidence.SHA_A})
    assert verify_formal_correctness(**kwargs).verdict == "invalid"


@pytest.mark.parametrize("fault", ["artifact", "family", "missing", "round", "stage"])
def test_wrong_bindings_rejected_before_evidence_read(tmp_path, fault):
    kwargs = inputs(tmp_path)
    if fault == "artifact":
        kwargs["context"] = kwargs["context"].model_copy(update={"artifact_id": uuid4()})
    elif fault == "missing":
        kwargs["members"] = kwargs["members"][:1]
    else:
        field, value = {"family": ("artifact_family_hash", evidence.SHA_A),
                        "round": ("task_id", uuid4()),
                        "stage": ("state", SearchRoundState.BUILDING)}[fault]
        kwargs["round_authority"] = kwargs["round_authority"].model_copy(update={field: value})
    with pytest.raises(Conflict):
        verify_formal_correctness(**kwargs)
