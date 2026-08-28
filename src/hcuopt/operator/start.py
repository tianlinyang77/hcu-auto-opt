# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.contracts.operator_v1 import (
    OperatorRoundStartRequest,
    OperatorServiceIdentity,
    OperatorStartCandidateMember,
    OperatorStartIntentView,
    OperatorStartView,
    ResolvedOperatorCandidate,
    ResolvedRoundPlan,
    RoundPlanPreviewView,
)
from hcuopt.domain.enums import (
    ManualCandidateKind,
    RoundCandidateState,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator.errors import (
    OperatorPlanHashMismatch,
    OperatorPreviewBlocked,
    OperatorPreviewExpired,
    OperatorServiceIdentityMismatch,
    OperatorStartFailed,
    OperatorWarningAcknowledgementRequired,
)
from hcuopt.operator.plans import OperatorAuthorityRepository, OperatorPlanCompiler


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def operator_start_request_digest(request: OperatorRoundStartRequest) -> str:
    return _sha256(request.model_dump(mode="json", exclude={"idempotency_key"}))


@dataclass(frozen=True, slots=True)
class FrozenScriptedPlans:
    search_plan_hash: str
    holdout_plan_commitment: str
    holdout_commitment_scheme: str
    holdout_plan_authority_id: str
    holdout_plan_authority_hash: str
    family_alpha: float


class OperatorScriptedPlanAuthority(Protocol):
    def freeze(
        self,
        *,
        round_id: UUID,
        plan: ResolvedRoundPlan,
    ) -> FrozenScriptedPlans: ...


class HmacScriptedPlanAuthority:
    """D-owned synthetic fixture authority with restart-stable sealed Holdout plans."""

    def __init__(
        self,
        secret: bytes,
        *,
        authority_id: str = "m2-operator-scripted-plan-authority",
        authority_version: str = "v1",
        family_alpha: float = 0.05,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("Scripted Plan Authority secret must contain at least 256 bits")
        if not authority_id or len(authority_id) > 300:
            raise ValueError("Scripted Plan Authority ID must be bounded non-empty text")
        if not authority_version or len(authority_version) > 200:
            raise ValueError("Scripted Plan Authority version must be bounded non-empty text")
        if not 0.0 < family_alpha < 1.0:
            raise ValueError("Scripted family alpha must be between zero and one")
        self._secret = bytes(secret)
        self.authority_id = authority_id
        self.family_alpha = family_alpha
        key_fingerprint = _sha256(
            hmac.new(
                self._secret,
                b"hcuopt:operator:authority-key-fingerprint:v1",
                hashlib.sha256,
            ).hexdigest()
        )
        self.authority_hash = _sha256(
            {
                "schema_version": "m2-operator-scripted-plan-authority-v1",
                "authority_id": authority_id,
                "authority_version": authority_version,
                "key_fingerprint": key_fingerprint,
                "synthetic": True,
            }
        )

    def freeze(
        self,
        *,
        round_id: UUID,
        plan: ResolvedRoundPlan,
    ) -> FrozenScriptedPlans:
        if plan.run_mode is not SearchRoundRunMode.SCRIPTED or not plan.synthetic:
            raise OperatorStartFailed("Plan Authority accepts only synthetic Scripted Plans")
        common = {
            "round_id": str(round_id),
            "resolved_plan_hash": _sha256(plan),
            "search_protocol_version": plan.search_protocol_version,
            "search_protocol_hash": plan.search_protocol_hash,
            "holdout_protocol_version": plan.holdout_protocol_version,
            "holdout_protocol_hash": plan.holdout_protocol_hash,
            "selection_rule_hash": plan.selection_rule_hash,
            "budget": plan.budget.model_dump(mode="json"),
            "max_promoted": plan.max_promoted,
            "family_alpha": self.family_alpha,
            "synthetic": True,
        }
        search_plan_hash = _sha256(
            {
                "schema_version": "m2-operator-scripted-search-plan-v1",
                "phase": "search",
                **common,
            }
        )
        holdout_plan = {
            "schema_version": "m2-operator-scripted-holdout-plan-v1",
            "phase": "holdout",
            **common,
        }
        encoded_holdout = canonical_json_bytes(holdout_plan)
        nonce = hmac.new(
            self._secret,
            b"hcuopt:operator:holdout:" + round_id.bytes,
            hashlib.sha256,
        ).digest()
        commitment = "sha256:" + hashlib.sha256(nonce + encoded_holdout).hexdigest()
        return FrozenScriptedPlans(
            search_plan_hash=search_plan_hash,
            holdout_plan_commitment=commitment,
            holdout_commitment_scheme="sha256-nonce-v1",
            holdout_plan_authority_id=self.authority_id,
            holdout_plan_authority_hash=self.authority_hash,
            family_alpha=self.family_alpha,
        )


class OperatorStartRepository(OperatorAuthorityRepository, Protocol):
    def get_operator_plan_preview(self, preview_id: UUID) -> RoundPlanPreviewView: ...

    def create_operator_start_intent(
        self, intent: OperatorStartIntentView
    ) -> tuple[OperatorStartIntentView, bool]: ...

    def get_operator_start_intent(self, intent_id: UUID) -> OperatorStartIntentView: ...

    def get_operator_start_intent_by_idempotency(
        self, idempotency_key: str
    ) -> OperatorStartIntentView | None: ...

    def record_operator_start_plans(
        self, intent_id: UUID, plans: FrozenScriptedPlans
    ) -> OperatorStartIntentView: ...

    def record_operator_start_round_created(
        self, intent_id: UUID
    ) -> OperatorStartIntentView: ...

    def record_operator_start_member_bound(
        self, intent_id: UUID, ordinal: int
    ) -> OperatorStartIntentView: ...

    def record_operator_start_intake_closed(
        self, intent_id: UUID, candidate_family_hash: str
    ) -> OperatorStartIntentView: ...

    def finalize_operator_start_intent(
        self, intent_id: UUID
    ) -> OperatorStartIntentView: ...

    def fail_operator_start_intent(
        self,
        intent_id: UUID,
        *,
        error_code: str,
        error_message: str,
        member_ordinal: int | None = None,
    ) -> OperatorStartIntentView: ...

    def create_search_round(self, request: SearchRound) -> dict[str, object]: ...

    def add_round_candidate(self, request: RoundCandidate) -> dict[str, object]: ...

    def close_search_round_intake(self, round_id: UUID) -> dict[str, object]: ...


class OperatorStartCoordinator:
    def __init__(
        self,
        compiler: OperatorPlanCompiler,
        *,
        plan_authority: OperatorScriptedPlanAuthority | None = None,
    ) -> None:
        self.compiler = compiler
        self.plan_authority = plan_authority

    @property
    def service_identity(self) -> OperatorServiceIdentity:
        return self.compiler.service_identity

    def start(
        self,
        request: OperatorRoundStartRequest,
        repository: OperatorStartRepository,
    ) -> OperatorStartView:
        if request.expected_service_identity.model_dump(mode="json") != (
            self.service_identity.model_dump(mode="json")
        ):
            raise OperatorServiceIdentityMismatch(
                "Operator service identity changed; create a new Preview"
            )
        existing = repository.get_operator_start_intent_by_idempotency(
            request.idempotency_key
        )
        if existing is not None:
            self._validate_existing_request(request, existing)
            reconciled = self.reconcile(existing.intent_id, repository)
            return self._start_view(reconciled, replayed=True)
        preview = repository.get_operator_plan_preview(request.preview_id)
        self._validate_start_request(request, preview)
        if self.plan_authority is None:
            raise OperatorStartFailed("Scripted Plan Authority is not configured")
        intent = self._build_intent(request, preview)
        revalidated = self.compiler.revalidate(
            preview,
            repository,
            allowed_round_id=intent.round_id,
        )
        if not revalidated.start_allowed:
            raise OperatorPreviewBlocked("Operator Preview no longer passes Preflight")
        if revalidated.resolved_plan_hash != preview.resolved_plan_hash:
            raise OperatorPlanHashMismatch("Operator Preview Authority has drifted")
        stored, created = repository.create_operator_start_intent(intent)
        reconciled = self.reconcile(stored.intent_id, repository)
        return self._start_view(reconciled, replayed=not created)

    def reconcile(
        self,
        intent_id: UUID,
        repository: OperatorStartRepository,
    ) -> OperatorStartIntentView:
        intent = repository.get_operator_start_intent(intent_id)
        if intent.state in {"finalized", "failed"}:
            return intent
        if intent.service_identity != self.service_identity:
            raise OperatorServiceIdentityMismatch(
                "Operator StartIntent belongs to another service deployment"
            )
        if self.plan_authority is None:
            raise OperatorStartFailed("Scripted Plan Authority is not configured")
        preview = repository.get_operator_plan_preview(intent.preview_id)
        if preview.resolved_plan_hash != intent.resolved_plan_hash:
            raise OperatorPlanHashMismatch(
                "Operator StartIntent and Preview Plan Hash do not match"
            )
        plans = self.plan_authority.freeze(
            round_id=intent.round_id,
            plan=preview.resolved_plan,
        )
        # This also verifies an already frozen Intent. A deployment that lost or
        # rotated the D-owned key must stop before creating more Round authority.
        repository.record_operator_start_plans(intent_id, plans)
        for _step in range(8):
            intent = repository.get_operator_start_intent(intent_id)
            if intent.state in {"finalized", "failed"}:
                return intent
            if intent.state == "plans_frozen":
                try:
                    repository.create_search_round(self._round(intent, preview))
                except Conflict:
                    return repository.fail_operator_start_intent(
                        intent_id,
                        error_code="operator_round_creation_failed",
                        error_message="SearchRound Authority could not be created.",
                    )
                repository.record_operator_start_round_created(intent_id)
                continue
            if intent.state == "round_created":
                pending = next(
                    (
                        member
                        for member in intent.candidate_members
                        if member.state == "pending"
                    ),
                    None,
                )
                if pending is not None:
                    try:
                        repository.add_round_candidate(
                            self._round_candidate(intent, pending)
                        )
                    except Conflict:
                        return repository.fail_operator_start_intent(
                            intent_id,
                            error_code="operator_candidate_intake_failed",
                            error_message="Candidate Intake could not bind the frozen member.",
                            member_ordinal=pending.ordinal,
                        )
                    repository.record_operator_start_member_bound(
                        intent_id, pending.ordinal
                    )
                    continue
                try:
                    round_row = repository.close_search_round_intake(intent.round_id)
                except Conflict:
                    return repository.fail_operator_start_intent(
                        intent_id,
                        error_code="operator_candidate_intake_failed",
                        error_message="Candidate Intake Close could not freeze the family.",
                    )
                family_hash = round_row.get("candidate_family_hash")
                if not isinstance(family_hash, str):
                    raise OperatorStartFailed(
                        "Candidate Intake Close did not return a frozen family Hash"
                    )
                repository.record_operator_start_intake_closed(intent_id, family_hash)
                continue
            if intent.state == "intake_closed":
                return repository.finalize_operator_start_intent(intent_id)
        raise OperatorStartFailed("StartIntent reconciliation exceeded its bounded steps")

    def _validate_existing_request(
        self,
        request: OperatorRoundStartRequest,
        intent: OperatorStartIntentView,
    ) -> None:
        if (
            intent.preview_id != request.preview_id
            or intent.resolved_plan_hash != request.resolved_plan_hash
            or intent.actor != request.actor
            or intent.idempotency_key != request.idempotency_key
            or intent.request_digest != operator_start_request_digest(request)
        ):
            raise OperatorPlanHashMismatch(
                "Operator Start idempotency key was reused with different inputs"
            )
        if intent.service_identity != self.service_identity:
            raise OperatorServiceIdentityMismatch(
                "Operator StartIntent belongs to another service deployment"
            )

    @staticmethod
    def _start_view(
        intent: OperatorStartIntentView,
        *,
        replayed: bool,
    ) -> OperatorStartView:
        return OperatorStartView.model_validate(
            {
                **intent.model_dump(mode="json"),
                "replayed": replayed,
                "executable": intent.state == "finalized",
            }
        )

    def _validate_start_request(
        self,
        request: OperatorRoundStartRequest,
        preview: RoundPlanPreviewView,
    ) -> None:
        now = datetime.now(timezone.utc)
        if preview.preview_id != request.preview_id:
            raise OperatorPlanHashMismatch("Operator Preview identity does not match")
        if preview.resolved_plan_hash != request.resolved_plan_hash:
            raise OperatorPlanHashMismatch("Operator resolved Plan Hash does not match")
        if preview.expires_at <= now:
            raise OperatorPreviewExpired("Operator Preview has expired")
        if not preview.start_allowed:
            raise OperatorPreviewBlocked("Operator Preview is blocked")
        if request.acknowledged_warning_codes != preview.required_ack_codes:
            raise OperatorWarningAcknowledgementRequired(
                "Operator Preview warning acknowledgements do not match"
            )
        if preview.service_identity != self.service_identity:
            raise OperatorServiceIdentityMismatch(
                "Operator Preview belongs to another service deployment"
            )

    def _build_intent(
        self,
        request: OperatorRoundStartRequest,
        preview: RoundPlanPreviewView,
    ) -> OperatorStartIntentView:
        intent_id = uuid5(
            NAMESPACE_URL, f"hcuopt:operator-start:{request.idempotency_key}"
        )
        task_id = uuid5(intent_id, "task")
        round_id = uuid5(intent_id, "round")
        members = tuple(
            self._member(round_id, intent_id, candidate)
            for candidate in preview.resolved_plan.candidates
        )
        now = datetime.now(timezone.utc)
        return OperatorStartIntentView(
            intent_id=intent_id,
            preview_id=preview.preview_id,
            resolved_plan_hash=preview.resolved_plan_hash,
            request_digest=operator_start_request_digest(request),
            task_id=task_id,
            round_id=round_id,
            actor=request.actor,
            idempotency_key=request.idempotency_key,
            state="preparing",
            candidate_members=members,
            service_identity=self.service_identity,
            version=1,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _member(
        round_id: UUID,
        intent_id: UUID,
        candidate: ResolvedOperatorCandidate,
    ) -> OperatorStartCandidateMember:
        payload = candidate.model_dump(mode="json")
        digest = _sha256(payload)
        candidate_id = candidate.candidate_id
        ordinal = candidate.ordinal
        round_candidate_id = uuid5(
            round_id, f"{ordinal}:{candidate_id}:{digest}"
        )
        return OperatorStartCandidateMember(
            **payload,
            candidate_input_digest=digest,
            round_candidate_id=round_candidate_id,
            intake_idempotency_key=(
                f"operator-start:{intent_id}:{ordinal}:{digest}"
            ),
        )

    @staticmethod
    def _round(
        intent: OperatorStartIntentView,
        preview: RoundPlanPreviewView,
    ) -> SearchRound:
        authority = preview.resolved_plan.authority
        if authority is None:
            raise OperatorPreviewBlocked("Operator Preview Authority is unresolved")
        return SearchRound(
            round_id=intent.round_id,
            task_id=intent.task_id,
            idempotency_key=f"operator-round:{intent.intent_id}",
            state=SearchRoundState.INTAKE_OPEN,
            run_mode=SearchRoundRunMode.SCRIPTED,
            project_mode=None,
            target_snapshot_id=authority.target_snapshot_id,
            stage0_run_id=authority.stage0_run_id,
            stage0_protocol_hash=authority.stage0_protocol_hash,
            baseline_epoch_id=authority.baseline_epoch_id,
            hotspot_id=authority.hotspot_id,
            replacement_point=authority.replacement_point,
            workload_id=authority.workload_id,
            workload_hash=authority.workload_hash,
            configuration_hash=authority.configuration_hash,
            image_digest=authority.image_digest,
            adapter_profile=authority.adapter_profile,
            declared_candidate_count=len(intent.candidate_members),
            max_promoted=preview.resolved_plan.max_promoted,
            family_alpha=intent.family_alpha,
            search_plan_hash=intent.search_plan_hash,
            holdout_plan_commitment=intent.holdout_plan_commitment,
            holdout_commitment_scheme=intent.holdout_commitment_scheme,
            holdout_plan_authority_id=intent.holdout_plan_authority_id,
            holdout_plan_authority_hash=intent.holdout_plan_authority_hash,
            selection_rule_hash=preview.resolved_plan.selection_rule_hash,
            budget=preview.resolved_plan.budget,
            version=1,
            created_at=intent.created_at,
        )

    @staticmethod
    def _round_candidate(
        intent: OperatorStartIntentView,
        member: OperatorStartCandidateMember,
    ) -> RoundCandidate:
        reference = member.source_package_ref
        return RoundCandidate(
            round_candidate_id=member.round_candidate_id,
            round_id=intent.round_id,
            candidate_id=member.candidate_id,
            ordinal=member.ordinal,
            source_package_store_id=member.source_package_store_id,
            source_package_store_hash=member.source_package_store_hash,
            source_package_hash=reference.source_package_hash,
            source_manifest_version=reference.manifest_schema_version,
            source_manifest_hash=reference.manifest_hash,
            baseline_source_hash=member.baseline_source_hash,
            candidate_source_hash=reference.candidate_source_hash,
            optimization_intent=member.optimization_intent,
            replacement_point=member.replacement_point,
            candidate_kind=ManualCandidateKind.FIXTURE,
            state=RoundCandidateState.INTAKE_ACCEPTED,
            idempotency_key=member.intake_idempotency_key,
        )
