# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN

M2_FORMAL_AUTHORITY_SCHEMA_VERSION = "m2a-formal-authority-v1"


def _canonical_hash(value: ContractModel) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256((payload + "\n").encode()).hexdigest()


class FrozenFormalAuthorityModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FormalEvidenceStoreRef(FrozenFormalAuthorityModel):
    store_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    store_version: int = Field(ge=1)
    store_hash: str = Field(pattern=SHA256_PATTERN)
    access_policy_hash: str = Field(pattern=SHA256_PATTERN)


class FormalVerifierRef(FrozenFormalAuthorityModel):
    verifier_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    verifier_version: str = Field(min_length=1, max_length=200)
    verifier_hash: str = Field(pattern=SHA256_PATTERN)


class FormalAuthorityContextContent(FrozenFormalAuthorityModel):
    schema_version: Literal["m2a-formal-authority-v1"] = (
        M2_FORMAL_AUTHORITY_SCHEMA_VERSION
    )
    authority_context_id: UUID
    round_id: UUID
    task_id: UUID
    target_snapshot_id: UUID
    stage0_run_id: UUID
    stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_epoch_id: UUID
    hotspot_id: UUID
    target_profile_hash: str = Field(pattern=SHA256_PATTERN)
    workload_profile_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_profile_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    search_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_commitment: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_authority_id: str = Field(min_length=1, max_length=300)
    holdout_plan_authority_hash: str = Field(pattern=SHA256_PATTERN)
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    evidence_store: FormalEvidenceStoreRef
    verifier: FormalVerifierRef
    sealed_by: str = Field(min_length=1, max_length=300)
    sealed_at: datetime
    run_mode: Literal["formal"] = "formal"
    project_mode: Literal["degraded_manual_intake"] = "degraded_manual_intake"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_timezone_aware_seal(self) -> FormalAuthorityContextContent:
        if self.sealed_at.tzinfo is None or self.sealed_at.utcoffset() is None:
            raise ValueError("Formal Authority seal time must be timezone-aware")
        return self


class FormalAuthorityContextDescriptor(FormalAuthorityContextContent):
    context_hash: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_context_hash(self) -> FormalAuthorityContextDescriptor:
        content = FormalAuthorityContextContent.model_validate(
            self.model_dump(mode="json", exclude={"context_hash"})
        )
        if formal_authority_context_hash(content) != self.context_hash:
            raise ValueError("Formal Authority Context does not match context_hash")
        return self


class FormalAuthorityContextRef(FrozenFormalAuthorityModel):
    authority_context_id: UUID
    context_hash: str = Field(pattern=SHA256_PATTERN)
    round_id: UUID
    task_id: UUID
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    search_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_commitment: str = Field(pattern=SHA256_PATTERN)
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    evidence_store: FormalEvidenceStoreRef
    run_mode: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


def formal_authority_context_hash(content: FormalAuthorityContextContent) -> str:
    return _canonical_hash(content)


def publish_formal_authority_context(
    content: FormalAuthorityContextContent,
) -> FormalAuthorityContextDescriptor:
    return FormalAuthorityContextDescriptor.model_validate(
        {
            **content.model_dump(mode="json"),
            "context_hash": formal_authority_context_hash(content),
        }
    )


def formal_authority_context_ref(
    context: FormalAuthorityContextDescriptor,
) -> FormalAuthorityContextRef:
    return FormalAuthorityContextRef(
        authority_context_id=context.authority_context_id,
        context_hash=context.context_hash,
        round_id=context.round_id,
        task_id=context.task_id,
        candidate_family_hash=context.candidate_family_hash,
        artifact_family_hash=context.artifact_family_hash,
        search_plan_hash=context.search_plan_hash,
        holdout_plan_commitment=context.holdout_plan_commitment,
        selection_rule_hash=context.selection_rule_hash,
        evidence_store=context.evidence_store,
    )
