# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid5

from hcuopt.contracts.m2_formal_operator_v1 import FormalRoundPlanPreviewView
from hcuopt.contracts.m2_formal_start_v1 import (
    FormalEvaluationStartAuthority,
    FormalExecutionStartAuthority,
    FormalStartActorAssertion,
    FormalStartCandidateBinding,
    FormalStartIntentActionRequest,
    FormalStartIntentRequest,
    FormalStartIntentResult,
    FormalStartIntentView,
    FormalStartSignerRef,
    derive_formal_start_ids,
    formal_start_action_subject_digest,
    formal_start_content_hash,
    formal_start_request_subject_digest,
)
from hcuopt.contracts.operator_v1 import OperatorServiceIdentity
from hcuopt.domain.errors import NotFound
from hcuopt.operator.errors import (
    OperatorFormalAuthorizationInvalid,
    OperatorFormalAuthorizationNotActive,
    OperatorFormalStartAuthenticationInvalid,
    OperatorFormalStartAuthorityInvalid,
    OperatorPlanHashMismatch,
    OperatorPreviewExpired,
    OperatorProfileModeMismatch,
    OperatorProfileNotFound,
    OperatorProfileRevoked,
    OperatorServiceIdentityMismatch,
)
from hcuopt.operator.formal_plans import (
    FormalOperatorAuthorityRepository,
    FormalOperatorPlanCompiler,
    formal_operator_resolved_plan_hash,
)


class FormalStartObjectStore(Protocol):
    """Deployment-owned content-addressed inputs; never populated by an API request."""

    def load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView: ...

    def load_execution_authority(
        self, authority_hash: str
    ) -> FormalExecutionStartAuthority: ...

    def load_evaluation_authority(
        self, authority_hash: str
    ) -> FormalEvaluationStartAuthority: ...


class DeploymentFormalStartSignatureVerifier(Protocol):
    @property
    def signer_ref(self) -> FormalStartSignerRef: ...

    def verify_signature(self, *, content_hash: str, signature: str) -> bool: ...


