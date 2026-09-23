# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Prepare a phase from deployment-owned materials, without acquiring any resource.

This is not an HTTP input model or a grant. The caller must load current Round,
member and Authority records; B rechecks live Lease/Target Lock at execution.
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from hcuopt.contracts.formal_profile_authorization_v1 import FormalProfileWindowAuthorization
from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import FormalAuthorityContextDescriptor
from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalExecutionBinding,
    M2FormalExecutionWindow,
    M2FormalPhaseExecutionRequest,
)
from hcuopt.domain.enums import RoundPhase
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.m2_formal_runner import M2FormalPhaseExecutionAdapter
from hcuopt.measurement.m2_models import M2PhaseBudgetReservationPlan

# Only deployment observations may enter here. Identity, Artifact, Plan, window,
# budget and phase bindings are derived from their existing authoritative objects.
DEPLOYMENT_PHASE_FIELDS = frozenset(
    "target_lock_hash execution_host_hash device_index numa_node cpu_affinity "
    "topology_lease_hash lease_id fencing_token lease_authority_hash lease_receipt_uri "
    "lease_receipt_hash lease_acquired_at lease_last_renewed_at lease_renewal_due_at "
    "lease_renewal_sequence lease_expires_at".split()
)


def prepare_formal_phase_request(
    *,
    adapter: M2FormalPhaseExecutionAdapter,
    round_authority: SearchRound,
    formal_authority: FormalAuthorityContextDescriptor,
    member: RoundCandidate,
    authorization_id: UUID,
    resolved_plan_hash: str,
    reservation: M2PhaseBudgetReservationPlan,
    deployment: Mapping[str, Any],
    harness_payload: Mapping[str, Any] | None = None,
) -> M2FormalPhaseExecutionRequest:
    """Assemble and validate; no journal, budget reservation, probe or Harness call.

Deployment fields remain evidence claims until B's live readers verify them.
Success does not mean execution-ready, resource ownership, or D acceptance.
"""
    missing = DEPLOYMENT_PHASE_FIELDS - deployment.keys()
    extra = deployment.keys() - DEPLOYMENT_PHASE_FIELDS
    if missing or extra:
        raise Conflict(
            f"Formal deployment material fields differ: missing={sorted(missing)}, "
            f"unexpected={sorted(extra)}"
        )
    # Revalidation also rejects invalid model_copy/update objects and stale context hashes.
    round_authority = SearchRound.model_validate(round_authority.model_dump(mode="json"))
    member = RoundCandidate.model_validate(member.model_dump(mode="json"))
    formal_authority = FormalAuthorityContextDescriptor.model_validate(
        formal_authority.model_dump(mode="json")
    )
    reservation = M2PhaseBudgetReservationPlan.model_validate(reservation.model_dump(mode="json"))
    if (
        not round_authority.artifact_family_hash
        or not member.artifact_id
        or not member.artifact_hash
    ):
        raise Conflict("Formal phase requires a frozen Artifact Family and a built member")
    if (
        formal_authority.round_id != round_authority.round_id
        or formal_authority.task_id != round_authority.task_id
        or formal_authority.target_snapshot_id != round_authority.target_snapshot_id
    ):
        raise Conflict("Formal phase Authority belongs to another Round, Task or target")
    authorization = FormalProfileWindowAuthorization.model_validate(
        adapter.authority_reader.load_authorization(authorization_id)
    )
    phase = reservation.phase
    phase_plan_hash = (
        round_authority.search_plan_hash
        if phase is RoundPhase.SEARCH
        else round_authority.holdout_plan_hash
    )
    if phase_plan_hash is None:
        raise Conflict("Formal Holdout phase requires its revealed Plan")
    profile = adapter.adapter_profile
    binding = M2FormalExecutionBinding(
        **dict(deployment),
        formal_authorization_id=authorization_id,
        formal_authorization_hash=authorization.authorization_hash,
        resolved_plan_hash=resolved_plan_hash,
        authority_context_id=formal_authority.authority_context_id,
        authority_context_hash=formal_authority.context_hash,
        round_id=round_authority.round_id,
        task_id=round_authority.task_id,
        candidate_family_hash=round_authority.candidate_family_hash,
        artifact_family_hash=round_authority.artifact_family_hash,
        holdout_family_hash=round_authority.holdout_family_hash,
        round_candidate_id=member.round_candidate_id,
        candidate_id=member.candidate_id,
        artifact_id=member.artifact_id,
        artifact_hash=member.artifact_hash,
        target_snapshot_id=round_authority.target_snapshot_id,
        target_profile_hash=formal_authority.target_profile_hash,
        workload_profile_hash=formal_authority.workload_profile_hash,
        measurement_profile_hash=formal_authority.measurement_profile_hash,
        stage0_protocol_hash=round_authority.stage0_protocol_hash,
        adapter_profile=profile.profile_id,
        adapter_profile_version=profile.profile_version,
        adapter_profile_hash=profile.profile_hash,
        phase=phase,
        phase_plan_hash=phase_plan_hash,
        measurement_plan_hash=reservation.measurement_plan_hash,
        holdout_reveal_evidence_hash=(
            round_authority.holdout_reveal_evidence_hash
            if phase is RoundPhase.HOLDOUT else None
        ),
        host_id=authorization.host_id,
        resource_id=authorization.resource_id,
        window=M2FormalExecutionWindow(
            starts_at=authorization.window_starts_at,
            expires_at=authorization.window_expires_at,
        ),
        job_id=reservation.job_id,
        attempt=reservation.attempt,
    )
    request = M2FormalPhaseExecutionRequest(
        binding=binding,
        reservation=reservation,
        harness_payload=dict(harness_payload or {}),
    )
    # Reuse B's checks instead of maintaining a second authorization policy.
    # These checks read authority only; live resource checks remain in run().
    adapter._validate_authority(round_authority, formal_authority, member, request)
    adapter._validate_deployment_authority(
        round_authority, formal_authority, member, request, adapter.clock()
    )
    return request
