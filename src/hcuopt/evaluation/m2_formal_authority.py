# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextRef,
    FrozenFormalAuthorityModel,
)
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import RoundPhase, SearchRoundRunMode
from hcuopt.evaluation.m2_models import (
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundEvidenceBundle,
)
from hcuopt.measurement.evidence import canonical_json_bytes


def formal_authority_payload_hash(value: ContractModel) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class FormalBarrierPersistence(FrozenFormalAuthorityModel):
    context: FormalAuthorityContextRef
    barrier: RoundBarrierResult
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    parent_search_barrier_id: UUID | None = None
    payload_hash: str = Field(pattern=SHA256_PATTERN)
    run_mode: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_formal_barrier(self) -> FormalBarrierPersistence:
        if (
            self.barrier.run_mode is not SearchRoundRunMode.FORMAL
            or self.barrier.synthetic
            or self.barrier.round_id != self.context.round_id
        ):
            raise ValueError("Formal Barrier disagrees with its Authority Context")
        if formal_authority_payload_hash(self.barrier) != self.payload_hash:
            raise ValueError("Formal Barrier payload does not match payload_hash")
        if self.barrier.phase is RoundPhase.SEARCH:
            if (
                self.barrier.input_family_hash != self.context.artifact_family_hash
                or self.holdout_family_hash is not None
                or self.parent_search_barrier_id is not None
            ):
                raise ValueError("Formal Search Barrier must bind only the Artifact Family")
        elif (
            self.holdout_family_hash is None
            or self.parent_search_barrier_id is None
            or self.barrier.input_family_hash != self.holdout_family_hash
        ):
            raise ValueError("Formal Holdout Barrier requires its Search parent and Family")
        return self


class FormalHoldoutRevealPersistence(FrozenFormalAuthorityModel):
    context: FormalAuthorityContextRef
    search_barrier_id: UUID
    reveal_lease_id: UUID
    fencing_token: int = Field(ge=1)
    holdout_family_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_plan_hash: str = Field(pattern=SHA256_PATTERN)
    reveal_evidence_uri: str = Field(min_length=1, max_length=4000)
    reveal_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    revealed_by: str = Field(min_length=1, max_length=300)
    revealed_at: datetime
    run_mode: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_protected_evidence_reference(self) -> FormalHoldoutRevealPersistence:
        if self.revealed_at.tzinfo is None or self.revealed_at.utcoffset() is None:
            raise ValueError("Formal Holdout Reveal time must be timezone-aware")
        parsed = urlparse(self.reveal_evidence_uri)
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or not parsed.path.startswith("/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Formal Holdout Reveal requires an absolute local file URI")
        return self


class FormalMultipleComparisonPersistence(FrozenFormalAuthorityModel):
    context: FormalAuthorityContextRef
    result: MultipleComparisonResult
    payload_hash: str = Field(pattern=SHA256_PATTERN)
    run_mode: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_formal_multiple_comparison(self) -> FormalMultipleComparisonPersistence:
        if (
            self.result.run_mode is not SearchRoundRunMode.FORMAL
            or self.result.synthetic
            or self.result.round_id != self.context.round_id
        ):
            raise ValueError("Formal FWER result disagrees with its Authority Context")
        if formal_authority_payload_hash(self.result) != self.payload_hash:
            raise ValueError("Formal FWER payload does not match payload_hash")
        return self


class FormalEvidenceBundlePersistence(FrozenFormalAuthorityModel):
    context: FormalAuthorityContextRef
    bundle: RoundEvidenceBundle
    payload_hash: str = Field(pattern=SHA256_PATTERN)
    run_mode: Literal["formal"] = "formal"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def bind_formal_evidence_bundle(self) -> FormalEvidenceBundlePersistence:
        if (
            self.bundle.run_mode is not SearchRoundRunMode.FORMAL
            or self.bundle.synthetic
            or self.bundle.round_id != self.context.round_id
            or self.bundle.task_id != self.context.task_id
            or self.bundle.candidate_family_hash != self.context.candidate_family_hash
            or self.bundle.artifact_family_hash != self.context.artifact_family_hash
            or self.bundle.search_plan_hash != self.context.search_plan_hash
            or self.bundle.holdout_plan_commitment
            != self.context.holdout_plan_commitment
        ):
            raise ValueError("Formal Evidence Bundle disagrees with its Authority Context")
        parsed = urlparse(self.bundle.evidence_index_uri)
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or not parsed.path.startswith("/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Formal Evidence Bundle requires an absolute local file Evidence Index"
            )
        if formal_authority_payload_hash(self.bundle) != self.payload_hash:
            raise ValueError("Formal Evidence Bundle payload does not match payload_hash")
        return self
