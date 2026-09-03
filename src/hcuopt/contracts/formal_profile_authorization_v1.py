# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.m2 import RoundBudget
from hcuopt.contracts.operator_v1 import OperatorProfileRef
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN
from hcuopt.measurement.evidence import canonical_json_bytes

M2A_FORMAL_PROFILE_AUTHORIZATION_SCHEMA_VERSION = (
    "m2a-formal-profile-window-authorization-v1"
)


class FrozenFormalProfileAuthorizationModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FormalProfileSetRef(FrozenFormalProfileAuthorizationModel):
    target_profile: OperatorProfileRef
    workload_profile: OperatorProfileRef
    measurement_profile: OperatorProfileRef

    @model_validator(mode="after")
    def require_canonical_profile_kinds(self) -> FormalProfileSetRef:
        expected = (
            (self.target_profile, "target"),
            (self.workload_profile, "workload"),
            (self.measurement_profile, "measurement"),
        )
        if any(reference.profile_kind != kind for reference, kind in expected):
            raise ValueError("Formal Profile Set refs must use their canonical kinds")
        return self


class FormalProfileGrantVerifierRef(FrozenFormalProfileAuthorizationModel):
    verifier_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    verifier_version: str = Field(min_length=1, max_length=200)
    verifier_hash: str = Field(pattern=SHA256_PATTERN)
    signature_scheme: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    key_id: str = Field(min_length=1, max_length=300)

    @field_validator("verifier_version", "key_id")
    @classmethod
    def require_normalized_verifier_text(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("Formal authorization verifier fields must be normalized")
        return value


class FormalProfileWindowAuthorizationContent(FrozenFormalProfileAuthorizationModel):
    """Project-owner decision for one exact Formal Profile set and HCU window."""

    schema_version: Literal["m2a-formal-profile-window-authorization-v1"] = (
        M2A_FORMAL_PROFILE_AUTHORIZATION_SCHEMA_VERSION
    )
    authorization_id: UUID
    decision: Literal["authorized", "rejected"]
    readiness_audit_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    readiness_audit_base_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    readiness_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    readiness_report_hash: str = Field(pattern=SHA256_PATTERN)
    profiles: FormalProfileSetRef
    source_family_hash: str = Field(pattern=SHA256_PATTERN)
    budget: RoundBudget
    host_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,199}$")
    resource_id: str = Field(min_length=1, max_length=200)
    window_starts_at: datetime
    window_expires_at: datetime
    authorized_by: str = Field(min_length=1, max_length=300)
    authorization_evidence_uri: str = Field(min_length=1, max_length=4000)
    authorization_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    issued_at: datetime
    verifier: FormalProfileGrantVerifierRef
    run_mode: Literal["formal"] = "formal"
    project_mode: Literal["degraded_manual_intake"] = "degraded_manual_intake"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @field_validator("resource_id", "authorized_by", "authorization_evidence_uri")
    @classmethod
    def require_normalized_authorization_text(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("Formal window authorization fields must be normalized")
        return value

    @model_validator(mode="after")
    def require_bounded_authorization_window(self) -> FormalProfileWindowAuthorizationContent:
        timestamps = (self.issued_at, self.window_starts_at, self.window_expires_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("Formal authorization times must be timezone-aware")
        if self.window_starts_at < self.issued_at:
            raise ValueError("Formal window cannot start before authorization is issued")
        if self.window_expires_at <= self.window_starts_at:
            raise ValueError("Formal window expiry must follow its start")
        return self


class FormalProfileWindowAuthorization(FormalProfileWindowAuthorizationContent):
    authorization_hash: str = Field(pattern=SHA256_PATTERN)
    signature: str = Field(min_length=1, max_length=16_384)

    @field_validator("signature")
    @classmethod
    def require_normalized_signature(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("Formal authorization signature must be normalized")
        return value

    @model_validator(mode="after")
    def verify_authorization_hash(self) -> FormalProfileWindowAuthorization:
        content = FormalProfileWindowAuthorizationContent.model_validate(
            self.model_dump(mode="json", exclude={"authorization_hash", "signature"})
        )
        if formal_profile_window_authorization_hash(content) != self.authorization_hash:
            raise ValueError("Formal authorization content does not match authorization_hash")
        return self


def formal_profile_window_authorization_hash(
    content: FormalProfileWindowAuthorizationContent,
) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def publish_formal_profile_window_authorization(
    content: FormalProfileWindowAuthorizationContent,
    *,
    signature: str,
) -> FormalProfileWindowAuthorization:
    return FormalProfileWindowAuthorization.model_validate(
        {
            **content.model_dump(mode="json"),
            "authorization_hash": formal_profile_window_authorization_hash(content),
            "signature": signature,
        }
    )
