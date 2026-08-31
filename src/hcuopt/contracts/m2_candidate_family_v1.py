# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.platform_v1 import SHA256_PATTERN

M2A_BUSINESS_CANDIDATE_FAMILY_SCHEMA_VERSION = "m2a-business-candidate-family-v1"


class BusinessCandidateFamilyMember(ContractModel):
    """One C-reviewed business Candidate package before a Formal Round exists."""

    candidate_id: UUID
    source_package_ref: CandidateSourcePackageRef
    optimization_intent: str = Field(min_length=1, max_length=2000)

    @field_validator("optimization_intent")
    @classmethod
    def require_normalized_intent(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("Candidate optimization intent must not have outer whitespace")
        return value


class BusinessCandidateFamilyManifest(ContractModel):
    """Immutable source-family commitment consumed later by the Formal compiler."""

    schema_version: Literal["m2a-business-candidate-family-v1"] = (
        M2A_BUSINESS_CANDIDATE_FAMILY_SCHEMA_VERSION
    )
    family_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    source_package_store_id: str = Field(min_length=1, max_length=300)
    source_package_store_hash: str = Field(pattern=SHA256_PATTERN)
    target_snapshot_id: UUID
    stage0_run_id: UUID
    baseline_epoch_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_id: UUID
    replacement_point: str = Field(min_length=1, max_length=1000)
    profiler_evidence_uri: str = Field(min_length=1, max_length=4000)
    profiler_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    overlay_mount_target: str = Field(min_length=1, max_length=2000)
    overlay_file_path: str = Field(min_length=1, max_length=2000)
    members: tuple[BusinessCandidateFamilyMember, ...] = Field(
        min_length=2,
        max_length=4,
    )
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    track: Literal["triton"] = "triton"
    release_mode: Literal["overlay"] = "overlay"
    candidate_kind: Literal["business"] = "business"
    synthetic: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @field_validator("source_package_store_id", "replacement_point", "reviewed_by")
    @classmethod
    def require_normalized_text(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("Candidate Family text fields must not have outer whitespace")
        return value

    @field_validator("overlay_mount_target")
    @classmethod
    def require_absolute_overlay_target(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Candidate Family Overlay target must be a clean absolute path")
        return value

    @field_validator("overlay_file_path")
    @classmethod
    def require_relative_overlay_file(cls, value: str) -> str:
        parts = value.split("/")
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in parts)
            or not value.endswith((".py", ".pyi"))
        ):
            raise ValueError("Candidate Family Overlay file must be normalized Python source")
        return value

    @model_validator(mode="after")
    def require_unique_business_members(self) -> BusinessCandidateFamilyManifest:
        identities = {
            "Candidate identity": [str(item.candidate_id) for item in self.members],
            "Candidate source": [
                item.source_package_ref.candidate_source_hash for item in self.members
            ],
            "source package": [
                item.source_package_ref.source_package_hash for item in self.members
            ],
            "source Manifest": [item.source_package_ref.manifest_hash for item in self.members],
        }
        for label, values in identities.items():
            if len(values) != len(set(values)):
                raise ValueError(f"Business Candidate Family contains duplicate {label}")
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("Candidate Family review time must be timezone-aware")
        return self
