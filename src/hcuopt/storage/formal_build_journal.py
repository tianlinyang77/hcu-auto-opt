# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Durable build invocation slot. Recording output is not publishing a built candidate."""

import re
from uuid import UUID

from psycopg.types.json import Jsonb

from hcuopt.adapters.formal_candidate_builder import FormalCandidateBuildResult
from hcuopt.contracts.m2 import RoundCandidateBuildTerminal
from hcuopt.contracts.v1 import ManualCandidateBuildResult
from hcuopt.domain.errors import Conflict
from hcuopt.operator.formal_dispatch import prepare_formal_round
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalBuildJournal:
    """Requires an existing real budget reservation; does not invent Job identities.

    Success output may be retained after claim expiry or stop for recovery, but
    only FormalBuildStore can publish it after revalidating current authority.
    Unknown failures keep their reservation outstanding until reconciled.
    """

    def __init__(self, claims: PostgresFormalClaimStore, intent_id: UUID,
                 worker_id: str, claim_token: UUID):
        self.claims, self.intent_id = claims, intent_id
        self.worker_id, self.claim_token = worker_id, claim_token

    def _locked(self, conn, candidate_id, reservation_id, input_hash):
        if not self.claims.enabled or not self.claims.dispatcher.enabled:
            raise Conflict("Formal build journal is disabled")
        if not isinstance(input_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", input_hash):
            raise ValueError("Formal build input Hash must be SHA256")
        intent = self.claims._lock_deployment_intent(conn, self.intent_id)
        if not any(c.candidate_id == candidate_id for c in intent.candidate_bindings):
            raise Conflict("Formal build candidate is outside claimed Intent")
        row = conn.execute(
            "SELECT * FROM formal_build_journal WHERE intent_id = %s AND candidate_id = %s "
            "FOR UPDATE", (self.intent_id, candidate_id),
        ).fetchone()
        if row is not None and (
            row["worker_id"] != self.worker_id or row["claim_token"] != self.claim_token
            or row["reservation_id"] != reservation_id or row["input_hash"] != input_hash
        ):
            raise Conflict("Formal build slot already binds another input or owner")
        return intent, row

    def begin(self, candidate_id: UUID, reservation_id: UUID, input_hash: str) -> tuple[dict, bool]:
        repo = self.claims.dispatcher.repository
        with repo.connection() as conn:
            original, previous = self._locked(conn, candidate_id, reservation_id, input_hash)
            if previous is not None:
                return dict(previous), False
        prepared = prepare_formal_round(self.claims.dispatcher.coordinator, original, repo)
        with repo.connection() as conn:
            current, previous = self._locked(conn, candidate_id, reservation_id, input_hash)
            if previous is not None:
                return dict(previous), False
            if current != prepared.intent:
                raise Conflict("Formal build Intent changed before invocation")
            self.claims.dispatcher._revalidate_locked(conn, prepared)
            # Hold the budget row through the journal insert. A concurrent finalize
            # cannot turn a checked reservation into released credit before begin.
            budget = conn.execute(
                "SELECT * FROM round_budget_reservations WHERE reservation_id = %s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            if budget is None or (
                budget["round_id"] != current.round_id or budget["candidate_id"] != candidate_id
                or budget["state"] != "reserved" or budget["phase"] is not None
                or budget["planned"].get("build_attempts") != 1
            ):
                raise Conflict("Formal build requires its reserved build budget")
            _insert(conn, "formal_build_journal", {
                "intent_id": self.intent_id, "candidate_id": candidate_id,
                "worker_id": self.worker_id, "claim_token": self.claim_token,
                "reservation_id": reservation_id, "input_hash": input_hash,
            })
            _, row = self._locked(conn, candidate_id, reservation_id, input_hash)
            return dict(row), True

    def record_result(self, candidate_id: UUID, reservation_id: UUID, input_hash: str,
                      result: FormalCandidateBuildResult) -> None:
        build = ManualCandidateBuildResult.model_validate(result.build.model_dump(mode="json"))
        terminal = RoundCandidateBuildTerminal.model_validate(
            result.terminal.model_dump(mode="json")
        )
        if (build.candidate_id != candidate_id or terminal.candidate_id != candidate_id
                or terminal.state != "built" or build.artifact.synthetic
                or terminal.artifact_id != build.artifact.artifact_id
                or terminal.artifact_hash != build.artifact.content_hash):
            raise Conflict("Formal build journal result binding differs")
        payload = {"build": build.model_dump(mode="json"),
                   "terminal": terminal.model_dump(mode="json")}
        with self.claims.dispatcher.repository.connection() as conn:
            intent, row = self._locked(conn, candidate_id, reservation_id, input_hash)
            if terminal.round_id != intent.round_id or not any(
                c.candidate_id == candidate_id
                and c.round_candidate_id == terminal.round_candidate_id
                for c in intent.candidate_bindings
            ):
                raise Conflict("Formal build result is outside claimed Round")
            if row is not None and row["state"] == "result_recorded" and row["result"] == payload:
                return
            if row is None or row["state"] != "invoking":
                raise Conflict("Formal build result requires an invoking slot")
            conn.execute(
                "UPDATE formal_build_journal SET state = 'result_recorded', result = %s, "
                "finished_at = clock_timestamp() WHERE intent_id = %s AND candidate_id = %s",
                (Jsonb(payload), self.intent_id, candidate_id),
            )

    def mark_unknown(self, candidate_id: UUID, reservation_id: UUID, input_hash: str) -> None:
        with self.claims.dispatcher.repository.connection() as conn:
            _, row = self._locked(conn, candidate_id, reservation_id, input_hash)
            if row is None:
                raise Conflict("Formal build was not journaled")
            if row["state"] == "invoking":
                conn.execute(
                    "UPDATE formal_build_journal SET state = 'recovery_required', "
                    "finished_at = clock_timestamp() WHERE intent_id = %s AND candidate_id = %s",
                    (self.intent_id, candidate_id),
                )
