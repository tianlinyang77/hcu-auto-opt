# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, TypeAdapter, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceVerifierIdentity,
    ProductionEvidenceRootDescriptor,
)
from hcuopt.contracts.m2 import RoundBudget
from hcuopt.contracts.operator_v1 import OperatorServiceIdentity, OperatorServiceIdentityAssertion
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.measurement.evidence import canonical_json_bytes

M2A_FORMAL_START_SCHEMA_VERSION = "m2a-formal-start-intent-v1"


class FrozenFormalStartModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FormalStartSignerRef(FrozenFormalStartModel):
    signer_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    signer_version: str = Field(min_length=1, max_length=200)
    signer_hash: str = Field(pattern=SHA256_PATTERN)
    signature_scheme: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    key_id: str = Field(min_length=1, max_length=300)


class FormalStartActorAssertionContent(FrozenFormalStartModel):
    schema_version: Literal["m2a-formal-start-actor-assertion-v1"] = (
        "m2a-formal-start-actor-assertion-v1"
    )
    assertion_id: UUID
    actor_id: str = Field(min_length=1, max_length=300)
    action: Literal["create", "reconcile", "cancel"]
    subject_digest: str = Field(pattern=SHA256_PATTERN)
    issued_at: datetime
    expires_at: datetime
    signer: FormalStartSignerRef

    @model_validator(mode="after")
    def require_bounded_assertion(self) -> FormalStartActorAssertionContent:
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (self.issued_at, self.expires_at)
        ):
            raise ValueError("Formal Start actor assertion times must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("Formal Start actor assertion must have positive duration")
        return self


class FormalStartActorAssertion(FormalStartActorAssertionContent):
    assertion_hash: str = Field(pattern=SHA256_PATTERN)
    signature: str = Field(min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def verify_assertion_hash(self) -> FormalStartActorAssertion:
        content = FormalStartActorAssertionContent.model_validate(
            self.model_dump(mode="json", exclude={"assertion_hash", "signature"})
        )
        if formal_start_content_hash(content) != self.assertion_hash:
            raise ValueError("Formal Start actor assertion content does not match its Hash")
        return self


class FormalExecutionStartAuthorityContent(FrozenFormalStartModel):
    """B-owned permission to use one registered execution profile in one window."""

    schema_version: Literal["m2a-formal-execution-start-authority-v2"] = (
        "m2a-formal-execution-start-authority-v2"
    )
    authority_id: UUID
    intent_id: UUID
    preview_id: UUID
    task_id: UUID
    round_id: UUID
    decision: Literal["authorized", "rejected"]
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    adapter_profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    adapter_profile_version: str = Field(min_length=1, max_length=100)
    adapter_profile_hash: str = Field(pattern=SHA256_PATTERN)
    registration_state: Literal["registered_for_authorized_window"] = (
        "registered_for_authorized_window"
    )
    host_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    resource_id: str = Field(pattern=r"^hcu-(0|[1-9][0-9]*)$")
    window_starts_at: datetime
    window_expires_at: datetime
    budget: RoundBudget
    lease_policy_hash: str = Field(pattern=SHA256_PATTERN)
    fencing_policy_hash: str = Field(pattern=SHA256_PATTERN)
    cleanup_policy_hash: str = Field(pattern=SHA256_PATTERN)
    issued_at: datetime
    expires_at: datetime
    signer: FormalStartSignerRef
    run_mode: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_bounded_execution_authority(self) -> FormalExecutionStartAuthorityContent:
        values = (
            self.window_starts_at,
            self.window_expires_at,
            self.issued_at,
            self.expires_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            raise ValueError("Formal execution Start Authority times must be timezone-aware")
        if not (
            self.window_starts_at <= self.issued_at < self.window_expires_at <= self.expires_at
        ):
            raise ValueError("Formal execution Start Authority window is invalid")
        return self


class FormalExecutionStartAuthority(FormalExecutionStartAuthorityContent):
    authority_hash: str = Field(pattern=SHA256_PATTERN)
    signature: str = Field(min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def verify_authority_hash(self) -> FormalExecutionStartAuthority:
        content = FormalExecutionStartAuthorityContent.model_validate(
            self.model_dump(mode="json", exclude={"authority_hash", "signature"})
        )
        if formal_start_content_hash(content) != self.authority_hash:
            raise ValueError("Formal execution Start Authority content does not match its Hash")
        return self


class FormalEvaluationStartAuthorityContent(FrozenFormalStartModel):
    """D-owned sealed Search/Holdout and evidence authority required before a Round."""

    schema_version: Literal["m2a-formal-evaluation-start-authority-v1"] = (
        "m2a-formal-evaluation-start-authority-v1"
    )
    authority_id: UUID
    intent_id: UUID
    preview_id: UUID
    task_id: UUID
    round_id: UUID
    decision: Literal["authorized", "rejected"]
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    search_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_commitment: str = Field(pattern=SHA256_PATTERN)
    holdout_commitment_scheme: Literal["sha256-nonce-v1"] = "sha256-nonce-v1"
    holdout_plan_authority_id: str = Field(min_length=1, max_length=300)
    holdout_plan_authority_hash: str = Field(pattern=SHA256_PATTERN)
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    family_alpha: float = Field(gt=0.0, lt=1.0)
    evidence_root: ProductionEvidenceRootDescriptor
    independent_verifier: FormalEvidenceVerifierIdentity
    issued_at: datetime
    expires_at: datetime
    signer: FormalStartSignerRef
    run_mode: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_bounded_evaluation_authority(self) -> FormalEvaluationStartAuthorityContent:
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (self.issued_at, self.expires_at)
        ):
            raise ValueError("Formal evaluation Start Authority times must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("Formal evaluation Start Authority must have positive duration")
        identities = {
            self.holdout_plan_authority_id,
            self.independent_verifier.verifier_id,
            self.signer.signer_id,
        }
        hashes = {
            self.holdout_plan_authority_hash,
            self.independent_verifier.identity_hash,
            self.signer.signer_hash,
        }
        if len(identities) != 3 or len(hashes) != 3:
            raise ValueError("Formal Holdout, verifier, and D signer identities must be separate")
        return self


class FormalEvaluationStartAuthority(FormalEvaluationStartAuthorityContent):
    authority_hash: str = Field(pattern=SHA256_PATTERN)
    signature: str = Field(min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def verify_authority_hash(self) -> FormalEvaluationStartAuthority:
        content = FormalEvaluationStartAuthorityContent.model_validate(
            self.model_dump(mode="json", exclude={"authority_hash", "signature"})
        )
        if formal_start_content_hash(content) != self.authority_hash:
            raise ValueError("Formal evaluation Start Authority content does not match its Hash")
        return self


class FormalStartIntentRequest(FrozenFormalStartModel):
    preview_id: UUID
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    execution_authority_hash: str = Field(pattern=SHA256_PATTERN)
    evaluation_authority_hash: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    expected_service_identity: OperatorServiceIdentityAssertion
    actor_assertion: FormalStartActorAssertion


class FormalStartIntentActionRequest(FrozenFormalStartModel):
    intent_id: UUID
    action: Literal["reconcile", "cancel"]
    actor_assertion: FormalStartActorAssertion

    @model_validator(mode="after")
    def require_matching_action(self) -> FormalStartIntentActionRequest:
        if self.actor_assertion.action != self.action:
            raise ValueError("Formal Start action and actor assertion differ")
        return self


class FormalStartCandidateBinding(FrozenFormalStartModel):
    ordinal: int = Field(ge=0, le=3)
    candidate_id: UUID
    round_candidate_id: UUID
    candidate_input_digest: str = Field(pattern=SHA256_PATTERN)
    candidate_kind: Literal["business"] = "business"


class FormalStartIntentView(FrozenFormalStartModel):
    schema_version: Literal["m2a-formal-start-intent-v1"] = (
        M2A_FORMAL_START_SCHEMA_VERSION
    )
    intent_id: UUID
    preview_id: UUID
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    execution_authority_hash: str = Field(pattern=SHA256_PATTERN)
    evaluation_authority_hash: str = Field(pattern=SHA256_PATTERN)
    request_digest: str = Field(pattern=SHA256_PATTERN)
    actor_id: str = Field(min_length=1, max_length=300)
    actor_assertion_hash: str = Field(pattern=SHA256_PATTERN)
    actor_signer_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    actor_signer_hash: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    task_id: UUID
    round_id: UUID
    candidate_bindings: tuple[FormalStartCandidateBinding, ...] = Field(
        min_length=2, max_length=4
    )
    state: Literal[
        "awaiting_authority",
        "ready_for_round_creation",
        "cancelled",
        "failed",
    ]
    blocker_codes: tuple[str, ...] = ()
    error_code: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9_]{2,99}$"
    )
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)
    service_identity: OperatorServiceIdentity
    authority_reconcile_count: int = Field(ge=0)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    last_reconciled_at: datetime | None = None
    ready_at: datetime | None = None
    cancelled_at: datetime | None = None
    authority_ready: bool
    round_creation_allowed: Literal[False] = False
    hcu_accessed: Literal[False] = False
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_consistent_state(self) -> FormalStartIntentView:
        ordinals = tuple(item.ordinal for item in self.candidate_bindings)
        if ordinals != tuple(range(len(self.candidate_bindings))):
            raise ValueError("Formal Start Candidate bindings must be canonically ordered")
        if self.blocker_codes != tuple(sorted(set(self.blocker_codes))):
            raise ValueError("Formal Start blocker codes must be unique and sorted")
        if self.authority_ready != (self.state == "ready_for_round_creation"):
            raise ValueError("Formal Start authority_ready must match its state")
        if self.state == "awaiting_authority" and not self.blocker_codes:
            raise ValueError("awaiting Formal StartIntent requires blocker codes")
        if self.state != "awaiting_authority" and self.blocker_codes:
            raise ValueError("only an awaiting Formal StartIntent may carry blockers")
        if self.state == "ready_for_round_creation" and (
            self.blocker_codes or self.ready_at is None
        ):
            raise ValueError("ready Formal StartIntent requires a ready time and no blockers")
        if self.state != "ready_for_round_creation" and self.ready_at is not None:
            raise ValueError("only a ready Formal StartIntent may carry ready_at")
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("Formal Start error code and message must be written together")
        if (self.state == "failed") != (self.error_code is not None):
            raise ValueError("failed Formal StartIntent requires exactly one safe error")
        if (self.state == "cancelled") != (self.cancelled_at is not None):
            raise ValueError("cancelled Formal StartIntent requires exactly one cancel time")
        timestamps = tuple(
            value
            for value in (
                self.created_at,
                self.updated_at,
                self.last_reconciled_at,
                self.ready_at,
                self.cancelled_at,
            )
            if value is not None
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("Formal StartIntent timestamps must be timezone-aware")
        if self.updated_at < self.created_at or any(
            value < self.created_at or value > self.updated_at
            for value in timestamps
            if value not in {self.created_at, self.updated_at}
        ):
            raise ValueError("Formal StartIntent timestamps must follow persisted order")
        return self


class FormalStartIntentResult(FormalStartIntentView):
    replayed: bool


def formal_start_content_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    else:
        value = TypeAdapter(Any).dump_python(value, mode="json")
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def publish_formal_start_actor_assertion(
    content: FormalStartActorAssertionContent,
    *,
    signature: str,
) -> FormalStartActorAssertion:
    return FormalStartActorAssertion.model_validate(
        {
            **content.model_dump(mode="json"),
            "assertion_hash": formal_start_content_hash(content),
            "signature": signature,
        }
    )


def publish_formal_execution_start_authority(
    content: FormalExecutionStartAuthorityContent,
    *,
    signature: str,
) -> FormalExecutionStartAuthority:
    return FormalExecutionStartAuthority.model_validate(
        {
            **content.model_dump(mode="json"),
            "authority_hash": formal_start_content_hash(content),
            "signature": signature,
        }
    )


def publish_formal_evaluation_start_authority(
    content: FormalEvaluationStartAuthorityContent,
    *,
    signature: str,
) -> FormalEvaluationStartAuthority:
    return FormalEvaluationStartAuthority.model_validate(
        {
            **content.model_dump(mode="json"),
            "authority_hash": formal_start_content_hash(content),
            "signature": signature,
        }
    )


def formal_start_request_subject_digest(request: FormalStartIntentRequest) -> str:
    return formal_start_content_hash(
        request.model_dump(mode="json", exclude={"actor_assertion"})
    )


def formal_start_action_subject_digest(*, action: str, intent_id: UUID) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json_bytes({"action": action, "intent_id": str(intent_id)})
    ).hexdigest()


def derive_formal_start_ids(idempotency_key: str) -> tuple[UUID, UUID, UUID]:
    """Return the one Intent, Task, and Round identity authorized by a start key."""

    intent_id = uuid5(
        NAMESPACE_URL,
        f"hcuopt:m2a-formal-start:{idempotency_key}",
    )
    return intent_id, uuid5(intent_id, "task"), uuid5(intent_id, "round")


__all__ = [
    "FormalEvaluationStartAuthority",
    "FormalEvaluationStartAuthorityContent",
    "FormalExecutionStartAuthority",
    "FormalExecutionStartAuthorityContent",
    "FormalStartActorAssertion",
    "FormalStartActorAssertionContent",
    "FormalStartCandidateBinding",
    "FormalStartIntentActionRequest",
    "FormalStartIntentRequest",
    "FormalStartIntentResult",
    "FormalStartIntentView",
    "FormalStartSignerRef",
    "derive_formal_start_ids",
    "formal_start_action_subject_digest",
    "formal_start_content_hash",
    "formal_start_request_subject_digest",
    "publish_formal_evaluation_start_authority",
    "publish_formal_execution_start_authority",
    "publish_formal_start_actor_assertion",
]
