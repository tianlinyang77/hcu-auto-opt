# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import RoundPhase
from hcuopt.measurement.models import INT64_MAX, StrictMeasurementModel

M2_ROUND_MEASUREMENT_REF_SCHEMA_VERSION = "m2a-round-measurement-ref-v1"
M1_KERNEL_PERFORMANCE_EVIDENCE_SCHEMA_VERSION = (
    "m1-kernel-performance-evidence-v1"
)


class RoundMeasurementRef(StrictMeasurementModel):
    """Immutable B-line reference to one complete M1 comparison evidence file."""

    schema_version: Literal["m2a-round-measurement-ref-v1"] = (
        M2_ROUND_MEASUREMENT_REF_SCHEMA_VERSION
    )
    round_measurement_ref_id: UUID
    round_id: UUID
    round_candidate_id: UUID
    candidate_id: UUID
    phase: RoundPhase
    candidate_family_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_family_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_family_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    artifact_id: UUID
    artifact_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_id: UUID
    evidence_schema_version: Literal["m1-kernel-performance-evidence-v1"] = (
        M1_KERNEL_PERFORMANCE_EVIDENCE_SCHEMA_VERSION
    )
    raw_evidence_uri: str = Field(min_length=1, max_length=4000)
    raw_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    measurement_plan_hash: str = Field(pattern=SHA256_PATTERN)
    phase_plan_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_reveal_evidence_hash: str | None = Field(
        default=None, pattern=SHA256_PATTERN
    )
    baseline_sample_set_hash: str = Field(pattern=SHA256_PATTERN)
    process_identity_set_hash: str = Field(pattern=SHA256_PATTERN)
    cache_namespace_set_hash: str = Field(pattern=SHA256_PATTERN)
    lease_id: UUID
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1, le=INT64_MAX)
    status: Literal["measured"] = "measured"
    synthetic: Literal[False] = False
    created_at: datetime

    @model_validator(mode="after")
    def require_phase_isolation(self) -> RoundMeasurementRef:
        holdout_only = (
            self.holdout_family_hash,
            self.holdout_reveal_evidence_hash,
        )
        if self.phase is RoundPhase.SEARCH and any(
            value is not None for value in holdout_only
        ):
            raise ValueError("Search Measurement Ref cannot bind Holdout authorities")
        if self.phase is RoundPhase.HOLDOUT and not all(
            value is not None for value in holdout_only
        ):
            raise ValueError(
                "Holdout Measurement Ref requires Family and Reveal evidence"
            )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Round Measurement Ref created_at must be timezone-aware")
        return self


def validate_round_measurement_refs(
    references: Iterable[RoundMeasurementRef],
) -> tuple[RoundMeasurementRef, ...]:
    """Validate one authority view without inventing measurement or verdict state."""

    items = tuple(
        sorted(
            references,
            key=lambda item: (
                str(item.round_id),
                item.phase.value,
                str(item.candidate_id),
            ),
        )
    )
    per_round_families: dict[UUID, tuple[str, str]] = {}
    seen_member_phases: set[tuple[UUID, UUID, RoundPhase]] = set()
    unique_values: dict[str, set[object]] = {
        "round_measurement_ref_id": set(),
        "measurement_id": set(),
        "raw_evidence_uri": set(),
        "raw_evidence_hash": set(),
        "baseline_sample_set_hash": set(),
        "process_identity_set_hash": set(),
        "cache_namespace_set_hash": set(),
    }

    for item in items:
        families = (item.candidate_family_hash, item.artifact_family_hash)
        existing_families = per_round_families.setdefault(item.round_id, families)
        if existing_families != families:
            raise ValueError("Round Measurement Refs bind different frozen families")

        member_phase = (item.round_id, item.candidate_id, item.phase)
        if member_phase in seen_member_phases:
            raise ValueError("Candidate Phase already has a Round Measurement Ref")
        seen_member_phases.add(member_phase)

        for field_name, values in unique_values.items():
            value = getattr(item, field_name)
            if value in values:
                raise ValueError(
                    f"Round Measurement Ref reuses {field_name} across Candidate or Phase"
                )
            values.add(value)
    return items
