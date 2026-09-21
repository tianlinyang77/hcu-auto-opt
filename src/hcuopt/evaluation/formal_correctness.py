# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bind existing M1 verification to a frozen Formal build family.

Deployment must supply durable inputs and a separately acquired correctness lease.
This offline bridge neither launches a process nor advances any database state.
"""

from collections.abc import Sequence

from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.m1_protocol import M1HotspotCorrectnessSpec
from hcuopt.evaluation.m1_verifier import (
    M1CorrectnessEvidenceReference,
    M1CorrectnessVerificationResult,
    M1CorrectnessVerifier,
    M1VerificationContext,
)
from hcuopt.orchestrator.search_round import artifact_family_hash


def verify_formal_correctness(
    *, round_authority: SearchRound, members: Sequence[RoundCandidate],
    context: M1VerificationContext, hotspot: M1HotspotCorrectnessSpec,
    reference: M1CorrectnessEvidenceReference, verifier: M1CorrectnessVerifier,
) -> M1CorrectnessVerificationResult:
    """Re-read raw evidence through D; never accept a producer's passed boolean."""
    round_authority = SearchRound.model_validate(round_authority.model_dump(mode="json"))
    members = [RoundCandidate.model_validate(m.model_dump(mode="json")) for m in members]
    context = M1VerificationContext.model_validate(context.model_dump(mode="python"))
    if round_authority.run_mode != "formal" or round_authority.state != "correctness":
        raise Conflict("Formal correctness requires a frozen correctness-stage Round")
    if any(m.round_id != round_authority.round_id for m in members):
        raise Conflict("Formal correctness member belongs to another Round")
    try:
        computed = artifact_family_hash(
            round_authority.model_dump(mode="json"),
            [m.model_dump(mode="json") for m in members],
        )
    except (ValueError, TypeError, KeyError) as error:
        raise Conflict("Formal correctness requires complete Build terminals") from error
    if computed != round_authority.artifact_family_hash:
        raise Conflict("Formal correctness Artifact Family differs")
    member = next((m for m in members if m.candidate_id == context.candidate_id), None)
    if member is None or member.state != "built" or member.candidate_kind != "business":
        raise Conflict("Formal correctness requires a built business member")
    fields = (
        "task_id", "baseline_epoch_id", "target_snapshot_id", "stage0_run_id",
        "stage0_protocol_hash", "workload_id", "workload_hash",
    )
    if any(getattr(context, f) != getattr(round_authority, f) for f in fields) or any(
        getattr(context, f) != getattr(member, f)
        for f in ("baseline_source_hash", "candidate_source_hash", "artifact_id", "artifact_hash")
    ):
        raise Conflict("Formal correctness context differs from frozen inputs")
    return verifier.verify(context, hotspot, reference)
