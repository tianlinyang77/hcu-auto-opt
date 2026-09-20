# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Deployment-side Formal Round materialization; never dispatches work itself."""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid5

from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.contracts.m2_formal_operator_v1 import FormalRoundPlanPreviewView
from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentView, formal_start_content_hash
from hcuopt.domain.enums import (
    ManualCandidateKind,
    ProjectMode,
    RoundCandidateState,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.domain.errors import Conflict
from hcuopt.operator.formal_start import FormalStartCoordinator, FormalStartRepository
from hcuopt.orchestrator.search_round import candidate_family_hash


@dataclass(frozen=True)
class PreparedFormalRound:
    """Internal transient inputs, not an authorization token or persisted receipt."""

    intent: FormalStartIntentView
    preview: FormalRoundPlanPreviewView
    round: SearchRound
    members: tuple[RoundCandidate, ...]
    valid_from: datetime
    valid_until: datetime


def prepare_formal_round(
    coordinator: FormalStartCoordinator,
    intent: FormalStartIntentView,
    repository: FormalStartRepository,
) -> PreparedFormalRound:
    """Revalidate sources and map existing authority fields without inventing any.

    Storage must consume this within a separate version-checked transaction.
    A successful return creates no Task, Round, Job, Lease or hardware access.
    """
    intent = FormalStartIntentView.model_validate(intent.model_dump(mode="json"))
    preview, execution, evaluation = coordinator.validate_for_dispatch(intent, repository)
    plan = preview.resolved_plan
    authority = plan.authority
    if authority is None:
        raise Conflict("Formal dispatch requires a resolved authority")
    if len(intent.candidate_bindings) != len(plan.candidates):
        raise Conflict("Formal dispatch candidate count drifted")
    members = []
    for ordinal, (binding, candidate) in enumerate(
        zip(intent.candidate_bindings, plan.candidates, strict=True)
    ):
        digest = formal_start_content_hash(candidate)
        expected_id = uuid5(intent.round_id, f"{ordinal}:{candidate.candidate_id}:{digest}")
        if (
            binding.ordinal != ordinal
            or candidate.ordinal != ordinal
            or binding.candidate_id != candidate.candidate_id
            or binding.round_candidate_id != expected_id
            or binding.candidate_input_digest != digest
            or candidate.candidate_kind != "business"
            or candidate.hotspot_id != authority.hotspot_id
            or candidate.replacement_point != authority.replacement_point
            or candidate.baseline_source_hash != authority.baseline_source_hash
        ):
            raise Conflict("Formal dispatch candidate binding drifted")
        ref = candidate.source_package_ref
        members.append(
            RoundCandidate(
                round_candidate_id=expected_id,
                round_id=intent.round_id,
                candidate_id=candidate.candidate_id,
                ordinal=ordinal,
                source_package_store_id=candidate.source_package_store_id,
                source_package_store_hash=candidate.source_package_store_hash,
                source_package_hash=ref.source_package_hash,
                source_manifest_version=ref.manifest_schema_version,
                source_manifest_hash=ref.manifest_hash,
                baseline_source_hash=candidate.baseline_source_hash,
                candidate_source_hash=ref.candidate_source_hash,
                optimization_intent=candidate.optimization_intent,
                replacement_point=candidate.replacement_point,
                candidate_kind=ManualCandidateKind.BUSINESS,
                state=RoundCandidateState.INTAKE_ACCEPTED,
                idempotency_key=f"formal-round-member:{expected_id}",
            )
        )
    round_fields = {
        field: getattr(authority, field)
        for field in (
            "target_snapshot_id",
            "stage0_run_id",
            "stage0_protocol_hash",
            "baseline_epoch_id",
            "hotspot_id",
            "replacement_point",
            "workload_id",
            "workload_hash",
            "configuration_hash",
            "image_digest",
            "adapter_profile",
        )
    }
    round_ = SearchRound(
        **round_fields,
        round_id=intent.round_id,
        task_id=intent.task_id,
        idempotency_key=f"formal-round:{intent.intent_id}",
        state=SearchRoundState.INTAKE_CLOSED,
        run_mode=SearchRoundRunMode.FORMAL,
        project_mode=ProjectMode.DEGRADED_MANUAL_INTAKE,
        declared_candidate_count=len(members),
        max_promoted=plan.max_promoted,
        family_alpha=evaluation.family_alpha,
        search_plan_hash=evaluation.search_plan_hash,
        holdout_plan_commitment=evaluation.holdout_plan_commitment,
        holdout_commitment_scheme=evaluation.holdout_commitment_scheme,
        holdout_plan_authority_id=evaluation.holdout_plan_authority_id,
        holdout_plan_authority_hash=evaluation.holdout_plan_authority_hash,
        selection_rule_hash=evaluation.selection_rule_hash,
        budget=execution.budget,
        candidate_family_hash=candidate_family_hash(
            round_fields, [member.model_dump(mode="python") for member in members]
        ),
        version=1,
        created_at=intent.created_at,
        intake_closed_at=intent.ready_at,
    )
    return PreparedFormalRound(
        intent=intent,
        preview=preview,
        round=round_,
        members=tuple(members),
        valid_from=max(
            preview.created_at,
            preview.formal_authorization.window_starts_at,
            execution.issued_at,
            execution.window_starts_at,
            evaluation.issued_at,
        ),
        valid_until=min(
            preview.expires_at,
            preview.formal_authorization.window_expires_at,
            execution.window_expires_at,
            execution.expires_at,
            evaluation.expires_at,
        ),
    )
