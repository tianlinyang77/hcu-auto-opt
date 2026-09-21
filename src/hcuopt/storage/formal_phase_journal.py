# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Durable, non-retryable phase invocation journal for an already acquired claim."""

from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalPhaseExecutionReceiptRef,
    M2FormalPhaseExecutionRequest,
    m2_formal_execution_id_for,
    m2_formal_phase_execution_request_hash,
)
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.m2_formal_receipt import M2FormalPhaseExecutionReceiptStore
from hcuopt.operator.formal_dispatch import prepare_formal_round
from hcuopt.operator.formal_phase_binding import require_claimed_phase_binding
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalPhaseJournal:
    def __init__(
        self,
        claims: PostgresFormalClaimStore,
        intent_id: UUID,
        worker_id: str,
        claim_token: UUID,
        receipt_store: M2FormalPhaseExecutionReceiptStore,
    ) -> None:
        self.claims = claims
        self.intent_id = intent_id
        self.worker_id = worker_id
        self.claim_token = claim_token
        self.receipt_store = receipt_store

    def _locked_row(self, connection: Connection, request: M2FormalPhaseExecutionRequest):
        intent = self.claims._lock_deployment_intent(connection, self.intent_id)
        require_claimed_phase_binding(intent, request)
        row = connection.execute(
            "SELECT * FROM formal_phase_journal WHERE intent_id = %s AND candidate_id = %s "
            "AND phase = %s FOR UPDATE",
            (self.intent_id, request.binding.candidate_id, request.binding.phase.value),
        ).fetchone()
        if row is not None and (
            row["request_hash"] != m2_formal_phase_execution_request_hash(request)
            or row["execution_id"] != m2_formal_execution_id_for(request.binding)
            or row["worker_id"] != self.worker_id
            or row["claim_token"] != self.claim_token
        ):
            raise Conflict("Formal phase slot already binds another request or owner")
        return intent, row

    def begin(self, request: M2FormalPhaseExecutionRequest) -> tuple[dict[str, Any], bool]:
        if not self.claims.enabled or not self.claims.dispatcher.enabled:
            raise Conflict("Formal phase invocation is disabled")
        request = M2FormalPhaseExecutionRequest.model_validate(request.model_dump(mode="json"))
        dispatcher = self.claims.dispatcher
        repository = dispatcher.repository
        # A replay reads a fact, even after expiry. It never grants permission to call B again.
        with repository.connection() as connection:
            original, previous = self._locked_row(connection, request)
            if previous is not None:
                return dict(previous), False
        prepared = prepare_formal_round(dispatcher.coordinator, original, repository)
        if request.binding.candidate_family_hash != prepared.round.candidate_family_hash:
            raise Conflict("Formal phase candidate family differs from frozen intake")
        with repository.connection() as connection:
            current, previous = self._locked_row(connection, request)
            if previous is not None:
                return dict(previous), False
            if current != prepared.intent:
                raise Conflict("Formal Intent changed before phase journal publication")
            dispatcher._revalidate_locked(connection, prepared)
            # DB trigger enforces live ownership, window and stop absence under this same lock.
            _insert(
                connection,
                "formal_phase_journal",
                {
                    "execution_id": m2_formal_execution_id_for(request.binding),
                    "intent_id": self.intent_id,
                    "claim_token": self.claim_token,
                    "worker_id": self.worker_id,
                    "candidate_id": request.binding.candidate_id,
                    "phase": request.binding.phase.value,
                    "request_hash": m2_formal_phase_execution_request_hash(request),
                    "request": request.model_dump(mode="json"),
                },
            )
            _, row = self._locked_row(connection, request)
            assert row is not None
            return dict(row), True

    def record_receipt(
        self,
        request: M2FormalPhaseExecutionRequest,
        reference: M2FormalPhaseExecutionReceiptRef,
    ) -> None:
        if not self.claims.enabled:
            raise Conflict("Formal phase journal is disabled")
        self.receipt_store.load_for_request(reference, request)
        with self.claims.dispatcher.repository.connection() as connection:
            _, row = self._locked_row(connection, request)
            if row is None:
                raise Conflict("Formal phase was not journaled")
            payload = reference.model_dump(mode="json")
            if row["state"] == "receipt_recorded" and row["receipt_ref"] == payload:
                return
            claim = connection.execute(
                "SELECT state FROM formal_dispatch_claims WHERE intent_id = %s", (self.intent_id,)
            ).fetchone()
            if row["state"] != "invoking" or claim is None or claim["state"] != "claimed":
                raise Conflict("Formal phase requires recovery; late completion refused")
            connection.execute(
                "UPDATE formal_phase_journal SET state = 'receipt_recorded', receipt_ref = %s, "
                "finished_at = clock_timestamp() WHERE execution_id = %s",
                (Jsonb(payload), row["execution_id"]),
            )

    def mark_unknown(self, request: M2FormalPhaseExecutionRequest) -> None:
        if not self.claims.enabled:
            raise Conflict("Formal phase journal is disabled")
        with self.claims.dispatcher.repository.connection() as connection:
            _, row = self._locked_row(connection, request)
            if row is None:
                raise Conflict("Formal phase was not journaled")
            if row["state"] != "invoking":
                return
            connection.execute(
                "UPDATE formal_phase_journal SET state = 'recovery_required', "
                "finished_at = clock_timestamp() WHERE execution_id = %s",
                (row["execution_id"],),
            )
