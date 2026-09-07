# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from urllib.parse import unquote, urlparse
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN
from hcuopt.measurement.evidence import canonical_json_bytes

PRODUCTION_EVIDENCE_ROOT_SCHEMA_VERSION = "m2a-production-evidence-root-v1"
FORMAL_EVIDENCE_VERIFIER_SCHEMA_VERSION = "m2a-formal-evidence-verifier-v1"
FORMAL_EVIDENCE_ACCEPTANCE_REVIEW_SCHEMA_VERSION = "m2a-formal-evidence-acceptance-review-v2"
FORMAL_EVIDENCE_ACCEPTANCE_REPORT_SCHEMA_VERSION = "m2a-formal-evidence-acceptance-report-v1"

FormalEvidenceProducerRole = Literal[
    "control_plane",
    "holdout_plan_authority",
    "measurement_producer",
    "source_artifact_producer",
    "independent_verifier",
    "project_owner",
]
FormalEvidenceClass = Literal[
    "authority_context",
    "round_authority",
    "search_plan",
    "search_measurement",
    "search_cleanup",
    "holdout_reveal",
    "holdout_measurement",
    "holdout_cleanup",
    "source_family",
    "artifact_family",
    "correctness",
    "barrier",
    "fwer",
    "evidence_bundle",
    "signoff",
]


class _FrozenAcceptanceModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _content_hash(value: ContractModel) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_absolute_local_uri(value: str) -> str:
    parsed = urlparse(value)
    path = unquote(parsed.path)
    if (
        parsed.scheme != "file"
        or parsed.netloc not in {"", "localhost"}
        or not path.startswith("/")
        or parsed.query
        or parsed.fragment
        or "/../" in f"{path}/"
    ):
        raise ValueError("production Evidence URI must be an absolute local file URI")
    return value.rstrip("/")


