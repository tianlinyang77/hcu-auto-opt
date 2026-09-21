# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bind the existing B adapter checkpoints to a durable Formal claim.

No execution entrypoint, new lease, automatic retry or resource release lives here.
The dispatcher's phase-attempt journal and production wiring remain separate.
"""

from dataclasses import dataclass
from uuid import UUID

from hcuopt.contracts.m2_formal_execution_v1 import M2FormalPhaseExecutionRequest
from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_claim import PostgresFormalClaimStore


@dataclass(frozen=True)
class FormalClaimCheckpoint:
    claims: PostgresFormalClaimStore
    intent_id: UUID
    worker_id: str
    claim_token: UUID

    def __call__(self, request: M2FormalPhaseExecutionRequest) -> None:
        intent = self.claims.dispatcher.repository.get_formal_start_intent(self.intent_id)
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
        self.claims.assert_active(self.intent_id, self.worker_id, self.claim_token)
