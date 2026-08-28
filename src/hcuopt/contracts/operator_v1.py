# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from hcuopt.contracts.base import ContractModel, ReadModel
from hcuopt.contracts.m2 import RoundBudget
from hcuopt.contracts.platform_v1 import GIT_COMMIT_PATTERN, SHA256_PATTERN
from hcuopt.domain.enums import SearchRoundRunMode

M2_OPERATOR_CONTRACT_VERSION = "m2-operator-v1"


class OperatorServiceIdentity(ReadModel):
    source_commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    control_contract_version: str = Field(min_length=1, max_length=100)
    operator_contract_version: Literal["m2-operator-v1"] = M2_OPERATOR_CONTRACT_VERSION
    profile_catalog_hash: str = Field(pattern=SHA256_PATTERN)
    server_instance_id: UUID


class TargetOperatorProfileRefs(ContractModel):
    target_id: str = Field(min_length=1, max_length=200)
    target_spec_hash: str = Field(pattern=SHA256_PATTERN)
    adapter_profile: str = Field(min_length=1, max_length=200)
    resource_policy_id: str = Field(min_length=1, max_length=200)
    resource_policy_hash: str = Field(pattern=SHA256_PATTERN)
    candidate_package_store_id: str = Field(min_length=1, max_length=200)
    candidate_package_store_version: int = Field(ge=1)
    candidate_package_store_hash: str = Field(pattern=SHA256_PATTERN)
    required_stage0_protocol_hash: str = Field(pattern=SHA256_PATTERN)


class WorkloadOperatorProfileRefs(ContractModel):
    workload_id: str = Field(min_length=1, max_length=200)
    workload_hash: str = Field(pattern=SHA256_PATTERN)
    configuration_hash: str = Field(pattern=SHA256_PATTERN)
    dataset_uri: str = Field(min_length=1, max_length=2000)
    dataset_hash: str = Field(pattern=SHA256_PATTERN)
    model_uri: str = Field(min_length=1, max_length=2000)
    model_hash: str = Field(pattern=SHA256_PATTERN)
    hotspot_scope_id: str = Field(min_length=1, max_length=200)
    hotspot_scope_hash: str = Field(pattern=SHA256_PATTERN)
    baseline_selection_policy: Literal["latest_frozen_matching"]


class MeasurementOperatorProfileRefs(ContractModel):
    search_protocol_version: str = Field(min_length=1, max_length=200)
    search_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    holdout_protocol_version: str = Field(min_length=1, max_length=200)
    holdout_protocol_hash: str = Field(pattern=SHA256_PATTERN)
    selection_rule_hash: str = Field(pattern=SHA256_PATTERN)
    budget: RoundBudget
    conclusion_boundary: Literal["explore", "standard", "formal"]


OperatorProfileAuthorityRefs: TypeAlias = (
    TargetOperatorProfileRefs
    | WorkloadOperatorProfileRefs
    | MeasurementOperatorProfileRefs
)


class OperatorProfileContent(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    profile_version: int = Field(ge=1)
    profile_kind: Literal["target", "workload", "measurement"]
    state: Literal["active", "deprecated", "revoked"]
    allowed_run_modes: tuple[SearchRoundRunMode, ...] = Field(min_length=1, max_length=2)
    display_name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    authority_refs: OperatorProfileAuthorityRefs
    synthetic: bool
    created_at: datetime

    @model_validator(mode="after")
    def require_canonical_kind_and_mode(self) -> OperatorProfileContent:
        expected = {
            "target": TargetOperatorProfileRefs,
            "workload": WorkloadOperatorProfileRefs,
            "measurement": MeasurementOperatorProfileRefs,
        }[self.profile_kind]
        if not isinstance(self.authority_refs, expected):
            raise ValueError("profile_kind does not match authority_refs")
        mode_values = tuple(mode.value for mode in self.allowed_run_modes)
        if len(set(mode_values)) != len(mode_values) or mode_values != tuple(
            sorted(mode_values)
        ):
            raise ValueError("allowed_run_modes must be unique and canonically sorted")
        if self.synthetic and self.allowed_run_modes != (SearchRoundRunMode.SCRIPTED,):
            raise ValueError("synthetic Operator Profile may allow only scripted")
        if not self.synthetic and SearchRoundRunMode.SCRIPTED in self.allowed_run_modes:
            raise ValueError("scripted mode requires a synthetic Operator Profile")
        return self


class OperatorProfileDescriptor(OperatorProfileContent, ReadModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    profile_hash: str = Field(pattern=SHA256_PATTERN)


class OperatorProfileRef(ContractModel):
    profile_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    profile_version: int = Field(ge=1)
    profile_kind: Literal["target", "workload", "measurement"]
    profile_hash: str = Field(pattern=SHA256_PATTERN)