class FormalStartRepository(FormalOperatorAuthorityRepository, Protocol):
    def create_formal_start_intent(
        self, intent: FormalStartIntentView
    ) -> tuple[FormalStartIntentView, bool]: ...

    def get_formal_start_intent(self, intent_id: UUID) -> FormalStartIntentView: ...

    def get_formal_start_intent_by_idempotency(
        self, idempotency_key: str
    ) -> FormalStartIntentView | None: ...

    def record_formal_start_reconciliation(
        self,
        intent_id: UUID,
        *,
        state: str,
        blocker_codes: tuple[str, ...],
        checked_at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> FormalStartIntentView: ...

    def cancel_formal_start_intent(
        self, intent_id: UUID, *, cancelled_at: datetime
    ) -> FormalStartIntentView: ...

    def list_recoverable_formal_start_intent_ids(self, limit: int) -> tuple[UUID, ...]: ...


class FormalStartCoordinator:
    """A-owned non-executing StartIntent gate for exact B and D authorities."""

    def __init__(
        self,
        compiler: FormalOperatorPlanCompiler,
        *,
        object_store: FormalStartObjectStore,
        actor_verifier: DeploymentFormalStartSignatureVerifier | None,
        execution_verifier: DeploymentFormalStartSignatureVerifier | None,
        evaluation_verifier: DeploymentFormalStartSignatureVerifier | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.compiler = compiler
        self.object_store = object_store
        self.actor_verifier = actor_verifier
        self.execution_verifier = execution_verifier
        self.evaluation_verifier = evaluation_verifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def service_identity(self) -> OperatorServiceIdentity:
        return self.compiler.service_identity

    def create(
        self,
        request: FormalStartIntentRequest,
        repository: FormalStartRepository,
    ) -> FormalStartIntentResult:
        now = self._now()
        self._require_verifiers()
        self._verify_actor(
            request.actor_assertion,
            action="create",
            subject_digest=formal_start_request_subject_digest(request),
            now=now,
        )
        if request.expected_service_identity.model_dump(mode="json") != (
            self.service_identity.model_dump(mode="json")
        ):
            raise OperatorServiceIdentityMismatch(
                "Formal Start request targets another service deployment"
            )
        existing = repository.get_formal_start_intent_by_idempotency(
            request.idempotency_key
        )
        if existing is not None:
            self._validate_existing(request, existing)
            reconciled = self._reconcile(existing.intent_id, repository, now=now)
            return self._result(reconciled, replayed=True)

        preview = self._load_preview(request.preview_id)
        self._validate_preview(request, preview, now=now)
        intent = self._build_intent(request, preview, now=now)
        stored, created = repository.create_formal_start_intent(intent)
        if not created:
            self._validate_existing(request, stored)
        reconciled = self._reconcile(stored.intent_id, repository, now=now)
        return self._result(reconciled, replayed=not created)

    def reconcile(
        self,
        request: FormalStartIntentActionRequest,
        repository: FormalStartRepository,
    ) -> FormalStartIntentView:
        if request.action != "reconcile":
            raise OperatorFormalStartAuthenticationInvalid(
                "Formal Start reconcile requires a reconcile assertion"
            )
        now = self._now()
        self._require_verifiers()
        self._verify_actor(
            request.actor_assertion,
            action="reconcile",
            subject_digest=formal_start_action_subject_digest(
                action="reconcile", intent_id=request.intent_id
            ),
            now=now,
        )
        intent = repository.get_formal_start_intent(request.intent_id)
        self._require_local_intent(intent)
        self._require_intent_actor(intent, request.actor_assertion)
        return self._reconcile(request.intent_id, repository, now=now)

    def cancel(
        self,
        request: FormalStartIntentActionRequest,
        repository: FormalStartRepository,
    ) -> FormalStartIntentView:
        if request.action != "cancel":
            raise OperatorFormalStartAuthenticationInvalid(
                "Formal Start cancel requires a cancel assertion"
            )
        now = self._now()
        self._require_verifiers()
        self._verify_actor(
            request.actor_assertion,
            action="cancel",
            subject_digest=formal_start_action_subject_digest(
                action="cancel", intent_id=request.intent_id
            ),
            now=now,
        )
        intent = repository.get_formal_start_intent(request.intent_id)
        self._require_local_intent(intent)
        self._require_intent_actor(intent, request.actor_assertion)
        return repository.cancel_formal_start_intent(
            request.intent_id, cancelled_at=now
        )

    def recover(
        self,
        repository: FormalStartRepository,
        *,
        limit: int = 100,
    ) -> tuple[FormalStartIntentView, ...]:
        """Internal restart hook; no Web write path and no Round/HCU side effect."""

        self._require_verifiers()
        if limit < 1 or limit > 1000:
            raise ValueError("Formal Start recovery limit must be between 1 and 1000")
        recovered: list[FormalStartIntentView] = []
        for intent_id in repository.list_recoverable_formal_start_intent_ids(limit):
            recovered.append(self._reconcile(intent_id, repository, now=self._now()))
        return tuple(recovered)

    def _reconcile(
        self,
        intent_id: UUID,
        repository: FormalStartRepository,
        *,
        now: datetime,
    ) -> FormalStartIntentView:
        intent = repository.get_formal_start_intent(intent_id)
        self._require_local_intent(intent)
        if intent.state in {"cancelled", "failed"}:
            return intent

        try:
            preview = self._load_preview(intent.preview_id)
            self._validate_intent_preview(intent, preview, now=now)
            revalidated = self.compiler.revalidate(
                preview,
                repository,
                allowed_round_id=intent.round_id,
            )
            if revalidated.resolved_plan_hash != intent.resolved_plan_hash:
                raise OperatorPlanHashMismatch(
                    "Formal Start Plan Authority drifted during revalidation"
                )
            non_start_blocks = tuple(
                sorted(
                    item.code
                    for item in revalidated.checks
                    if item.status == "block"
                    and item.code != "formal_start_authority_not_bound"
                )
            )
            if non_start_blocks:
                return repository.record_formal_start_reconciliation(
                    intent_id,
                    state="awaiting_authority",
                    blocker_codes=non_start_blocks,
                    checked_at=now,
                )
            missing: list[str] = []
            try:
                execution = FormalExecutionStartAuthority.model_validate(
                    self.object_store.load_execution_authority(
                        intent.execution_authority_hash
                    )
                )
            except (NotFound, OSError):
                missing.append("formal_execution_start_authority_unavailable")
                execution = None
            except Exception as error:
                raise OperatorFormalStartAuthorityInvalid(
                    "B execution Start Authority is malformed"
                ) from error
            try:
                evaluation = FormalEvaluationStartAuthority.model_validate(
                    self.object_store.load_evaluation_authority(
                        intent.evaluation_authority_hash
                    )
                )
            except (NotFound, OSError):
                missing.append("formal_evaluation_start_authority_unavailable")
                evaluation = None
            except Exception as error:
                raise OperatorFormalStartAuthorityInvalid(
                    "D evaluation Start Authority is malformed"
                ) from error
            if missing:
                return repository.record_formal_start_reconciliation(
                    intent_id,
                    state="awaiting_authority",
                    blocker_codes=tuple(sorted(missing)),
                    checked_at=now,
                )
            assert execution is not None and evaluation is not None
            self._verify_execution_authority(intent, preview, execution, now=now)
            self._verify_evaluation_authority(intent, preview, evaluation, now=now)
            self._require_authority_role_separation(
                intent,
                execution,
                evaluation,
                preview,
            )
        except (OperatorFormalAuthorizationNotActive, OperatorProfileNotFound):
            return repository.record_formal_start_reconciliation(
                intent_id,
                state="awaiting_authority",
                blocker_codes=("formal_operator_authority_revalidation_failed",),
                checked_at=now,
            )
        except OperatorFormalStartAuthorityInvalid:
            return repository.record_formal_start_reconciliation(
                intent_id,
                state="failed",
                blocker_codes=(),
                checked_at=now,
                error_code="formal_start_authority_invalid",
                error_message="Formal Start Authority verification failed closed.",
            )
        except (
            OperatorFormalAuthorizationInvalid,
            OperatorPlanHashMismatch,
            OperatorPreviewExpired,
            OperatorProfileModeMismatch,
            OperatorProfileRevoked,
            OperatorServiceIdentityMismatch,
            ValueError,
        ):
            return repository.record_formal_start_reconciliation(
                intent_id,
                state="failed",
                blocker_codes=(),
                checked_at=now,
                error_code="formal_start_plan_invalid",
                error_message="Formal Start Plan or window is no longer valid.",
            )

        return repository.record_formal_start_reconciliation(
            intent_id,
            state="ready_for_round_creation",
            blocker_codes=(),
            checked_at=now,
        )

    def _verify_execution_authority(
        self,
        intent: FormalStartIntentView,
        preview: FormalRoundPlanPreviewView,
        authority: FormalExecutionStartAuthority,
        *,
        now: datetime,
    ) -> None:
        verifier = self.execution_verifier
        assert verifier is not None
        self._verify_signed(
            verifier,
            authority.signer,
            authority.authority_hash,
            authority.signature,
            label="execution",
        )
        plan = preview.resolved_plan
        adapter_profile = plan.authority.adapter_profile if plan.authority else None
        expected = (
            intent.execution_authority_hash,
            intent.intent_id,
            intent.preview_id,
            intent.task_id,
            intent.round_id,
            intent.formal_authorization_hash,
            intent.resolved_plan_hash,
            plan.source_family_hash,
            adapter_profile,
            plan.authorized_host_id,
            plan.authorized_resource_id,
            plan.authorization_window_starts_at,
            plan.authorization_window_expires_at,
            plan.budget,
        )
        actual = (
            authority.authority_hash,
            authority.intent_id,
            authority.preview_id,
            authority.task_id,
            authority.round_id,
            authority.formal_authorization_hash,
            authority.resolved_plan_hash,
            authority.candidate_family_hash,
            authority.adapter_profile_id,
            authority.host_id,
            authority.resource_id,
            authority.window_starts_at,
            authority.window_expires_at,
            authority.budget,
        )
        if (
            authority.decision != "authorized"
            or expected != actual
            or now < authority.issued_at
            or not authority.window_starts_at <= now < authority.window_expires_at
            or now >= authority.expires_at
        ):
            raise OperatorFormalStartAuthorityInvalid(
                "B execution Start Authority does not bind this Plan and window"
            )

    def _verify_evaluation_authority(
        self,
        intent: FormalStartIntentView,
        preview: FormalRoundPlanPreviewView,
        authority: FormalEvaluationStartAuthority,
        *,
        now: datetime,
    ) -> None:
        verifier = self.evaluation_verifier
        assert verifier is not None
        self._verify_signed(
            verifier,
            authority.signer,
            authority.authority_hash,
            authority.signature,
            label="evaluation",
        )
        plan = preview.resolved_plan
        if (
            authority.decision != "authorized"
            or authority.authority_hash != intent.evaluation_authority_hash
            or authority.intent_id != intent.intent_id
            or authority.preview_id != intent.preview_id
            or authority.task_id != intent.task_id
            or authority.round_id != intent.round_id
            or authority.formal_authorization_hash != intent.formal_authorization_hash
            or authority.resolved_plan_hash != intent.resolved_plan_hash
            or authority.candidate_family_hash != plan.source_family_hash
            or authority.selection_rule_hash != plan.selection_rule_hash
            or now < authority.issued_at
            or now >= authority.expires_at
        ):
            raise OperatorFormalStartAuthorityInvalid(
                "D evaluation Start Authority does not bind this Plan"
            )

    def _verify_actor(
        self,
        assertion: FormalStartActorAssertion,
        *,
        action: str,
        subject_digest: str,
        now: datetime,
    ) -> None:
        verifier = self.actor_verifier
        if verifier is None:
            raise OperatorFormalStartAuthenticationInvalid(
                "Formal Start actor authentication is not configured"
            )
        if (
            assertion.action != action
            or assertion.subject_digest != subject_digest
            or not assertion.issued_at <= now < assertion.expires_at
        ):
            raise OperatorFormalStartAuthenticationInvalid(
                "Formal Start actor assertion is invalid or expired"
            )
        try:
            self._verify_signed(
                verifier,
                assertion.signer,
                assertion.assertion_hash,
                assertion.signature,
                label="actor",
            )
        except OperatorFormalStartAuthorityInvalid as error:
            raise OperatorFormalStartAuthenticationInvalid(
                "Formal Start actor assertion verification failed closed"
            ) from error

    @staticmethod
    def _verify_signed(
        verifier: DeploymentFormalStartSignatureVerifier,
        signer: FormalStartSignerRef,
        content_hash: str,
        signature: str,
        *,
        label: str,
    ) -> None:
        if verifier.signer_ref != signer:
            raise OperatorFormalStartAuthorityInvalid(
                f"Formal {label} signer identity drifted"
            )
        try:
            valid = verifier.verify_signature(
                content_hash=content_hash,
                signature=signature,
            )
        except Exception as error:
            raise OperatorFormalStartAuthorityInvalid(
                f"Formal {label} signature verifier failed closed"
            ) from error
        if valid is not True:
            raise OperatorFormalStartAuthorityInvalid(
                f"Formal {label} signature was rejected"
            )

    def _require_verifiers(self) -> None:
        if any(
            verifier is None
            for verifier in (
                self.actor_verifier,
                self.execution_verifier,
                self.evaluation_verifier,
            )
        ):
            raise OperatorFormalStartAuthorityInvalid(
                "Formal Start production authentication and authority verifiers are required"
            )

    def _load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView:
        try:
            return FormalRoundPlanPreviewView.model_validate(
                self.object_store.load_preview(preview_id)
            )
        except Exception as error:
            raise OperatorPlanHashMismatch(
                "Formal Start could not reread its deployment-owned Preview"
            ) from error

    def _validate_preview(
        self,
        request: FormalStartIntentRequest,
        preview: FormalRoundPlanPreviewView,
        *,
        now: datetime,
    ) -> None:
        if (
            preview.preview_id != request.preview_id
            or preview.resolved_plan_hash != request.resolved_plan_hash
            or preview.resolved_plan.formal_authorization_hash
            != request.formal_authorization_hash
            or preview.formal_authorization.authorization_hash
            != request.formal_authorization_hash
            or formal_operator_resolved_plan_hash(preview.resolved_plan)
            != request.resolved_plan_hash
        ):
            raise OperatorPlanHashMismatch("Formal Start Preview bindings differ")
        self._validate_complete_preview(preview, now=now)

    def _validate_intent_preview(
        self,
        intent: FormalStartIntentView,
        preview: FormalRoundPlanPreviewView,
        *,
        now: datetime,
    ) -> None:
        if (
            preview.preview_id != intent.preview_id
            or preview.resolved_plan_hash != intent.resolved_plan_hash
            or preview.resolved_plan.formal_authorization_hash
            != intent.formal_authorization_hash
            or preview.formal_authorization.authorization_hash
            != intent.formal_authorization_hash
            or formal_operator_resolved_plan_hash(preview.resolved_plan)
            != intent.resolved_plan_hash
        ):
            raise OperatorPlanHashMismatch("Formal StartIntent Preview bindings differ")
        self._validate_complete_preview(preview, now=now)

    def _validate_complete_preview(
        self,
        preview: FormalRoundPlanPreviewView,
        *,
        now: datetime,
    ) -> None:
        if preview.service_identity != self.service_identity:
            raise OperatorServiceIdentityMismatch(
                "Formal Start Preview belongs to another service deployment"
            )
        if preview.expires_at <= now:
            raise OperatorPreviewExpired("Formal Start Preview has expired")
        plan = preview.resolved_plan
        if (
            plan.synthetic
            or plan.automatic_release_allowed
            or plan.authority is None
            or plan.hotspot is None
            or plan.candidate_family is None
            or plan.source_family_hash is None
            or not plan.candidates
            or plan.candidate_input_set_hash is None
        ):
            raise OperatorPlanHashMismatch(
                "Formal Start requires a complete non-synthetic resolved Plan"
            )

    def _build_intent(
        self,
        request: FormalStartIntentRequest,
        preview: FormalRoundPlanPreviewView,
        *,
        now: datetime,
    ) -> FormalStartIntentView:
        intent_id, task_id, round_id = derive_formal_start_ids(
            request.idempotency_key
        )
        bindings = tuple(
            FormalStartCandidateBinding(
                ordinal=candidate.ordinal,
                candidate_id=candidate.candidate_id,
                round_candidate_id=uuid5(
                    round_id,
                    f"{candidate.ordinal}:{candidate.candidate_id}:"
                    f"{formal_start_content_hash(candidate)}",
                ),
                candidate_input_digest=formal_start_content_hash(candidate),
            )
            for candidate in preview.resolved_plan.candidates
        )
        return FormalStartIntentView(
            intent_id=intent_id,
            preview_id=request.preview_id,
            resolved_plan_hash=request.resolved_plan_hash,
            formal_authorization_hash=request.formal_authorization_hash,
            execution_authority_hash=request.execution_authority_hash,
            evaluation_authority_hash=request.evaluation_authority_hash,
            request_digest=formal_start_request_subject_digest(request),
            actor_id=request.actor_assertion.actor_id,
            actor_assertion_hash=request.actor_assertion.assertion_hash,
            actor_signer_id=request.actor_assertion.signer.signer_id,
            actor_signer_hash=request.actor_assertion.signer.signer_hash,
            idempotency_key=request.idempotency_key,
            task_id=task_id,
            round_id=round_id,
            candidate_bindings=bindings,
            state="awaiting_authority",
            blocker_codes=(
                "formal_evaluation_start_authority_unavailable",
                "formal_execution_start_authority_unavailable",
            ),
            service_identity=self.service_identity,
            authority_reconcile_count=0,
            version=1,
            created_at=now,
            updated_at=now,
            authority_ready=False,
        )

    def _validate_existing(
        self,
        request: FormalStartIntentRequest,
        intent: FormalStartIntentView,
    ) -> None:
        expected = (
            request.preview_id,
            request.resolved_plan_hash,
            request.formal_authorization_hash,
            request.execution_authority_hash,
            request.evaluation_authority_hash,
            formal_start_request_subject_digest(request),
            request.actor_assertion.actor_id,
            request.actor_assertion.assertion_hash,
            request.actor_assertion.signer.signer_id,
            request.actor_assertion.signer.signer_hash,
            request.idempotency_key,
        )
        actual = (
            intent.preview_id,
            intent.resolved_plan_hash,
            intent.formal_authorization_hash,
            intent.execution_authority_hash,
            intent.evaluation_authority_hash,
            intent.request_digest,
            intent.actor_id,
            intent.actor_assertion_hash,
            intent.actor_signer_id,
            intent.actor_signer_hash,
            intent.idempotency_key,
        )
        if expected != actual:
            raise OperatorPlanHashMismatch(
                "Formal Start idempotency key was reused with different inputs"
            )
        self._require_local_intent(intent)

    def _require_local_intent(self, intent: FormalStartIntentView) -> None:
        if intent.service_identity != self.service_identity:
            raise OperatorServiceIdentityMismatch(
                "Formal StartIntent belongs to another service deployment"
            )

    @staticmethod
    def _require_intent_actor(
        intent: FormalStartIntentView,
        assertion: FormalStartActorAssertion,
    ) -> None:
        if (
            assertion.actor_id != intent.actor_id
            or assertion.signer.signer_id != intent.actor_signer_id
            or assertion.signer.signer_hash != intent.actor_signer_hash
        ):
            raise OperatorFormalStartAuthenticationInvalid(
                "Formal Start action actor does not own this Intent"
            )

    @staticmethod
    def _require_authority_role_separation(
        intent: FormalStartIntentView,
        execution: FormalExecutionStartAuthority,
        evaluation: FormalEvaluationStartAuthority,
        preview: FormalRoundPlanPreviewView,
    ) -> None:
        ids = {
            intent.actor_signer_id,
            execution.signer.signer_id,
            evaluation.signer.signer_id,
            evaluation.holdout_plan_authority_id,
            evaluation.independent_verifier.verifier_id,
            preview.formal_authorization.verifier.verifier_id,
        }
        hashes = {
            intent.actor_signer_hash,
            execution.signer.signer_hash,
            evaluation.signer.signer_hash,
            evaluation.holdout_plan_authority_hash,
            evaluation.independent_verifier.identity_hash,
            preview.formal_authorization.verifier.verifier_hash,
        }
        if len(ids) != 6 or len(hashes) != 6:
            raise OperatorFormalStartAuthorityInvalid(
                "Formal Start actor, owner, B, Holdout, D signer, and verifier must be separate"
            )

    @staticmethod
    def _result(
        intent: FormalStartIntentView,
        *,
        replayed: bool,
    ) -> FormalStartIntentResult:
        return FormalStartIntentResult.model_validate(
            {**intent.model_dump(mode="json"), "replayed": replayed}
        )

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Formal Start clock must be timezone-aware")
        return now


__all__ = [
    "DeploymentFormalStartSignatureVerifier",
    "FormalStartCoordinator",
    "FormalStartObjectStore",
    "FormalStartRepository",
]
