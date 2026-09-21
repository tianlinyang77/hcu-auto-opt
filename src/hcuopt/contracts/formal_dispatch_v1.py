# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from typing import Literal
from uuid import UUID

from pydantic import Field

from hcuopt.contracts.m2_formal_start_v1 import FrozenFormalStartModel
from hcuopt.contracts.operator_v1 import OperatorServiceIdentity
from hcuopt.contracts.platform_v1 import SHA256_PATTERN


class FormalDispatchStatus(FrozenFormalStartModel):
    """Creation-outbox observation only, not a physical execution receipt."""

    schema_version: Literal["formal-dispatch-status-v1"] = "formal-dispatch-status-v1"
    intent_id: UUID
    round_id: UUID
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    state: Literal[
        "not_created", "queued", "cancelled", "claimed", "recovery_required", "stop_requested"
    ]
    service_identity: OperatorServiceIdentity
    execution_consumer_enabled: Literal[False] = False
    automatic_release_allowed: Literal[False] = False
