# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from typing import Literal
from uuid import UUID

from pydantic import Field

from hcuopt.contracts.m2_formal_start_v1 import FrozenFormalStartModel
from hcuopt.contracts.operator_v1 import OperatorServiceIdentity
from hcuopt.contracts.platform_v1 import SHA256_PATTERN


class FormalRecoveryReport(FrozenFormalStartModel):
    schema_version: Literal["formal-correctness-recovery-v1"]
    job_id: UUID
    input_hash: str = Field(pattern=SHA256_PATTERN)
    snapshot_hash: str = Field(pattern=SHA256_PATTERN)
    job_state: str
    status: Literal[
        "not_invoked", "invocation_unresolved", "unknown_requires_manual_recovery",
        "result_ready", "settlement_pending", "completed", "inconsistent",
        "ownership_or_budget_conflict",
    ]
    resource_id: str
    resource_state: str
    resource_owned_by_attempt: bool
    budget_state: str
    reservation_id: UUID | None
    recorded_result: bool
    release_recorded: bool
    reconciliation_allowed: bool
    execution_retry_allowed: Literal[False]
    automatic_release_allowed: Literal[False]
    required_manual_checks: tuple[str, ...]


class FormalRecoveryObservation(FrozenFormalStartModel):
    schema_version: Literal["formal-recovery-observation-v1"] = "formal-recovery-observation-v1"
    intent_id: UUID
    round_id: UUID
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    service_identity: OperatorServiceIdentity
    report: FormalRecoveryReport
    web_reconciliation_allowed: Literal[False] = False
