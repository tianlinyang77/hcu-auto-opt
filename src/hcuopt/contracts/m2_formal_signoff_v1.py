# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.m2_formal_authority_v1 import FormalAuthorityContextRef
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.evaluation.m2_models import FrozenEvaluationModel
from hcuopt.measurement.evidence import canonical_json_bytes

M2_FORMAL_SIGNOFF_SCHEMA_VERSION = "m2a-formal-signoff-v1"
M2_FORMAL_SIGNOFF_ARTIFACT_SCHEMA_VERSION = "m2a-formal-signoff-decision-v1"

FormalRoundSignoffDecision = Literal["approved", "rejected"]
FormalRoundSignoffIntentState = Literal[
    "preparing", "artifact_published", "finalized", "failed"
]
FormalRoundSignoffOutboxState = Literal["pending", "artifact_published", "finalized"]


class FormalRoundSignoffRequest(FrozenEvaluationModel):
    round_id: UUID
    round_evidence_bundle_id: UUID
    decision: FormalRoundSignoffDecision
    actor: str = Field(min_length=1, max_length=300)
    actor_identity_hash: str = Field(pattern=SHA256_PATTERN)
    reason: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=8, max_length=300)


class FormalRoundSignoffIntent(FrozenEvaluationModel):
    schema_version: Literal["m2a-formal-signoff-v1"] = M2_FORMAL_SIGNOFF_SCHEMA_VERSION
    signoff_intent_id: UUID
    round_signoff_id: UUID
    round_id: UUID
    task_id: UUID
    authority_context: FormalAuthorityContextRef
    round_evidence_bundle_id: UUID
    evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    decision: FormalRoundSignoffDecision
    actor: str = Field(min_length=1, max_length=300)
    actor_identity_hash: str = Field(pattern=SHA256_PATTERN)
    reason: str = Field(min_length=1, max_length=4000)
    decision_at: datetime
    input_digest: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    state: FormalRoundSignoffIntentState = "preparing"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_deterministic_identity(self) -> FormalRoundSignoffIntent:
        if self.decision_at.tzinfo is None or self.decision_at.utcoffset() is None:
            raise ValueError("Formal Signoff decision_at must be timezone-aware")
        expected = formal_round_signoff_input_digest(self)
        if self.input_digest != expected:
            raise ValueError("Formal Signoff input_digest does not match frozen inputs")
        if self.signoff_intent_id != uuid5(
            NAMESPACE_URL, f"hcuopt:m2-formal-signoff-intent:{expected}"
        ):
            raise ValueError("Formal Signoff Intent ID is not deterministic")
        if self.round_signoff_id != uuid5(
            NAMESPACE_URL, f"hcuopt:m2-formal-round-signoff:{expected}"
        ):
            raise ValueError("Formal Round Signoff ID is not deterministic")
        return self


class FormalDecisionSignature(FrozenEvaluationModel):
    signer_id: str = Field(min_length=1, max_length=300)
    signer_key_id: str = Field(min_length=1, max_length=300)
    signer_identity_hash: str = Field(pattern=SHA256_PATTERN)
    algorithm: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    value: str = Field(pattern=r"^[A-Za-z0-9_-]{16,16384}$")


class FormalRoundSignoffArtifactContent(FrozenEvaluationModel):
    schema_version: Literal["m2a-formal-signoff-decision-v1"] = (
        M2_FORMAL_SIGNOFF_ARTIFACT_SCHEMA_VERSION
    )
    signoff_intent_id: UUID
    round_signoff_id: UUID
    round_id: UUID
    task_id: UUID
    authority_context_id: UUID
    authority_context_hash: str = Field(pattern=SHA256_PATTERN)
    round_evidence_bundle_id: UUID
    evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    decision: FormalRoundSignoffDecision
    actor: str = Field(min_length=1, max_length=300)
    actor_identity_hash: str = Field(pattern=SHA256_PATTERN)
    reason: str = Field(min_length=1, max_length=4000)
    decision_at: datetime
    input_digest: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    performance_scope: Literal["formal_single_operation_only"] = (
        "formal_single_operation_only"
    )
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @field_validator("decision_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Formal Signoff Artifact time must be timezone-aware")
        return value


class FormalRoundSignoffDecisionArtifact(FrozenEvaluationModel):
    content: FormalRoundSignoffArtifactContent
    signature: FormalDecisionSignature


