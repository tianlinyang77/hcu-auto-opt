# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.platform_v1 import (
    GIT_COMMIT_PATTERN,
    SHA256_PATTERN,
    AdapterProvenance,
)

M2A_BUSINESS_CANDIDATE_FAMILY_SCHEMA_VERSION = "m2a-business-candidate-family-v1"
M2A_BUSINESS_CANDIDATE_STORE_DESCRIPTOR_SCHEMA_VERSION = (
    "m2a-business-candidate-store-descriptor-v1"
)
M2A_BUSINESS_CANDIDATE_FAMILY_VERIFICATION_SCHEMA_VERSION = (
    "m2a-business-candidate-family-verification-v1"
)


def _require_relative_posix_path(value: str, *, label: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} must be a normalized repository-relative POSIX path")
    return value


class BusinessCandidatePackageStoreDescriptor(ContractModel):
    """C-owned immutable description of one reviewable Candidate Package Store."""

    schema_version: Literal["m2a-business-candidate-store-descriptor-v1"] = (
        M2A_BUSINESS_CANDIDATE_STORE_DESCRIPTOR_SCHEMA_VERSION
    )
    store_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    store_version: int = Field(ge=1)
    profile: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    layout: Literal["sha256-prefix-v1"] = "sha256-prefix-v1"
    baseline_repository: str = Field(min_length=1, max_length=2000)
    baseline_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    allowed_overlay_roots: tuple[str, ...] = Field(min_length=1, max_length=8)
    approved_mount_targets: dict[str, str] = Field(min_length=1, max_length=8)
    packages: tuple[CandidateSourcePackageRef, ...] = Field(min_length=2, max_length=4)

    @field_validator("baseline_repository")
    @classmethod
    def require_normalized_repository(cls, value: str) -> str:
        if value.strip() != value:
            raise ValueError("Baseline repository must be normalized")
        return value

    @field_validator("allowed_overlay_roots")
    @classmethod
    def require_safe_overlay_roots(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _require_relative_posix_path(value, label="allowed Overlay root")
            for value in values
        )
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("allowed Overlay roots must be unique and canonically sorted")
        return normalized

    @model_validator(mode="after")
    def require_canonical_store_authority(self) -> BusinessCandidatePackageStoreDescriptor:
        package_hashes = tuple(
            item.candidate_source_hash
            for item in sorted(self.packages, key=lambda item: item.candidate_source_hash)
        )
        if tuple(item.candidate_source_hash for item in self.packages) != package_hashes:
            raise ValueError("Candidate Store packages must be canonically sorted")
        if len(package_hashes) != len(set(package_hashes)):
            raise ValueError("Candidate Store contains duplicate Candidate source identities")
        for replacement_point, mount_target in self.approved_mount_targets.items():
            if replacement_point.strip() != replacement_point:
                raise ValueError("approved replacement points must be normalized")
            path = PurePosixPath(mount_target)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("approved Overlay mount targets must be clean absolute paths")
        return self


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
    workload_id: str = Field(min_length=1, max_length=200)
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

    @field_validator(
        "source_package_store_id",
        "workload_id",
        "replacement_point",
        "reviewed_by",
    )
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


class VerifiedBusinessCandidatePackage(ContractModel):
    """One package identity independently reread from the C-owned Store."""

    candidate_id: UUID
    source_package_ref: CandidateSourcePackageRef
    overlay_file_path: str = Field(min_length=1, max_length=2000)
    overlay_content_hash: str = Field(pattern=SHA256_PATTERN)
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime

    @field_validator("overlay_file_path")
    @classmethod
    def require_safe_overlay_file(cls, value: str) -> str:
        return _require_relative_posix_path(value, label="verified Overlay file")

    @model_validator(mode="after")
    def require_review_time(self) -> VerifiedBusinessCandidatePackage:
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("Candidate Package review time must be timezone-aware")
        if self.reviewed_by.strip() != self.reviewed_by:
            raise ValueError("Candidate Package reviewer must be normalized")
        return self


class BusinessCandidateFamilyVerificationRecord(ContractModel):
    """No-HCU evidence produced after Store reread and Baseline source replay."""

    schema_version: Literal["m2a-business-candidate-family-verification-v1"] = (
        M2A_BUSINESS_CANDIDATE_FAMILY_VERIFICATION_SCHEMA_VERSION
    )
    store_descriptor_path: str = Field(min_length=1, max_length=1000)
    store_descriptor_hash: str = Field(pattern=SHA256_PATTERN)
    family_manifest_path: str = Field(min_length=1, max_length=1000)
    family_manifest_hash: str = Field(pattern=SHA256_PATTERN)
    family_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    source_family_hash: str = Field(pattern=SHA256_PATTERN)
    source_package_store_id: str = Field(min_length=1, max_length=300)
    source_package_store_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_repository: str = Field(min_length=1, max_length=2000)
    baseline_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    target_snapshot_id: UUID
    stage0_run_id: UUID
    baseline_epoch_id: UUID
    hotspot_id: UUID
    workload_id: str = Field(min_length=1, max_length=200)
    replacement_point: str = Field(min_length=1, max_length=1000)
    members: tuple[VerifiedBusinessCandidatePackage, ...] = Field(
        min_length=2,
        max_length=4,
    )
    verifier_provenance: AdapterProvenance
    verified_by: str = Field(min_length=1, max_length=200)
    verified_at: datetime
    decision: Literal["accepted_for_formal_window"] = "accepted_for_formal_window"
    baseline_source_replay_verified: Literal[True] = True
    synthetic: Literal[False] = False
    hcu_accessed: Literal[False] = False
    formal_round_created: Literal[False] = False
    performance_conclusion: Literal["not_measured"] = "not_measured"
    automatic_release_allowed: Literal[False] = False

    @field_validator("store_descriptor_path", "family_manifest_path")
    @classmethod
    def require_safe_evidence_paths(cls, value: str) -> str:
        return _require_relative_posix_path(value, label="Family verification input")

    @model_validator(mode="after")
    def require_independent_real_verification(self) -> BusinessCandidateFamilyVerificationRecord:
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("Candidate Family verification time must be timezone-aware")
        if self.verified_by.strip() != self.verified_by:
            raise ValueError("Candidate Family verifier must be normalized")
        if self.verifier_provenance.implementation_kind != "real":
            raise ValueError("Candidate Family acceptance requires a real Store verifier")
        candidate_ids = tuple(str(item.candidate_id) for item in self.members)
        content_hashes = tuple(item.overlay_content_hash for item in self.members)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("verified Candidate identities must be unique")
        if len(content_hashes) != len(set(content_hashes)):
            raise ValueError("verified Candidate source contents must be unique")
        return self
