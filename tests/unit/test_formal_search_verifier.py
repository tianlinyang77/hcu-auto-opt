# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from dataclasses import replace
from uuid import uuid4

import pytest

from hcuopt.domain.enums import RoundCandidateState, RoundPhase, SearchRoundState
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.formal_search_verifier import (
    FormalSearchVerificationMaterials,
    FormalSearchVerifier,
)
from hcuopt.evaluation.m1_verifier import M1CorrectnessVerifier, M1PerformanceVerifier
from hcuopt.measurement.m2_models import RoundMeasurementRef
from tests.unit import test_m1_d_verifier as evidence
from tests.unit.test_formal_correctness import from_suite


def setup(tmp_path):
    suite = evidence._Suite(tmp_path / "raw")
    raw, expected = evidence._performance(suite, suite.verify(), [0.2] * 4)
    inputs = from_suite(suite)
    context = suite.performance_context
    authority = inputs["round_authority"].model_copy(
        update={
            "round_id": context.round_id,
            "state": SearchRoundState.SEARCH_MEASURING,
            "configuration_hash": context.configuration_hash,
            "image_digest": context.image_digest,
            "adapter_profile": context.adapter_profile,
        }
    )
    member = inputs["members"][0].model_copy(
        update={
            "round_id": authority.round_id,
            "state": RoundCandidateState.CORRECTNESS_PASSED,
        }
    )
    material = FormalSearchVerificationMaterials(
        authority,
        member,
        suite.context,
        suite.reference,
        suite.hotspot,
        context,
    )
    reference = RoundMeasurementRef(
        round_measurement_ref_id=uuid4(),
        round_id=authority.round_id,
        round_candidate_id=member.round_candidate_id,
        candidate_id=member.candidate_id,
        phase=RoundPhase.SEARCH,
        candidate_family_hash=authority.candidate_family_hash,
        artifact_family_hash=authority.artifact_family_hash,
        artifact_id=member.artifact_id,
        artifact_hash=member.artifact_hash,
        measurement_id=raw.measurement_id,
        raw_evidence_uri=raw.uri,
        raw_evidence_hash=raw.sha256,
        measurement_plan_hash=expected.plan_hash,
        phase_plan_hash=authority.search_plan_hash,
        baseline_sample_set_hash=evidence.SHA_A,
        process_identity_set_hash=evidence.SHA_A,
        cache_namespace_set_hash=evidence.SHA_A,
        lease_id=context.lease_id,
        resource_id=context.resource_id,
        fencing_token=context.fencing_token,
        created_at=authority.created_at,
    )

    class Loader:
        def load(self, ref):
            return self.material

    loader = Loader()
    loader.material = material
    reader = evidence._PortableReader(suite.root)
    verifier = FormalSearchVerifier(
        loader,
        M1CorrectnessVerifier(suite.protocol, reader),
        M1PerformanceVerifier(suite.protocol, reader),
    )
    return verifier, reference, loader, expected


def test_bridge_uses_existing_d_raw_evidence_verification(tmp_path):
    verifier, reference, _, expected = setup(tmp_path)
    actual = verifier.verify(reference)
    assert actual == expected
    assert actual.restart_effects


@pytest.mark.parametrize("fault", ["raw_hash", "correctness_hash"])
def test_tampering_returns_invalid_not_cached_success(tmp_path, fault):
    verifier, reference, loader, _ = setup(tmp_path)
    if fault == "raw_hash":
        reference = reference.model_copy(update={"raw_evidence_hash": evidence.SHA_A})
    else:
        loader.material = replace(
            loader.material,
            correctness_reference=loader.material.correctness_reference.model_copy(
                update={"sha256": evidence.SHA_A}
            ),
        )
    assert verifier.verify(reference).verdict.value == "invalid"


@pytest.mark.parametrize(
    "field,value",
    [
        ("round_id", uuid4()),
        ("artifact_id", uuid4()),
        ("fencing_token", 999),
        ("phase", RoundPhase.HOLDOUT),
    ],
)
def test_wrong_execution_binding_is_rejected(tmp_path, field, value):
    verifier, reference, _, _ = setup(tmp_path)
    with pytest.raises(Conflict):
        verifier.verify(reference.model_copy(update={field: value}))


def test_raw_d_verification_feeds_search_barrier(tmp_path):
    from hcuopt.contracts.m2_formal_authority_v1 import formal_authority_context_ref
    from hcuopt.evaluation.formal_search import close_formal_search
    from hcuopt.evaluation.m2_models import BarrierMemberResult
    from tests.unit.test_m2_formal_finalizer import _fixture

    verifier, reference, loader, expected = setup(tmp_path)
    store, descriptor, _, _, _ = _fixture()
    authority = loader.material.round_authority
    context = formal_authority_context_ref(descriptor).model_copy(
        update={
            name: getattr(authority, name)
            for name in (
                "round_id",
                "task_id",
                "candidate_family_hash",
                "artifact_family_hash",
                "search_plan_hash",
                "selection_rule_hash",
            )
        }
    )
    measured = BarrierMemberResult(
        round_candidate_id=reference.round_candidate_id,
        candidate_id=reference.candidate_id,
        candidate_state=RoundCandidateState.SEARCH_MEASURED,
        artifact_id=reference.artifact_id,
        artifact_hash=reference.artifact_hash,
        correctness_evidence_hash=evidence.SHA_A,
        round_measurement_ref_id=reference.round_measurement_ref_id,
        budget_usage_evidence_hash=evidence.SHA_A,
        cleanup_evidence_hash=expected.cleanup_hash,
        synthetic=False,
    )
    failed = BarrierMemberResult(
        round_candidate_id=uuid4(),
        candidate_id=uuid4(),
        candidate_state=RoundCandidateState.BUILD_FAILED,
        failure_evidence_hash=evidence.SHA_A,
        budget_usage_evidence_hash=evidence.SHA_A,
        synthetic=False,
    )

    class Repository:
        def record_formal_barrier(self, record):
            self.record = record
            return {}

    repository = Repository()
    record = close_formal_search(
        round_authority=authority,
        context=context,
        members=(measured, failed),
        references=(reference,),
        verifier=verifier,
        publisher=store,
        repository=repository,
        closed_at=authority.created_at,
        closed_by="fixture-d",
        idempotency_key="raw-d-search-test",
    )
    assert repository.record == record
    assert record.barrier.promoted_candidate_ids == (reference.candidate_id,)
    assert record.barrier.expected_member_count == 2
