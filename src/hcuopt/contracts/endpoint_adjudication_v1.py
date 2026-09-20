# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Contracts for independent formal adjudication of an endpoint campaign."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.endpoint_control_v1 import EndpointAcquisitionResultRef
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.measurement.endpoint_models import (
    EndpointMeasurementPlan,
    EndpointWorkloadSpec,
    SignedM1EvidenceReference,
    endpoint_plan_hash,
)
from hcuopt.measurement.evidence import canonical_json_bytes


class EndpointAdjudicationGroupRef(ContractModel):
    group_ordinal: int = Field(ge=0, le=7)
    endpoint_run_id: UUID
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    acquisitions: tuple[EndpointAcquisitionResultRef, ...]

    @field_validator("acquisitions", mode="before")
    @classmethod
    def freeze_acquisitions(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def require_complete_group(self) -> EndpointAdjudicationGroupRef:
        expected = tuple(enumerate(("baseline", "candidate", "candidate", "baseline")))
        actual = tuple((item.acquisition_ordinal, item.arm) for item in self.acquisitions)
        if actual != expected:
            raise ValueError("endpoint adjudication group requires one complete ordered ABBA block")
        if len({item.evidence_uri for item in self.acquisitions}) != 4:
            raise ValueError("endpoint adjudication acquisitions require distinct evidence URIs")
        return self


class EndpointFormalAdjudicationRequest(ContractModel):
    schema_version: Literal["endpoint-formal-adjudication-request-v1"] = (
        "endpoint-formal-adjudication-request-v1"
    )
    campaign_id: UUID
    signed_m1: SignedM1EvidenceReference
    workload: EndpointWorkloadSpec
    group_plan: EndpointMeasurementPlan
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    baseline_module_hash: str = Field(pattern=SHA256_PATTERN)
    groups: tuple[EndpointAdjudicationGroupRef, ...]
    raw_evidence_manifest_uri: str = Field(min_length=1, max_length=4000)
    raw_evidence_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    confidence_level: Literal[0.95] = 0.95
    producer_verdict: Literal[None] = None
    automatic_release_allowed: Literal[False] = False

    @field_validator("groups", mode="before")
    @classmethod
    def freeze_groups(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def bind_frozen_campaign(self) -> EndpointFormalAdjudicationRequest:
        if self.workload.target_id != self.signed_m1.target_id:
            raise ValueError("endpoint adjudication workload differs from the signed M1 target")
        if self.group_plan.run_mode != "provisional" or tuple(
            self.group_plan.acquisition_order
        ) != ("baseline", "candidate", "candidate", "baseline"):
            raise ValueError("endpoint adjudication v1 consumes frozen provisional ABBA groups")
        if len(self.groups) != 8:
            raise ValueError("endpoint formal adjudication v1 requires exactly eight ABBA groups")
        if tuple(item.group_ordinal for item in self.groups) != tuple(range(8)):
            raise ValueError("endpoint adjudication group ordinals must be contiguous")
        if len({item.endpoint_run_id for item in self.groups}) != 8:
            raise ValueError("endpoint adjudication requires eight independent endpoint Runs")
        if len({item.plan_hash for item in self.groups}) != 1:
            raise ValueError("endpoint adjudication groups must share one frozen plan")
        if self.groups[0].plan_hash != endpoint_plan_hash(self.group_plan):
            raise ValueError("endpoint adjudication group Hash differs from the frozen plan")
        acquisitions = [item for group in self.groups for item in group.acquisitions]
        if len({item.evidence_uri for item in acquisitions}) != 32:
            raise ValueError("endpoint adjudication requires 32 distinct evidence URIs")
        if len({item.cache_namespace_sha256 for item in acquisitions}) != 32:
            raise ValueError("endpoint adjudication requires 32 distinct cache namespaces")
        return self


class EndpointCampaignCreate(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    endpoint_run_ids: tuple[UUID, ...]
    raw_evidence_manifest_uri: str = Field(min_length=1, max_length=4000)
    raw_evidence_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    automatic_release_allowed: Literal[False] = False

    @field_validator("endpoint_run_ids", mode="before")
    @classmethod
    def freeze_run_ids(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(UUID(item) if isinstance(item, str) else item for item in value)
        return value

    @model_validator(mode="after")
    def require_eight_independent_groups(self) -> EndpointCampaignCreate:
        if len(self.endpoint_run_ids) != 8 or len(set(self.endpoint_run_ids)) != 8:
            raise ValueError("endpoint campaign requires eight distinct endpoint Runs")
        parsed = urlparse(self.raw_evidence_manifest_uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("endpoint campaign manifest must be a local file URI")
        return self


class EndpointAdjudicationGroupResult(ContractModel):
    group_ordinal: int = Field(ge=0, le=7)
    baseline_mean_ns: float = Field(gt=0)
    candidate_mean_ns: float = Field(gt=0)
    log_ratio: float
    acquisition_means_ns: tuple[float, float, float, float]

    @field_validator("acquisition_means_ns", mode="before")
    @classmethod
    def freeze_means(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class EndpointFormalAdjudicationResult(ReadModel):
    schema_version: Literal["endpoint-formal-adjudication-result-v1"] = (
        "endpoint-formal-adjudication-result-v1"
    )
    campaign_id: UUID
    verdict: Literal["faster", "slower", "inconclusive", "invalid"]
    reason: str = Field(min_length=1, max_length=1000)
    successful_groups: int = Field(ge=0, le=8)
    measured_requests: int = Field(ge=0)
    baseline_mean_ns: float | None = Field(default=None, gt=0)
    candidate_mean_ns: float | None = Field(default=None, gt=0)
    paired_latency_reduction_percent: float | None = None
    confidence_interval_percent: tuple[float, float] | None = None
    groups: tuple[EndpointAdjudicationGroupResult, ...] = ()
    verified_file_count: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    formal_d_adjudication: Literal[True] = True
    automatic_release_allowed: Literal[False] = False

    @field_validator("groups", mode="before")
    @classmethod
    def freeze_group_results(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def bind_valid_and_invalid_shapes(self) -> EndpointFormalAdjudicationResult:
        statistics = (
            self.baseline_mean_ns,
            self.candidate_mean_ns,
            self.paired_latency_reduction_percent,
            self.confidence_interval_percent,
        )
        if self.verdict == "invalid":
            if any(item is not None for item in statistics) or self.groups:
                raise ValueError("invalid endpoint adjudication cannot publish statistics")
        elif (
            any(item is None for item in statistics)
            or self.successful_groups != 8
            or len(self.groups) != 8
        ):
            raise ValueError("valid endpoint adjudication requires all frozen statistics")
        return self


class EndpointCampaignView(ReadModel):
    campaign_id: UUID
    name: str
    signed_m1_task_id: UUID
    target_snapshot_id: UUID
    adapter_profile: str
    environment_fingerprint: str = Field(pattern=SHA256_PATTERN)
    endpoint_run_ids: tuple[UUID, ...]
    adjudication_job_id: UUID | None = None
    state: Literal[
        "awaiting_adjudication",
        "adjudicating",
        "adjudication_failed",
        "awaiting_signoff",
        "completed",
        "rejected",
        "invalid",
    ]
    adjudication_request: EndpointFormalAdjudicationRequest
    adjudication_result: EndpointFormalAdjudicationResult | None = None
    automatic_release_allowed: Literal[False] = False
    created_at: datetime
    updated_at: datetime

    @field_validator("endpoint_run_ids", mode="before")
    @classmethod
    def freeze_view_run_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class EndpointCampaignSignoffRequest(ContractModel):
    decision: Literal["accepted", "rejected"]
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    adjudication_result_sha256: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    automatic_release_allowed: Literal[False] = False


class EndpointCampaignSignoffView(ReadModel):
    signoff_id: UUID
    campaign_id: UUID
    decision: Literal["accepted", "rejected"]
    actor: str
    reason: str
    adjudication_result_sha256: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str
    campaign_state: Literal["completed", "rejected"]
    automatic_release_allowed: Literal[False] = False
    created_at: datetime


def endpoint_adjudication_result_hash(
    result: EndpointFormalAdjudicationResult | dict,
) -> str:
    materialized = (
        result.model_dump(mode="json")
        if isinstance(result, EndpointFormalAdjudicationResult)
        else EndpointFormalAdjudicationResult.model_validate(result).model_dump(mode="json")
    )
    return "sha256:" + hashlib.sha256(canonical_json_bytes(materialized)).hexdigest()


__all__ = [
    "EndpointAdjudicationGroupRef",
    "EndpointAdjudicationGroupResult",
    "EndpointCampaignCreate",
    "EndpointCampaignSignoffRequest",
    "EndpointCampaignSignoffView",
    "EndpointCampaignView",
    "EndpointFormalAdjudicationRequest",
    "EndpointFormalAdjudicationResult",
    "endpoint_adjudication_result_hash",
]
