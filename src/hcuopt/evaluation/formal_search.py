# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""D-side Search barrier producer. No HCU execution or producer-side verdicts."""

from datetime import datetime
from statistics import fmean
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from hcuopt.contracts.m2 import SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextRef,
)
from hcuopt.domain.enums import (
    ManualCandidateVerdict,
    RoundBarrierOutcome,
    RoundCandidateState,
    RoundPhase,
    SearchRoundRunMode,
)
from hcuopt.evaluation.endpoint_selection import FormalSearchSelectionInput, SearchSelectionScore
from hcuopt.evaluation.m1_verifier import M1PerformanceVerificationResult
from hcuopt.evaluation.m2_formal_authority import (
    FormalBarrierPersistence,
    formal_authority_payload_hash,
)
from hcuopt.evaluation.m2_models import BarrierMemberResult, RoundBarrierResult
from hcuopt.evaluation.m2_statistics import holdout_family_hash
from hcuopt.measurement.evidence import EvidenceArtifact
from hcuopt.measurement.m2_models import RoundMeasurementRef, validate_round_measurement_refs


class SearchEvidenceVerifier(Protocol):
    """Trusted D adapter must reread raw evidence; callers cannot supply scores."""

    def verify(self, reference: RoundMeasurementRef) -> M1PerformanceVerificationResult: ...


class SearchEvidencePublisher(Protocol):
    def publish(self, value: object) -> EvidenceArtifact: ...


class SearchBarrierRepository(Protocol):
    def record_formal_barrier(self, record: FormalBarrierPersistence) -> dict: ...


def close_formal_search(
    *,
    round_authority: SearchRound,
    context: FormalAuthorityContextRef,
    members: tuple[BarrierMemberResult, ...],
    references: tuple[RoundMeasurementRef, ...],
    verifier: SearchEvidenceVerifier,
    publisher: SearchEvidencePublisher,
    repository: SearchBarrierRepository,
    closed_at: datetime,
    closed_by: str,
    idempotency_key: str,
) -> FormalBarrierPersistence:
    """Publish immutable D evidence before atomically persisting the batch barrier.

    Repository admission rechecks current state and frozen membership under lock.
    A persistence error never repeats physical measurements; publication is content-addressed.
    """
    if (
        round_authority.run_mode is not SearchRoundRunMode.FORMAL
        or round_authority.round_id != context.round_id
        or round_authority.task_id != context.task_id
        or round_authority.candidate_family_hash != context.candidate_family_hash
        or round_authority.search_plan_hash != context.search_plan_hash
        or round_authority.artifact_family_hash != context.artifact_family_hash
        or round_authority.selection_rule_hash != context.selection_rule_hash
    ):
        raise ValueError("Formal Search authority differs")
    ordered = tuple(sorted(members, key=lambda m: str(m.candidate_id)))
    if (
        len(ordered) != round_authority.declared_candidate_count
        or len({m.candidate_id for m in ordered}) != len(ordered)
        or any(m.synthetic for m in ordered)
    ):
        raise ValueError("Formal Search must collect the complete real family")
    measured = {
        m.candidate_id: m
        for m in ordered
        if m.candidate_state is RoundCandidateState.SEARCH_MEASURED
    }
    refs = validate_round_measurement_refs(references)
    if {r.candidate_id for r in refs} != set(measured):
        raise ValueError("Formal Search references differ from measured members")
    scores = []
    replacements = {}
    for ref in refs:
        member = measured[ref.candidate_id]
        if (
            ref.phase is not RoundPhase.SEARCH
            or ref.round_id != round_authority.round_id
            or ref.candidate_family_hash != round_authority.candidate_family_hash
            or ref.artifact_family_hash != round_authority.artifact_family_hash
            or ref.phase_plan_hash != round_authority.search_plan_hash
            or ref.round_candidate_id != member.round_candidate_id
            or ref.round_measurement_ref_id != member.round_measurement_ref_id
            or ref.artifact_id != member.artifact_id
            or ref.artifact_hash != member.artifact_hash
        ):
            raise ValueError("Formal Search measurement binding differs")
        result = verifier.verify(ref)
        if (
            result.measurement_id != ref.measurement_id
            or result.raw_evidence_hash != ref.raw_evidence_hash
            or result.raw_evidence_uri != ref.raw_evidence_uri
        ):
            raise ValueError("D returned a result for another measurement")
        if result.verdict is ManualCandidateVerdict.INVALID:
            failure = publisher.publish(result)
            if failure.sha256 != formal_authority_payload_hash(result):
                raise ValueError("D failure publication hash differs")
            replacements[member.candidate_id] = member.model_copy(
                update={
                    "candidate_state": RoundCandidateState.INVALID,
                    "round_measurement_ref_id": None,
                    "failure_evidence_hash": failure.sha256,
                }
            )
            continue
        if (
            len(result.restart_effects) < 4
            or result.failure_codes
            or result.plan_hash != ref.measurement_plan_hash
            or result.lease_id != ref.lease_id
            or result.resource_id != ref.resource_id
            or result.fencing_token != ref.fencing_token
            or result.cleanup_hash != member.cleanup_evidence_hash
        ):
            raise ValueError("D result lacks complete restart or cleanup evidence")
        scores.append(
            SearchSelectionScore(
                candidate_id=ref.candidate_id,
                round_measurement_ref_id=ref.round_measurement_ref_id,
                mean_effect=fmean(result.restart_effects),
            )
        )
    summary = FormalSearchSelectionInput(
        round_id=round_authority.round_id,
        artifact_family_hash=context.artifact_family_hash,
        selection_rule_hash=context.selection_rule_hash,
        scores=tuple(sorted(scores, key=lambda s: str(s.candidate_id))),
    )
    ranked = sorted(
        (s for s in scores if s.mean_effect > 0),
        key=lambda s: (-s.mean_effect, str(s.candidate_id)),
    )
    promoted = tuple(
        sorted((s.candidate_id for s in ranked[: round_authority.max_promoted]), key=str)
    )
    final_members = tuple(replacements.get(m.candidate_id, m) for m in ordered)
    digest = formal_authority_payload_hash(summary)
    barrier = RoundBarrierResult(
        barrier_id=uuid5(NAMESPACE_URL, f"hcuopt:formal-search:{digest}"),
        round_id=round_authority.round_id,
        run_mode=SearchRoundRunMode.FORMAL,
        synthetic=False,
        phase=RoundPhase.SEARCH,
        input_family_hash=context.artifact_family_hash,
        expected_member_count=len(final_members),
        members=final_members,
        rule_version="formal-search-mean-v1",
        rule_hash=context.selection_rule_hash,
        promoted_candidate_ids=promoted,
        outcome=RoundBarrierOutcome.MEMBERS_PROMOTED
        if promoted
        else RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE,
        input_summary_hash=digest,
        closed_by=closed_by,
        closed_at=closed_at,
        idempotency_key=idempotency_key,
    )
    record = FormalBarrierPersistence(
        context=context,
        barrier=barrier,
        payload_hash=formal_authority_payload_hash(barrier),
        holdout_family_hash=holdout_family_hash(
            round_authority=round_authority,
            members=tuple(m for m in final_members if m.candidate_id in promoted),
        )
        if promoted
        else None,
    )
    if publisher.publish(summary).sha256 != digest:
        raise ValueError("Search summary publication hash differs")
    repository.record_formal_barrier(record)
    return record
