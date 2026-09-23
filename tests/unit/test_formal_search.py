# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from uuid import uuid4

import pytest
from test_m2_formal_finalizer import NOW, _fixture

from hcuopt.contracts.m2_formal_authority_v1 import formal_authority_context_ref
from hcuopt.domain.enums import LeaseScope, ManualCandidateVerdict, RoundPhase
from hcuopt.evaluation.formal_search import close_formal_search
from hcuopt.evaluation.m1_verifier import M1PerformanceVerificationResult
from hcuopt.measurement.m2_models import RoundMeasurementRef


def setup_case(effects=(0.2, 0.1)):
    store, context, authority, barrier, _ = _fixture()
    refs, results = [], {}
    for i, member in enumerate(barrier.members):

        def hashed(label, ordinal=i):
            return store.publish({"label": label, "candidate": ordinal}).sha256

        ref = RoundMeasurementRef(
            round_measurement_ref_id=member.round_measurement_ref_id,
            round_id=authority.round_id,
            round_candidate_id=member.round_candidate_id,
            candidate_id=member.candidate_id,
            phase=RoundPhase.SEARCH,
            candidate_family_hash=authority.candidate_family_hash,
            artifact_family_hash=authority.artifact_family_hash,
            artifact_id=member.artifact_id,
            artifact_hash=member.artifact_hash,
            measurement_id=uuid4(),
            raw_evidence_uri=f"file:///raw-{i}.json",
            raw_evidence_hash=hashed("raw"),
            measurement_plan_hash=hashed("plan"),
            phase_plan_hash=authority.search_plan_hash,
            baseline_sample_set_hash=hashed("baseline"),
            process_identity_set_hash=hashed("process"),
            cache_namespace_set_hash=hashed("cache"),
            lease_id=uuid4(),
            resource_id="test",
            fencing_token=1,
            created_at=NOW,
        )
        refs.append(ref)
        results[ref.candidate_id] = M1PerformanceVerificationResult(
            verdict=ManualCandidateVerdict.INCONCLUSIVE,
            input_digest=hashed("input"),
            measurement_id=ref.measurement_id,
            raw_evidence_uri=ref.raw_evidence_uri,
            raw_evidence_hash=ref.raw_evidence_hash,
            metric_name="kernel_elapsed",
            unit="ns",
            protocol_version="m1-kernel-performance-v1",
            sample_count=16,
            warmup_count=2,
            process_restart_count=4,
            plan_hash=ref.measurement_plan_hash,
            sample_budget_hash=hashed("budget"),
            adapter_profile="test",
            lease_id=ref.lease_id,
            lease_scope=LeaseScope.EXCLUSIVE,
            resource_id=ref.resource_id,
            fencing_token=ref.fencing_token,
            process_identities=("p1", "p2", "p3", "p4"),
            cache_namespaces=("c1", "c2", "c3", "c4"),
            environment_fingerprint=hashed("env"),
            cleanup_hash=member.cleanup_evidence_hash,
            credible_threshold=0.01,
            restart_effects=(effects[i],) * 4,
        )

    class Verifier:
        def verify(self, reference):
            return results[reference.candidate_id]

    class Repository:
        def __init__(self):
            self.records = []

        def record_formal_barrier(self, record):
            self.records.append(record)
            return {}

    repository = Repository()
    args = dict(
        round_authority=authority,
        context=formal_authority_context_ref(context),
        members=barrier.members,
        references=tuple(refs),
        verifier=Verifier(),
        publisher=store,
        repository=repository,
        closed_at=NOW,
        closed_by="test-d",
        idempotency_key="formal-search-test",
    )
    return args, results, repository


def test_collect_rank_publish_then_persist():
    args, _, repository = setup_case()
    record = close_formal_search(**args)
    assert record.barrier.promoted_candidate_ids == (args["members"][0].candidate_id,)
    assert repository.records == [record]
    assert args["publisher"].artifact_for_hash(record.barrier.input_summary_hash)


def test_zero_positive_candidates_closes_without_holdout():
    args, _, _ = setup_case((-0.1, 0))
    record = close_formal_search(**args)
    assert record.barrier.promoted_candidate_ids == ()
    assert record.holdout_family_hash is None


@pytest.mark.parametrize("fault", ["missing", "phase", "result", "cleanup"])
def test_drift_never_persists_barrier(fault):
    args, results, repository = setup_case()
    if fault == "missing":
        args["references"] = args["references"][:1]
    elif fault == "phase":
        args["references"] = (
            args["references"][0].model_copy(
                update={
                    "phase": RoundPhase.HOLDOUT,
                }
            ),
            args["references"][1],
        )
    else:
        key = args["members"][0].candidate_id
        updates = {"measurement_id": uuid4()} if fault == "result" else {"cleanup_hash": None}
        results[key] = results[key].model_copy(update=updates)
    with pytest.raises(ValueError):
        close_formal_search(**args)
    assert not repository.records


def test_invalid_d_result_stays_in_batch_as_failure():
    args, results, _ = setup_case()
    key = args["members"][0].candidate_id
    results[key] = results[key].model_copy(update={"verdict": ManualCandidateVerdict.INVALID})
    record = close_formal_search(**args)
    assert record.barrier.members[0].candidate_state.value == "invalid"
    assert record.barrier.members[0].failure_evidence_hash
    assert record.barrier.promoted_candidate_ids == (args["members"][1].candidate_id,)
