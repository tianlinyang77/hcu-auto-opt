# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileWindowAuthorization,
    FormalProfileWindowAuthorizationContent,
    formal_profile_window_authorization_hash,
)
from hcuopt.contracts.m2_formal_execution_v1 import M2FormalExecutionAdapterProfile
from hcuopt.contracts.m2_formal_operator_v1 import FormalRoundPlanPreviewView
from hcuopt.contracts.m2_formal_start_v1 import (
    FormalExecutionStartAuthority,
    FormalExecutionStartAuthorityContent,
    FormalStartSignerRef,
    derive_formal_start_ids,
    formal_start_content_hash,
    publish_formal_execution_start_authority,
)
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.operator.formal_plans import formal_operator_resolved_plan_hash
from hcuopt.operator.formal_profiles import DeploymentFormalProfileGrantVerifier


class FrozenFormalExecutionRegistrationModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class M2FormalExecutionProfileRegistration(FrozenFormalExecutionRegistrationModel):
    """Deployment-only proof that one implementation is registered for one window."""

    schema_version: Literal["m2a-formal-execution-profile-registration-v1"] = (
        "m2a-formal-execution-profile-registration-v1"
    )
    registration_id: UUID
    profile: M2FormalExecutionAdapterProfile
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    host_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    resource_id: str = Field(pattern=r"^hcu-(0|[1-9][0-9]*)$")
    window_starts_at: datetime
    window_expires_at: datetime
    lease_policy_hash: str = Field(pattern=SHA256_PATTERN)
    fencing_policy_hash: str = Field(pattern=SHA256_PATTERN)
    cleanup_policy_hash: str = Field(pattern=SHA256_PATTERN)
    registered_at: datetime
    expires_at: datetime
    registration_state: Literal["registered_for_authorized_window"] = (
        "registered_for_authorized_window"
    )
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_active_window_registration(
        self,
    ) -> M2FormalExecutionProfileRegistration:
        values = (
            self.window_starts_at,
            self.window_expires_at,
            self.registered_at,
            self.expires_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            raise ValueError("Formal execution registration times must be timezone-aware")
        if not (
            self.window_starts_at <= self.registered_at < self.window_expires_at <= self.expires_at
        ):
            raise ValueError("Formal execution registration window is invalid")
        if self.profile.registration_state != "implementation_ready_unregistered":
            raise ValueError("Formal execution registration requires a reviewed implementation")
        return self


class M2FormalExecutionPreviewStore(Protocol):
    def load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView: ...


class M2FormalExecutionProfileRegistry(Protocol):
    def load_registration(
        self,
        *,
        adapter_profile_id: str,
        formal_authorization_hash: str,
    ) -> M2FormalExecutionProfileRegistration: ...


class DeploymentFormalExecutionStartSigner(Protocol):
    @property
    def signer_ref(self) -> FormalStartSignerRef: ...

    def sign_authority(self, *, content_hash: str) -> str: ...


class M2FormalExecutionStartAuthorityError(MeasurementSafetyError):
    """B-owned Authority could not be issued from exact deployment inputs."""


class M2FormalExecutionStartAuthorityIssuer:
    """Issue one signed B Authority without creating a Round or touching HCU."""

    def __init__(
        self,
        *,
        preview_store: M2FormalExecutionPreviewStore,
        profile_registry: M2FormalExecutionProfileRegistry,
        authorization_verifier: DeploymentFormalProfileGrantVerifier,
        signer: DeploymentFormalExecutionStartSigner,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.preview_store = preview_store
        self.profile_registry = profile_registry
        self.authorization_verifier = authorization_verifier
        self.signer = signer
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(
        self,
        *,
        preview_id: UUID,
        idempotency_key: str,
    ) -> FormalExecutionStartAuthority:
        now = self._now()
        preview = self._load_preview(preview_id)
        self._verify_preview(preview, now=now)
        authorization = preview.formal_authorization
        self._verify_authorization(authorization, now=now)
        plan = preview.resolved_plan
        assert plan.authority is not None and plan.source_family_hash is not None
        registration = self._load_registration(
            adapter_profile_id=plan.authority.adapter_profile,
            authorization_hash=authorization.authorization_hash,
        )
        self._verify_registration(registration, preview, now=now)
        if (
            self.signer.signer_ref.signer_id == authorization.verifier.verifier_id
            or self.signer.signer_ref.signer_hash == authorization.verifier.verifier_hash
        ):
            raise M2FormalExecutionStartAuthorityError(
                "Formal owner verifier and B execution signer must be separate"
            )

        intent_id, task_id, round_id = derive_formal_start_ids(idempotency_key)
        content = FormalExecutionStartAuthorityContent(
            authority_id=uuid5(intent_id, "b-formal-execution-start-authority-v2"),
            intent_id=intent_id,
            preview_id=preview.preview_id,
            task_id=task_id,
            round_id=round_id,
            decision="authorized",
            formal_authorization_hash=authorization.authorization_hash,
            resolved_plan_hash=preview.resolved_plan_hash,
            candidate_family_hash=plan.source_family_hash,
            adapter_profile_id=registration.profile.profile_id,
            adapter_profile_version=registration.profile.profile_version,
            adapter_profile_hash=registration.profile.profile_hash,
            host_id=registration.host_id,
            resource_id=registration.resource_id,
            window_starts_at=registration.window_starts_at,
            window_expires_at=registration.window_expires_at,
            budget=plan.budget,
            lease_policy_hash=registration.lease_policy_hash,
            fencing_policy_hash=registration.fencing_policy_hash,
            cleanup_policy_hash=registration.cleanup_policy_hash,
            issued_at=now,
            expires_at=min(registration.expires_at, authorization.window_expires_at),
            signer=self.signer.signer_ref,
        )
        content_hash = formal_start_content_hash(content)
        try:
            signature = self.signer.sign_authority(content_hash=content_hash)
            return publish_formal_execution_start_authority(
                content,
                signature=signature,
            )
        except Exception as error:
            raise M2FormalExecutionStartAuthorityError(
                "Formal B execution Authority signer failed closed"
            ) from error

    def _load_preview(self, preview_id: UUID) -> FormalRoundPlanPreviewView:
        try:
            return FormalRoundPlanPreviewView.model_validate(
                self.preview_store.load_preview(preview_id)
            )
        except Exception as error:
            raise M2FormalExecutionStartAuthorityError(
                "Formal B issuer could not reread the exact Preview"
            ) from error

    def _load_registration(
        self,
        *,
        adapter_profile_id: str,
        authorization_hash: str,
    ) -> M2FormalExecutionProfileRegistration:
        try:
            return M2FormalExecutionProfileRegistration.model_validate(
                self.profile_registry.load_registration(
                    adapter_profile_id=adapter_profile_id,
                    formal_authorization_hash=authorization_hash,
                )
            )
        except Exception as error:
            raise M2FormalExecutionStartAuthorityError(
                "Formal execution Adapter is not registered for this window"
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
            raise M2FormalExecutionStartAuthorityError(
                "Formal owner window Authorization is invalid or inactive"
            )
        try:
            valid = self.authorization_verifier.verify_signature(
                authorization_hash=authorization.authorization_hash,
                signature=authorization.signature,
            )
        except Exception as error:
            raise M2FormalExecutionStartAuthorityError(
                "Formal owner Authorization verifier failed closed"
            ) from error
        if valid is not True:
            raise M2FormalExecutionStartAuthorityError(
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
            preview.expires_at <= now
            or blocking
            or formal_operator_resolved_plan_hash(plan) != preview.resolved_plan_hash
            or plan.authority is None
            or plan.candidate_family is None
            or plan.source_family_hash is None
            or not plan.candidates
            or plan.synthetic
            or plan.automatic_release_allowed
        ):
            raise M2FormalExecutionStartAuthorityError(
                "Formal B issuer requires one complete immutable non-Synthetic Preview"
            )

    @staticmethod
    def _verify_registration(
        registration: M2FormalExecutionProfileRegistration,
        preview: FormalRoundPlanPreviewView,
        *,
        now: datetime,
    ) -> None:
        authorization = preview.formal_authorization
        plan = preview.resolved_plan
        adapter_profile_id = plan.authority.adapter_profile if plan.authority else None
        expected = (
            authorization.authorization_hash,
            adapter_profile_id,
            authorization.host_id,
            authorization.resource_id,
            authorization.window_starts_at,
            authorization.window_expires_at,
        )
        actual = (
            registration.formal_authorization_hash,
            registration.profile.profile_id,
            registration.host_id,
            registration.resource_id,
            registration.window_starts_at,
            registration.window_expires_at,
        )
        if (
            expected != actual
            or registration.registered_at > now
            or now >= registration.expires_at
            or registration.profile.synthetic
            or registration.profile.automatic_release_allowed
        ):
            raise M2FormalExecutionStartAuthorityError(
                "Formal execution Adapter registration drifted from the authorized Plan"
            )

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise M2FormalExecutionStartAuthorityError(
                "Formal B execution Authority clock must be timezone-aware"
            )
        return now


__all__ = [
    "DeploymentFormalExecutionStartSigner",
    "M2FormalExecutionPreviewStore",
    "M2FormalExecutionProfileRegistration",
    "M2FormalExecutionProfileRegistry",
    "M2FormalExecutionStartAuthorityError",
    "M2FormalExecutionStartAuthorityIssuer",
]
