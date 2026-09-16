# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Control-plane contracts for a bounded M1 endpoint validation run."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.platform_v1 import SHA256_PATTERN, AdapterProvenance
from hcuopt.measurement.endpoint_models import (
    EndpointArm,
    EndpointMeasurementPlan,
    EndpointWorkloadSpec,
    SignedM1EvidenceReference,
    endpoint_plan_hash,
)


class EndpointValidationRunCreate(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    signed_m1: SignedM1EvidenceReference
    workload: EndpointWorkloadSpec
    plan: EndpointMeasurementPlan
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    adapter_profile: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=300)

    @field_validator("signed_m1", mode="before")
    @classmethod
    def parse_signed_m1_wire_uuids(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        converted = dict(value)
        for name in (
            "task_id",
            "candidate_id",
            "baseline_epoch_id",
            "target_snapshot_id",
            "artifact_id",
            "evidence_bundle_id",
            "signoff_id",
        ):
            if isinstance(converted.get(name), str):
                converted[name] = UUID(converted[name])
        return converted

    @model_validator(mode="after")
    def bind_provisional_scope(self) -> EndpointValidationRunCreate:
        if self.plan.run_mode != "provisional":
            raise ValueError("the first endpoint control-plane run must be provisional")
        if self.workload.target_id != self.signed_m1.target_id:
            raise ValueError("endpoint workload differs from the signed M1 target")
        if tuple(self.plan.acquisition_order) != (
            "baseline",
            "candidate",
            "candidate",
            "baseline",
        ):
            raise ValueError("provisional endpoint run requires exactly one ABBA group")
        return self


class EndpointValidationRunView(ReadModel):
    endpoint_run_id: UUID
    task_id: UUID
    job_id: UUID
    signed_m1_task_id: UUID
    target_snapshot_id: UUID
    adapter_profile: str
    environment_fingerprint: str
    workload: EndpointWorkloadSpec
    plan: EndpointMeasurementPlan
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    state: Literal["queued", "running", "provisional_passed", "failed"]
    result: dict | None = None
    automatic_release_allowed: Literal[False] = False
    created_at: datetime
    updated_at: datetime


class EndpointAcquisitionResultRef(ContractModel):
    acquisition_ordinal: int = Field(ge=0, le=3)
    arm: EndpointArm
    evidence_uri: str = Field(min_length=1, max_length=4000)
    result_sha256: str = Field(pattern=SHA256_PATTERN)
    activation_sha256: str = Field(pattern=SHA256_PATTERN)
    cache_namespace_sha256: str = Field(pattern=SHA256_PATTERN)
    cleanup_succeeded: Literal[True] = True


class EndpointValidationJobResult(ContractModel):
    schema_version: Literal["bw20-endpoint-provisional-result-v1"] = (
        "bw20-endpoint-provisional-result-v1"
    )
    endpoint_run_id: UUID
    run_mode: Literal["provisional"] = "provisional"
    status: Literal["provisional_passed"] = "provisional_passed"
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    staging_receipt_hash: str = Field(pattern=SHA256_PATTERN)
    acquisitions: tuple[EndpointAcquisitionResultRef, ...]
    cleanup_evidence: dict
    adapter_provenance: tuple[AdapterProvenance, ...] = Field(min_length=1)
    producer_verdict: Literal[None] = None
    automatic_release_allowed: Literal[False] = False

    @field_validator("acquisitions", "adapter_provenance", mode="before")
    @classmethod
    def freeze_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_complete_provisional_abba(self) -> EndpointValidationJobResult:
        expected = tuple(enumerate(("baseline", "candidate", "candidate", "baseline")))
        actual = tuple((item.acquisition_ordinal, item.arm) for item in self.acquisitions)
        if actual != expected:
            raise ValueError("endpoint result requires one complete ordered ABBA group")
        hashes = [item.result_sha256 for item in self.acquisitions]
        if len(hashes) != len(set(hashes)):
            raise ValueError("endpoint acquisitions require distinct result evidence")
        fence = self.cleanup_evidence.get("fence")
        health = self.cleanup_evidence.get("health")
        if (
            not isinstance(fence, dict)
            or fence.get("fenced") is not True
            or not isinstance(health, dict)
            or health.get("healthy") is not True
        ):
            raise ValueError("endpoint result requires fenced cleanup and health evidence")
        if any(
            item.implementation_kind != "real"
            or item.capability != "endpoint_measurement_runner"
            for item in self.adapter_provenance
        ):
            raise ValueError("endpoint result requires the real measurement Runner")
        return self


def endpoint_create_plan_hash(request: EndpointValidationRunCreate) -> str:
    return endpoint_plan_hash(request.plan)


__all__ = [
    "EndpointAcquisitionResultRef",
    "EndpointValidationJobResult",
    "EndpointValidationRunCreate",
    "EndpointValidationRunView",
    "endpoint_create_plan_hash",
]
