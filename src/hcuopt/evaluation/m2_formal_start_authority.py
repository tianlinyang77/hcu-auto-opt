# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceVerifierIdentity,
    ProductionEvidenceRootDescriptor,
)
from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileWindowAuthorization,
    FormalProfileWindowAuthorizationContent,
    formal_profile_window_authorization_hash,
)
from hcuopt.contracts.m2_formal_operator_v1 import FormalRoundPlanPreviewView
from hcuopt.contracts.m2_formal_start_v1 import (
    FormalEvaluationStartAuthority,
    FormalEvaluationStartAuthorityContent,
    FormalStartSignerRef,
    derive_formal_start_ids,
    formal_start_content_hash,
    publish_formal_evaluation_start_authority,
)
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.operator.formal_plans import formal_operator_resolved_plan_hash
from hcuopt.operator.formal_profiles import DeploymentFormalProfileGrantVerifier


class FrozenFormalEvaluationRegistrationModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class M2FormalEvaluationStartRegistration(FrozenFormalEvaluationRegistrationModel):
    """Deployment proof for one sealed D evaluation plan and evidence boundary."""

    schema_version: Literal["m2a-formal-evaluation-start-registration-v1"] = (
        "m2a-formal-evaluation-start-registration-v1"
    )
    registration_id: UUID
    preview_id: UUID
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    search_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    search_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_commitment: str = Field(pattern=SHA256_PATTERN)
    holdout_commitment_scheme: Literal["sha256-nonce-v1"] = "sha256-nonce-v1"
    holdout_plan_authority_id: str = Field(min_length=1, max_length=300)
    holdout_plan_authority_hash: str = Field(pattern=SHA256_PATTERN)
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    family_alpha: float = Field(gt=0.0, lt=1.0)
    evidence_root: ProductionEvidenceRootDescriptor
    independent_verifier: FormalEvidenceVerifierIdentity
    host_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    resource_id: str = Field(pattern=r"^hcu-(0|[1-9][0-9]*)$")
    window_starts_at: datetime
    window_expires_at: datetime
    registered_at: datetime
    expires_at: datetime
    registration_state: Literal["registered_for_authorized_window"] = (
        "registered_for_authorized_window"
    )
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_sealed_window_registration(
        self,
    ) -> M2FormalEvaluationStartRegistration:
        timestamps = (
            self.window_starts_at,
            self.window_expires_at,
            self.registered_at,
            self.expires_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("Formal evaluation registration times must be timezone-aware")
        if not (
            self.window_starts_at <= self.registered_at < self.window_expires_at <= self.expires_at
        ):
            raise ValueError("Formal evaluation registration window is invalid")
        if (
            self.holdout_plan_authority_id == self.independent_verifier.verifier_id
            or self.holdout_plan_authority_hash == self.independent_verifier.identity_hash
        ):
            raise ValueError(
                "Formal Holdout Plan Authority and independent verifier must be separate"
            )
        return self


class M2FormalEvaluationPreviewStore(Protocol):
    def load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView: ...


class M2FormalEvaluationStartRegistry(Protocol):
    def load_registration(
        self,
        *,
        preview_id: UUID,
        formal_authorization_hash: str,
        resolved_plan_hash: str,
    ) -> M2FormalEvaluationStartRegistration: ...


class DeploymentFormalEvaluationStartSigner(Protocol):
    @property
    def signer_ref(self) -> FormalStartSignerRef: ...

    def sign_authority(self, *, content_hash: str) -> str: ...


class M2FormalEvaluationStartAuthorityError(RuntimeError):
    """D-owned Start Authority could not be issued from exact deployment inputs."""


class M2FormalEvaluationStartAuthorityIssuer:
    """Issue one signed D Authority without revealing Holdout or touching HCU."""

    def __init__(
        self,
        *,
        preview_store: M2FormalEvaluationPreviewStore,
        evaluation_registry: M2FormalEvaluationStartRegistry,
        authorization_verifier: DeploymentFormalProfileGrantVerifier,
        signer: DeploymentFormalEvaluationStartSigner,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.preview_store = preview_store
        self.evaluation_registry = evaluation_registry
        self.authorization_verifier = authorization_verifier
        self.signer = signer
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(
        self,
        *,
        preview_id: UUID,
        idempotency_key: str,
    ) -> FormalEvaluationStartAuthority:
        now = self._now()
        preview = self._load_preview(preview_id)
        self._verify_preview(preview, now=now)
        authorization = preview.formal_authorization
        self._verify_authorization(authorization, now=now)
        plan = preview.resolved_plan
        assert plan.source_family_hash is not None
        registration = self._load_registration(preview)
        self._verify_registration(registration, preview, now=now)
        self._require_role_separation(registration, authorization)

        intent_id, task_id, round_id = derive_formal_start_ids(idempotency_key)
        content = FormalEvaluationStartAuthorityContent(
            authority_id=uuid5(intent_id, "d-formal-evaluation-start-authority-v1"),
            intent_id=intent_id,
            preview_id=preview.preview_id,
            task_id=task_id,
            round_id=round_id,
            decision="authorized",
            formal_authorization_hash=authorization.authorization_hash,
            resolved_plan_hash=preview.resolved_plan_hash,
            candidate_family_hash=plan.source_family_hash,
            search_plan_hash=registration.search_plan_hash,
            holdout_plan_commitment=registration.holdout_plan_commitment,
            holdout_commitment_scheme=registration.holdout_commitment_scheme,
            holdout_plan_authority_id=registration.holdout_plan_authority_id,
            holdout_plan_authority_hash=registration.holdout_plan_authority_hash,
            selection_rule_hash=registration.selection_rule_hash,
            family_alpha=registration.family_alpha,
            evidence_root=registration.evidence_root,
            independent_verifier=registration.independent_verifier,
            issued_at=now,
            expires_at=min(
                preview.expires_at,
                registration.expires_at,
                authorization.window_expires_at,
            ),
            signer=self.signer.signer_ref,
        )
        content_hash = formal_start_content_hash(content)
        try:
            signature = self.signer.sign_authority(content_hash=content_hash)
            return publish_formal_evaluation_start_authority(content, signature=signature)
        except Exception as error:
            raise M2FormalEvaluationStartAuthorityError(
                "Formal D evaluation Authority signer failed closed"
            ) from error

    def _load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView:
        try:
            return FormalRoundPlanPreviewView.model_validate(
                self.preview_store.load_preview(preview_id)
            )
        except Exception as error:
            raise M2FormalEvaluationStartAuthorityError(
                "Formal D issuer could not reread the exact Preview"
            ) from error

    def _load_registration(
        self,
        preview: FormalRoundPlanPreviewView,
    ) -> M2FormalEvaluationStartRegistration:
        try:
            return M2FormalEvaluationStartRegistration.model_validate(
                self.evaluation_registry.load_registration(
                    preview_id=preview.preview_id,
                    formal_authorization_hash=(preview.formal_authorization.authorization_hash),
                    resolved_plan_hash=preview.resolved_plan_hash,
                )
            )
        except Exception as error:
            raise M2FormalEvaluationStartAuthorityError(
                "Formal evaluation plan is not registered for this Preview"
            ) from error

    def _verify_authorization(
        self,
        authorization: FormalProfileWindowAuthorization,
        *,
        now: datetime,
    ) -> None:
        content = FormalProfileWindowAuthorizationContent.model_validate(
            authorization.model_dump(
                mode="json",
                exclude={"authorization_hash", "signature"},
            )
        )
        if (
            formal_profile_window_authorization_hash(content) != authorization.authorization_hash
            or authorization.decision != "authorized"
            or not authorization.window_starts_at <= now < authorization.window_expires_at
            or self.authorization_verifier.verifier_ref != authorization.verifier
        ):
            raise M2FormalEvaluationStartAuthorityError(
                "Formal owner window Authorization is invalid or inactive"
            )
        try:
            valid = self.authorization_verifier.verify_signature(
                authorization_hash=authorization.authorization_hash,
                signature=authorization.signature,
            )
        except Exception as error:
            raise M2FormalEvaluationStartAuthorityError(
                "Formal owner Authorization verifier failed closed"
            ) from error
        if valid is not True:
            raise M2FormalEvaluationStartAuthorityError(
                "Formal owner Authorization signature was rejected"
            )

    @staticmethod
    def _verify_preview(
        preview: FormalRoundPlanPreviewView,
        *,
        now: datetime,
    ) -> None:
        plan = preview.resolved_plan
        blocking = {
            item.code
            for item in preview.checks
            if item.status == "block" and item.code != "formal_start_authority_not_bound"
        }
        if (
            preview.created_at > now
            or preview.expires_at <= now
            or blocking
            or formal_operator_resolved_plan_hash(plan) != preview.resolved_plan_hash
            or plan.authority is None
            or plan.candidate_family is None
            or plan.source_family_hash is None
            or not plan.candidates
            or plan.synthetic
            or plan.automatic_release_allowed
        ):
            raise M2FormalEvaluationStartAuthorityError(
                "Formal D issuer requires one complete immutable non-Synthetic Preview"
            )

    def _require_role_separation(
        self,
        registration: M2FormalEvaluationStartRegistration,
        authorization: FormalProfileWindowAuthorization,
    ) -> None:
        ids = {
            self.signer.signer_ref.signer_id,
            registration.holdout_plan_authority_id,
            registration.independent_verifier.verifier_id,
            authorization.verifier.verifier_id,
        }
        hashes = {
            self.signer.signer_ref.signer_hash,
            registration.holdout_plan_authority_hash,
            registration.independent_verifier.identity_hash,
            authorization.verifier.verifier_hash,
        }
        if len(ids) != 4 or len(hashes) != 4:
            raise M2FormalEvaluationStartAuthorityError(
                "Formal owner, Holdout, D signer, and verifier must be separate"
            )

    @staticmethod
    def _verify_registration(
        registration: M2FormalEvaluationStartRegistration,
        preview: FormalRoundPlanPreviewView,
        *,
        now: datetime,
    ) -> None:
        authorization = preview.formal_authorization
        plan = preview.resolved_plan
        expected = (
            preview.preview_id,
            authorization.authorization_hash,
            preview.resolved_plan_hash,
            plan.source_family_hash,
            plan.search_protocol_hash,
            plan.holdout_protocol_hash,
            plan.selection_rule_hash,
            authorization.host_id,
            authorization.resource_id,
            authorization.window_starts_at,
            authorization.window_expires_at,
        )
        actual = (
            registration.preview_id,
            registration.formal_authorization_hash,
            registration.resolved_plan_hash,
            registration.candidate_family_hash,
            registration.search_protocol_hash,
            registration.holdout_protocol_hash,
            registration.selection_rule_hash,
            registration.host_id,
            registration.resource_id,
            registration.window_starts_at,
            registration.window_expires_at,
        )
        if expected != actual or registration.registered_at > now or now >= registration.expires_at:
            raise M2FormalEvaluationStartAuthorityError(
                "Formal evaluation registration drifted from the authorized Plan"
            )

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise M2FormalEvaluationStartAuthorityError(
                "Formal D evaluation Authority clock must be timezone-aware"
            )
        return now


__all__ = [
    "DeploymentFormalEvaluationStartSigner",
    "M2FormalEvaluationPreviewStore",
    "M2FormalEvaluationStartAuthorityError",
    "M2FormalEvaluationStartAuthorityIssuer",
    "M2FormalEvaluationStartRegistration",
    "M2FormalEvaluationStartRegistry",
]
