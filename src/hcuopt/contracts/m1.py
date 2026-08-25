# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import ManualCandidateKind


class CandidateOverlayFile(ContractModel):
    """One reviewed Python/Triton file supplied by the deployment-owned intake."""

    path: str = Field(min_length=1, max_length=2000)
    content_hash: str = Field(pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def require_safe_relative_posix_path(cls, value: str) -> str:
        parts = value.split("/")
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("overlay path must be a normalized relative POSIX path")
        if not value.endswith((".py", ".pyi")):
            raise ValueError("M1 startup Overlay accepts only Python/Triton source files")
        return value


class CandidateSourcePackageManifest(ContractModel):
    """Reviewed input loaded only from a deployment-owned content-addressed root."""

    schema_version: Literal["m1-candidate-source-v1"] = "m1-candidate-source-v1"
    candidate_id: UUID
    hotspot_id: UUID
    baseline_source_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_source_hash: str = Field(pattern=SHA256_PATTERN)
    replacement_point: str = Field(min_length=1, max_length=1000)
    candidate_kind: ManualCandidateKind
    overlay_mount_target: str = Field(min_length=1, max_length=2000)
    files: list[CandidateOverlayFile] = Field(min_length=1, max_length=1)
    profiler_evidence_uri: str = Field(min_length=1)
    profiler_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime

    @field_validator("overlay_mount_target")
    @classmethod
    def require_absolute_overlay_target(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("overlay_mount_target must be a clean absolute POSIX path")
        return value

    @model_validator(mode="after")
    def require_unique_files_and_real_change(self) -> CandidateSourcePackageManifest:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("candidate source package contains duplicate overlay paths")
        if self.candidate_source_hash == self.baseline_source_hash:
            raise ValueError("M1 Candidate source must differ from its Baseline")
        return self
