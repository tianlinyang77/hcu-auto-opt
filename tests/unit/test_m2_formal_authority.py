# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextContent,
    FormalAuthorityContextDescriptor,
    FormalEvidenceStoreRef,
    FormalVerifierRef,
    formal_authority_context_ref,
    publish_formal_authority_context,
)
from hcuopt.domain.enums import (
    ManualCandidateVerdict,
    RoundBarrierOutcome,
    RoundCandidateState,
    RoundPhase,
    RoundTerminalReason,
    SearchRoundRunMode,
)
from hcuopt.evaluation.m2_formal_authority import (
    FormalBarrierPersistence,
    FormalEvidenceBundlePersistence,
    FormalHoldoutRevealPersistence,
    FormalMultipleComparisonPersistence,
    formal_authority_payload_hash,
)
from hcuopt.evaluation.m2_models import (
    AdjustedCandidateResult,
    BarrierMemberResult,
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundCandidateEvidence,
    RoundEvidenceBundle,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _hash(value: str) -> str:
    return "sha256:" + value * 64


def _context() -> FormalAuthorityContextDescriptor:
    return publish_formal_authority_context(
        FormalAuthorityContextContent(
            authority_context_id=_uuid(1),
            round_id=_uuid(2),
            task_id=_uuid(3),
            target_snapshot_id=_uuid(4),
            stage0_run_id=_uuid(5),
            stage0_protocol_hash=_hash("1"),
            baseline_epoch_id=_uuid(6),
            hotspot_id=_uuid(7),
            target_profile_hash=_hash("2"),
            workload_profile_hash=_hash("3"),
            measurement_profile_hash=_hash("4"),
            candidate_family_hash=_hash("5"),
            artifact_family_hash=_hash("6"),
            search_plan_hash=_hash("7"),
            holdout_plan_commitment=_hash("8"),
            holdout_plan_authority_id="d-holdout-authority-v1",
            holdout_plan_authority_hash=_hash("9"),
            selection_rule_hash=_hash("a"),
            evidence_store=FormalEvidenceStoreRef(
                store_id="formal-evidence-v1",
                store_version=1,
                store_hash=_hash("b"),
                access_policy_hash=_hash("c"),
            ),
            verifier=FormalVerifierRef(
                verifier_id="m2-d-verifier",
                verifier_version="m2-d-formal-v1",
                verifier_hash=_hash("d"),
            ),
            sealed_by="operator-a",
            sealed_at=NOW,
        )
    )


def _search_member(ordinal: int) -> BarrierMemberResult:
    return BarrierMemberResult(
        round_candidate_id=_uuid(20 + ordinal),
        candidate_id=_uuid(30 + ordinal),
        candidate_state=RoundCandidateState.SEARCH_MEASURED,
        artifact_id=_uuid(40 + ordinal),
        artifact_hash=_hash(str(ordinal + 1)),
        correctness_evidence_hash=_hash(str(ordinal + 3)),
        round_measurement_ref_id=_uuid(50 + ordinal),
        budget_usage_evidence_hash=_hash(str(ordinal + 5)),
        cleanup_evidence_hash=_hash(str(ordinal + 7)),
        synthetic=False,
    )


def _search_barrier() -> RoundBarrierResult:
    members = (_search_member(0), _search_member(1))
    return RoundBarrierResult(
        barrier_id=_uuid(60),
        round_id=_context().round_id,
        run_mode=SearchRoundRunMode.FORMAL,
        synthetic=False,
        phase=RoundPhase.SEARCH,
        input_family_hash=_context().artifact_family_hash,
        expected_member_count=2,
        members=members,
        rule_version="m2-search-v1",
        rule_hash=_hash("e"),
        promoted_candidate_ids=(members[0].candidate_id,),
        outcome=RoundBarrierOutcome.MEMBERS_PROMOTED,
        input_summary_hash=_hash("f"),
        closed_by="verifier-d",
        closed_at=NOW,
        idempotency_key="formal-search-barrier-001",
    )


def _multiple_comparison() -> MultipleComparisonResult:
    candidate = AdjustedCandidateResult(
        candidate_id=_uuid(30),
        round_measurement_ref_id=_uuid(70),
        synthetic=False,
        correctness_evidence_hash=_hash("1"),
        raw_evidence_hash=_hash("2"),
        baseline_sample_set_hash=_hash("3"),
        verdict=ManualCandidateVerdict.INCONCLUSIVE,
        adjusted_ci_lower=-0.01,
        adjusted_ci_upper=0.01,
        stage0_mde_ratio=0.02,
        workload_mde_ratio=0.02,
        credible_threshold=0.02,
    )
    return MultipleComparisonResult(
        multiple_comparison_id=_uuid(71),
        round_id=_context().round_id,
        run_mode=SearchRoundRunMode.FORMAL,
        synthetic=False,
        holdout_barrier_id=_uuid(72),
        holdout_family_hash=_hash("4"),
        protocol_version="m2-bonferroni-v1",
        protocol_hash=_hash("5"),
        family_alpha=0.05,
        m=1,
        alpha_candidate=0.05,
        candidate_results=(candidate,),
        result_hash=_hash("6"),
        created_at=NOW,
    )


def _zero_promotion_bundle() -> RoundEvidenceBundle:
    candidate_evidence = tuple(
        RoundCandidateEvidence(
            round_candidate_id=_uuid(20 + ordinal),
            candidate_id=_uuid(30 + ordinal),
            search_member_state=RoundCandidateState.SEARCH_MEASURED,
            search_evidence_hash=_hash(str(ordinal + 1)),
            budget_evidence_hashes=(_hash(str(ordinal + 3)),),
            cleanup_evidence_hashes=(_hash(str(ordinal + 5)),),
        )
        for ordinal in range(2)
    )
    context = _context()
    return RoundEvidenceBundle(
        round_evidence_bundle_id=_uuid(80),
        round_id=context.round_id,
        task_id=context.task_id,
        run_mode=SearchRoundRunMode.FORMAL,
        terminal_reason=RoundTerminalReason.NO_PROMOTABLE_CANDIDATE,
        candidate_family_hash=context.candidate_family_hash,
        artifact_family_hash=context.artifact_family_hash,
        search_plan_hash=context.search_plan_hash,
        holdout_plan_commitment=context.holdout_plan_commitment,
        candidate_evidence=candidate_evidence,
        search_barrier_id=_uuid(60),
        budget_ledger_hash=_hash("7"),
        evidence_index_uri="file:///protected/m2/evidence-index.json",
        evidence_index_hash=_hash("8"),
        summary={"decision": "no_promotable_candidate"},
        synthetic=False,
        created_at=NOW,
    )


def test_formal_context_is_deterministic_frozen_and_hash_bound() -> None:
    first = _context()
    second = _context()

    assert first == second
    assert first.context_hash == second.context_hash
    assert first.synthetic is False
    assert first.automatic_release_allowed is False
    with pytest.raises(ValidationError, match="frozen_instance"):
        first.sealed_by = "changed"

    raw = first.model_dump(mode="json")
    raw["context_hash"] = _hash("0")
    with pytest.raises(ValidationError, match="context_hash"):
        FormalAuthorityContextDescriptor.model_validate(raw)


def test_formal_barrier_binds_context_family_mode_and_payload_hash() -> None:
    context = formal_authority_context_ref(_context())
    barrier = _search_barrier()
    record = FormalBarrierPersistence(
        context=context,
        barrier=barrier,
        payload_hash=formal_authority_payload_hash(barrier),
    )

    assert record.barrier.input_family_hash == context.artifact_family_hash
    with pytest.raises(ValidationError, match="Artifact Family"):
        FormalBarrierPersistence(
            context=context,
            barrier=barrier.model_copy(update={"input_family_hash": _hash("0")}),
            payload_hash=formal_authority_payload_hash(
                barrier.model_copy(update={"input_family_hash": _hash("0")})
            ),
        )
    with pytest.raises(ValidationError, match="payload_hash"):
        FormalBarrierPersistence(
            context=context,
            barrier=barrier,
            payload_hash=_hash("0"),
        )


def test_scripted_barrier_cannot_enter_formal_persistence() -> None:
    barrier = _search_barrier()
    scripted_members = tuple(
        item.model_copy(
            update={
                "round_measurement_ref_id": None,
                "scripted_phase_receipt_id": _uuid(90 + ordinal),
                "synthetic": True,
            }
        )
        for ordinal, item in enumerate(barrier.members)
    )
    scripted = barrier.model_copy(
        update={
            "run_mode": SearchRoundRunMode.SCRIPTED,
            "synthetic": True,
            "members": scripted_members,
        }
    )

    with pytest.raises(ValidationError, match="Authority Context"):
        FormalBarrierPersistence(
            context=formal_authority_context_ref(_context()),
            barrier=scripted,
            payload_hash=formal_authority_payload_hash(scripted),
        )


def test_reveal_persists_only_protected_reference_and_fencing() -> None:
    payload = {
        "context": formal_authority_context_ref(_context()),
        "search_barrier_id": _uuid(60),
        "reveal_lease_id": _uuid(61),
        "fencing_token": 7,
        "holdout_family_hash": _hash("1"),
        "holdout_plan_hash": _hash("2"),
        "reveal_evidence_uri": "file:///protected/m2/reveal.json",
        "reveal_evidence_hash": _hash("3"),
        "revealed_by": "verifier-d",
        "revealed_at": NOW,
    }
    record = FormalHoldoutRevealPersistence.model_validate(payload)

    assert record.synthetic is False
    assert record.automatic_release_allowed is False
    with pytest.raises(ValidationError, match="extra_forbidden"):
        FormalHoldoutRevealPersistence.model_validate({**payload, "nonce": "secret"})
    with pytest.raises(ValidationError, match="absolute local file URI"):
        FormalHoldoutRevealPersistence.model_validate(
            {**payload, "reveal_evidence_uri": "https://example.invalid/reveal.json"}
        )
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        FormalHoldoutRevealPersistence.model_validate({**payload, "fencing_token": 0})


def test_fwer_and_bundle_records_rehash_and_bind_formal_identity() -> None:
    context = formal_authority_context_ref(_context())
    comparison = _multiple_comparison()
    bundle = _zero_promotion_bundle()

    comparison_record = FormalMultipleComparisonPersistence(
        context=context,
        result=comparison,
        payload_hash=formal_authority_payload_hash(comparison),
    )
    bundle_record = FormalEvidenceBundlePersistence(
        context=context,
        bundle=bundle,
        payload_hash=formal_authority_payload_hash(bundle),
    )

    assert comparison_record.synthetic is False
    assert bundle_record.automatic_release_allowed is False
    drifted = bundle.model_copy(update={"search_plan_hash": _hash("0")})
    with pytest.raises(ValidationError, match="Authority Context"):
        FormalEvidenceBundlePersistence(
            context=context,
            bundle=drifted,
            payload_hash=formal_authority_payload_hash(drifted),
        )