class FormalRoundSignoffArtifactPublication(FrozenEvaluationModel):
    signoff_intent_id: UUID
    round_signoff_id: UUID
    decision_artifact_uri: str = Field(min_length=1, max_length=4000)
    decision_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    signature: FormalDecisionSignature

    @field_validator("decision_artifact_uri")
    @classmethod
    def require_local_file_uri(cls, value: str) -> str:
        if not (value.startswith("file:///") or value.startswith("file://localhost/")):
            raise ValueError("Formal Signoff Artifact requires a local file URI")
        if "?" in value or "#" in value:
            raise ValueError("Formal Signoff Artifact URI cannot contain query or fragment")
        return value


class FormalRoundSignoff(FrozenEvaluationModel):
    schema_version: Literal["m2a-formal-signoff-v1"] = M2_FORMAL_SIGNOFF_SCHEMA_VERSION
    round_signoff_id: UUID
    signoff_intent_id: UUID
    round_id: UUID
    task_id: UUID
    authority_context: FormalAuthorityContextRef
    round_evidence_bundle_id: UUID
    evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    decision: FormalRoundSignoffDecision
    actor: str = Field(min_length=1, max_length=300)
    actor_identity_hash: str = Field(pattern=SHA256_PATTERN)
    reason: str = Field(min_length=1, max_length=4000)
    decision_at: datetime
    input_digest: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    decision_artifact_uri: str = Field(min_length=1, max_length=4000)
    decision_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    signature: FormalDecisionSignature
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


def build_formal_round_signoff_intent(
    *,
    request: FormalRoundSignoffRequest,
    task_id: UUID,
    authority_context: FormalAuthorityContextRef,
    evidence_bundle_hash: str,
    candidate_family_hash: str,
    artifact_family_hash: str,
    holdout_family_hash: str | None,
    decision_at: datetime,
) -> FormalRoundSignoffIntent:
    values = {
        "round_id": request.round_id,
        "task_id": task_id,
        "authority_context": authority_context,
        "round_evidence_bundle_id": request.round_evidence_bundle_id,
        "evidence_bundle_hash": evidence_bundle_hash,
        "candidate_family_hash": candidate_family_hash,
        "artifact_family_hash": artifact_family_hash,
        "holdout_family_hash": holdout_family_hash,
        "decision": request.decision,
        "actor": request.actor,
        "actor_identity_hash": request.actor_identity_hash,
        "reason": request.reason,
        "decision_at": decision_at,
        "idempotency_key": request.idempotency_key,
        "synthetic": False,
        "automatic_release_allowed": False,
    }
    digest = _sha256(values)
    return FormalRoundSignoffIntent(
        signoff_intent_id=uuid5(
            NAMESPACE_URL, f"hcuopt:m2-formal-signoff-intent:{digest}"
        ),
        round_signoff_id=uuid5(
            NAMESPACE_URL, f"hcuopt:m2-formal-round-signoff:{digest}"
        ),
        input_digest=digest,
        state="preparing",
        **values,
    )


def formal_round_signoff_input_digest(intent: FormalRoundSignoffIntent) -> str:
    values = intent.model_dump(
        mode="json",
        exclude={
            "schema_version",
            "signoff_intent_id",
            "round_signoff_id",
            "input_digest",
            "state",
        },
    )
    return _sha256(values)


def formal_round_signoff_artifact_content(
    intent: FormalRoundSignoffIntent,
) -> FormalRoundSignoffArtifactContent:
    return FormalRoundSignoffArtifactContent(
        signoff_intent_id=intent.signoff_intent_id,
        round_signoff_id=intent.round_signoff_id,
        round_id=intent.round_id,
        task_id=intent.task_id,
        authority_context_id=intent.authority_context.authority_context_id,
        authority_context_hash=intent.authority_context.context_hash,
        round_evidence_bundle_id=intent.round_evidence_bundle_id,
        evidence_bundle_hash=intent.evidence_bundle_hash,
        candidate_family_hash=intent.candidate_family_hash,
        artifact_family_hash=intent.artifact_family_hash,
        holdout_family_hash=intent.holdout_family_hash,
        decision=intent.decision,
        actor=intent.actor,
        actor_identity_hash=intent.actor_identity_hash,
        reason=intent.reason,
        decision_at=intent.decision_at,
        input_digest=intent.input_digest,
        idempotency_key=intent.idempotency_key,
    )


def formal_round_signoff_artifact_hash(
    artifact: FormalRoundSignoffDecisionArtifact,
) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(artifact)).hexdigest()


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(_jsonable(value))).hexdigest()


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value
