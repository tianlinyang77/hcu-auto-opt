# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from hcuopt.contracts.m2_formal_execution_v1 import M2FormalPhaseExecutionRequest
from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentView
from hcuopt.domain.errors import Conflict


def require_claimed_phase_binding(
    intent: FormalStartIntentView, request: M2FormalPhaseExecutionRequest
) -> None:
    binding = request.binding
    if (
        binding.task_id != intent.task_id
        or binding.round_id != intent.round_id
        or binding.resolved_plan_hash != intent.resolved_plan_hash
        or binding.formal_authorization_hash != intent.formal_authorization_hash
        or not any(
            member.candidate_id == binding.candidate_id
            and member.round_candidate_id == binding.round_candidate_id
            for member in intent.candidate_bindings
        )
    ):
        raise Conflict("Formal phase differs from its claimed Intent")