class ProductionEvidenceRootContent(_FrozenAcceptanceModel):
    schema_version: Literal["m2a-production-evidence-root-v1"] = (
        PRODUCTION_EVIDENCE_ROOT_SCHEMA_VERSION
    )
    root_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    root_version: int = Field(ge=1)
    root_uri: str = Field(min_length=1, max_length=4000)
    object_layout: Literal["objects/sha256/<2>/<62>"] = "objects/sha256/<2>/<62>"
    access_policy_hash: str = Field(pattern=SHA256_PATTERN)
    retention_policy_hash: str = Field(pattern=SHA256_PATTERN)
    deployment_owner: str = Field(min_length=1, max_length=300)
    max_object_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @field_validator("root_uri")
    @classmethod
    def require_protected_local_root(cls, value: str) -> str:
        return _require_absolute_local_uri(value)

    @field_validator("deployment_owner")
    @classmethod
    def require_normalized_owner(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("Evidence Root deployment owner must be normalized")
        return value


class ProductionEvidenceRootDescriptor(ProductionEvidenceRootContent):
    root_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_root_hash(self) -> ProductionEvidenceRootDescriptor:
        content = ProductionEvidenceRootContent.model_validate(
            self.model_dump(mode="json", exclude={"root_hash"})
        )
        if production_evidence_root_hash(content) != self.root_hash:
            raise ValueError("production Evidence Root content does not match root_hash")
        return self


class FormalEvidenceVerifierIdentityContent(_FrozenAcceptanceModel):
    schema_version: Literal["m2a-formal-evidence-verifier-v1"] = (
        FORMAL_EVIDENCE_VERIFIER_SCHEMA_VERSION
    )
    verifier_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    verifier_version: str = Field(min_length=1, max_length=200)
    capability: Literal["formal_evidence_acceptance"] = "formal_evidence_acceptance"
    authority_role: Literal["independent_verifier"] = "independent_verifier"
    implementation_kind: Literal["real"] = "real"
    executable_hash: str = Field(pattern=SHA256_PATTERN)
    configuration_hash: str = Field(pattern=SHA256_PATTERN)
    source_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    attestation_scheme: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    attestation_key_id: str = Field(min_length=1, max_length=300)
    identity_evidence_uri: str = Field(min_length=1, max_length=4000)
    identity_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    automatic_release_allowed: Literal[False] = False

    @field_validator("verifier_version", "attestation_key_id")
    @classmethod
    def require_normalized_identity_text(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("Formal Verifier identity fields must be normalized")
        return value

    @field_validator("identity_evidence_uri")
    @classmethod
    def require_identity_evidence_uri(cls, value: str) -> str:
        return _require_absolute_local_uri(value)


class FormalEvidenceVerifierIdentity(FormalEvidenceVerifierIdentityContent):
    identity_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_identity_hash(self) -> FormalEvidenceVerifierIdentity:
        content = FormalEvidenceVerifierIdentityContent.model_validate(
            self.model_dump(mode="json", exclude={"identity_hash"})
        )
        if formal_evidence_verifier_identity_hash(content) != self.identity_hash:
            raise ValueError("Formal Verifier identity does not match identity_hash")
        return self


class FormalProducerIdentity(_FrozenAcceptanceModel):
    producer_role: FormalEvidenceProducerRole
    producer_id: str = Field(min_length=1, max_length=300)
    producer_hash: str = Field(pattern=SHA256_PATTERN)
    capability: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")


class FormalEvidenceAuthoritySet(_FrozenAcceptanceModel):
    control_plane: FormalProducerIdentity
    measurement_producer: FormalProducerIdentity
    source_artifact_producer: FormalProducerIdentity
    project_owner: FormalProducerIdentity
    independent_verifier: FormalEvidenceVerifierIdentity

    @model_validator(mode="after")
    def require_role_separation(self) -> FormalEvidenceAuthoritySet:
        expected_roles = (
            (self.control_plane, "control_plane"),
            (self.measurement_producer, "measurement_producer"),
            (self.source_artifact_producer, "source_artifact_producer"),
            (self.project_owner, "project_owner"),
        )
        if any(identity.producer_role != role for identity, role in expected_roles):
            raise ValueError("Formal producer identity is assigned to the wrong role")
        identity_ids = [identity.producer_id for identity, _ in expected_roles]
        identity_hashes = [identity.producer_hash for identity, _ in expected_roles]
        identity_ids.append(self.independent_verifier.verifier_id)
        identity_hashes.append(self.independent_verifier.identity_hash)
        if len(set(identity_ids)) != len(identity_ids) or len(set(identity_hashes)) != len(
            identity_hashes
        ):
            raise ValueError("Formal producer and Verifier identities must be role-separated")
        return self


class ProductionFormalEvidenceRef(_FrozenAcceptanceModel):
    evidence_class: FormalEvidenceClass
    uri: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=SHA256_PATTERN)
    byte_count: int = Field(ge=1, le=64 * 1024 * 1024)
    producer_role: FormalEvidenceProducerRole
    producer_id: str = Field(min_length=1, max_length=300)
    producer_hash: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime

    @field_validator("uri")
    @classmethod
    def require_local_object_uri(cls, value: str) -> str:
        return _require_absolute_local_uri(value)

    @model_validator(mode="after")
    def require_aware_creation_time(self) -> ProductionFormalEvidenceRef:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Formal Evidence creation time must be timezone-aware")
        return self


class FormalEvidenceAcceptanceReviewContent(_FrozenAcceptanceModel):
    """D-owned review of one exact Formal evidence terminal path."""

    schema_version: Literal["m2a-formal-evidence-acceptance-review-v2"] = (
        FORMAL_EVIDENCE_ACCEPTANCE_REVIEW_SCHEMA_VERSION
    )
    review_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,299}$")
    decision: Literal["accepted_for_formal_window", "blocked"]
    blocker_codes: tuple[str, ...] = Field(default=(), max_length=64)
    reason: str = Field(min_length=1, max_length=4000)
    readiness_audit_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    readiness_audit_base_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    readiness_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    readiness_report_hash: str = Field(pattern=SHA256_PATTERN)
    round_id: UUID
    authority_context_id: UUID
    authority_context_hash: str = Field(pattern=SHA256_PATTERN)
    target_lock_hash: str = Field(pattern=SHA256_PATTERN)
    terminal_path: Literal["zero_promotion", "holdout_fwer"]
    round_evidence_bundle_id: UUID
    round_evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    signoff_artifact_hash: str = Field(pattern=SHA256_PATTERN)
    signoff_signer_identity_hash: str = Field(pattern=SHA256_PATTERN)
    evidence_root: ProductionEvidenceRootDescriptor
    verifier: FormalEvidenceVerifierIdentity
    verification_input_digest: str = Field(pattern=SHA256_PATTERN)
    verified_evidence_count: int = Field(ge=0)
    recursive_semantic_verification: Literal["verified", "blocked"]
    allowlisted_signature_verification: Literal["verified", "blocked"]
    verification_summary_uri: str = Field(min_length=1, max_length=4000)
    verification_summary_hash: str = Field(pattern=SHA256_PATTERN)
    reviewed_at: datetime
    owner_window_authorization: Literal["not_granted"] = "not_granted"
    hcu_accessed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @field_validator("blocker_codes")
    @classmethod
    def require_canonical_blockers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("Formal acceptance blockers must be unique and sorted")
        return value

    @field_validator("verification_summary_uri")
    @classmethod
    def require_summary_uri(cls, value: str) -> str:
        return _require_absolute_local_uri(value)

    @model_validator(mode="after")
    def require_decision_evidence(self) -> FormalEvidenceAcceptanceReviewContent:
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("Formal acceptance review time must be timezone-aware")
        if self.decision == "accepted_for_formal_window" and (
            self.blocker_codes
            or self.recursive_semantic_verification != "verified"
            or self.allowlisted_signature_verification != "verified"
            or self.verified_evidence_count == 0
        ):
            raise ValueError(
                "accepted_for_formal_window requires recursive semantics, an allowlisted "
                "signature, verified evidence, and no blockers"
            )
        if self.decision == "blocked" and not self.blocker_codes:
            raise ValueError("blocked Formal evidence requires explicit blocker codes")
        return self


