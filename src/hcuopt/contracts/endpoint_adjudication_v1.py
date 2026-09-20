# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Contracts for independent formal adjudication of an endpoint campaign."""

from __future__ import annotations

from typing import Literal
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


__all__ = [
    "EndpointAdjudicationGroupRef",
    "EndpointAdjudicationGroupResult",
    "EndpointFormalAdjudicationRequest",
    "EndpointFormalAdjudicationResult",
]