class FormalEvidenceAcceptanceReviewSignature(_FrozenAcceptanceModel):
    verifier_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    verifier_key_id: str = Field(min_length=1, max_length=300)
    verifier_identity_hash: str = Field(pattern=SHA256_PATTERN)
    algorithm: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    value: str = Field(pattern=r"^[A-Za-z0-9_-]{16,16384}$")


class FormalEvidenceAcceptanceReview(FormalEvidenceAcceptanceReviewContent):
    review_hash: str = Field(pattern=SHA256_PATTERN)
    signature: FormalEvidenceAcceptanceReviewSignature

    @model_validator(mode="after")
    def verify_review_hash(self) -> FormalEvidenceAcceptanceReview:
        content = FormalEvidenceAcceptanceReviewContent.model_validate(
            self.model_dump(mode="json", exclude={"review_hash", "signature"})
        )
        if formal_evidence_acceptance_review_hash(content) != self.review_hash:
            raise ValueError("Formal acceptance review does not match review_hash")
        if (
            self.signature.verifier_id != self.verifier.verifier_id
            or self.signature.verifier_key_id != self.verifier.attestation_key_id
            or self.signature.verifier_identity_hash != self.verifier.identity_hash
            or self.signature.algorithm != self.verifier.attestation_scheme
        ):
            raise ValueError("Formal acceptance review signature identity drifted")
        return self


class FormalEvidenceAcceptanceReport(_FrozenAcceptanceModel):
    """Authenticated read model for one persisted D-owned Formal review."""

    schema_version: Literal["m2a-formal-evidence-acceptance-report-v1"] = (
        FORMAL_EVIDENCE_ACCEPTANCE_REPORT_SCHEMA_VERSION
    )
    round_id: UUID
    readiness_audit_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    snapshot_hash: str = Field(pattern=SHA256_PATTERN)
    review: FormalEvidenceAcceptanceReview
    owner_window_authorization: Literal["not_granted"] = "not_granted"
    hcu_accessed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_bound_review(self) -> FormalEvidenceAcceptanceReport:
        if (
            self.review.round_id != self.round_id
            or self.review.readiness_audit_id != self.readiness_audit_id
            or self.review.verification_input_digest != self.snapshot_hash
        ):
            raise ValueError("Formal acceptance Report review is bound to another snapshot")
        return self


def production_evidence_root_hash(content: ProductionEvidenceRootContent) -> str:
    return _content_hash(content)


def publish_production_evidence_root(
    content: ProductionEvidenceRootContent,
) -> ProductionEvidenceRootDescriptor:
    return ProductionEvidenceRootDescriptor.model_validate(
        {**content.model_dump(mode="json"), "root_hash": production_evidence_root_hash(content)}
    )


def formal_evidence_verifier_identity_hash(
    content: FormalEvidenceVerifierIdentityContent,
) -> str:
    return _content_hash(content)


def publish_formal_evidence_verifier_identity(
    content: FormalEvidenceVerifierIdentityContent,
) -> FormalEvidenceVerifierIdentity:
    return FormalEvidenceVerifierIdentity.model_validate(
        {
            **content.model_dump(mode="json"),
            "identity_hash": formal_evidence_verifier_identity_hash(content),
        }
    )


def formal_evidence_acceptance_review_hash(
    content: FormalEvidenceAcceptanceReviewContent,
) -> str:
    return _content_hash(content)


def publish_formal_evidence_acceptance_review(
    content: FormalEvidenceAcceptanceReviewContent,
    *,
    signature: FormalEvidenceAcceptanceReviewSignature,
) -> FormalEvidenceAcceptanceReview:
    return FormalEvidenceAcceptanceReview.model_validate(
        {
            **content.model_dump(mode="json"),
            "review_hash": formal_evidence_acceptance_review_hash(content),
            "signature": signature,
        }
    )
