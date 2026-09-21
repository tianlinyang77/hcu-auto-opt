# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""One-shot control-plane claim. No physical executor or automatic retry."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection

from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentView
from hcuopt.domain.errors import Conflict
from hcuopt.operator.formal_dispatch import prepare_formal_round
from hcuopt.storage.formal_dispatch import PostgresFormalDispatcher, _insert


class PostgresFormalClaimStore:
    """Default-disabled ownership; a claim is not a B resource lease/fence.

    There is deliberately no release, requeue, renewal or completion API yet.
    Lost responses and expired claims require recovery, not another execution.
    """

    def __init__(self, dispatcher: PostgresFormalDispatcher, *, enabled: bool = False):
        self.dispatcher = dispatcher
        self.enabled = enabled

    def claim(self, intent_id: UUID, worker_id: str, *, ttl_seconds: int = 60) -> dict[str, Any]:
        if not self.enabled or not self.dispatcher.enabled:
            raise Conflict("Formal claiming is disabled")
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1-128 characters")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("claim TTL must be 1-300 seconds")
        dispatcher = self.dispatcher
        repository = dispatcher.repository
        original = repository.get_formal_start_intent(intent_id)
        if original.service_identity != dispatcher.coordinator.service_identity:
            raise Conflict("Formal claim belongs to another deployment")
        prepared = prepare_formal_round(dispatcher.coordinator, original, repository)
        with repository.connection() as connection:
            row = connection.execute(
                "SELECT * FROM formal_operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None or repository._formal_start_intent(row) != prepared.intent:
                raise Conflict("Formal Intent changed before claim")
            dispatch = connection.execute(
                "SELECT * FROM formal_round_dispatches WHERE intent_id = %s", (intent_id,)
            ).fetchone()
            if dispatch is None or dispatch["state"] != "queued":
                raise Conflict("Formal dispatch is not queued")
            if (
                dispatch["round_id"] != original.round_id
                or dispatch["request_digest"] != original.request_digest
                or dispatch["resolved_plan_hash"] != original.resolved_plan_hash
                or dispatch["candidate_family_hash"] != prepared.round.candidate_family_hash
                or dispatch["service_identity"] != original.service_identity.model_dump(mode="json")
            ):
                raise Conflict("Formal dispatch bindings differ")
            if connection.execute(
                "SELECT 1 FROM formal_dispatch_claims WHERE intent_id = %s", (intent_id,)
            ).fetchone():
                raise Conflict(
                    "Formal dispatch already claimed; recovery required before any retry"
                )
            dispatcher._revalidate_locked(connection, prepared)
            now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            expires = min(
                now + timedelta(seconds=ttl_seconds), prepared.valid_until, dispatch["valid_until"]
            )
            _insert(
                connection,
                "formal_dispatch_claims",
                {
                    "intent_id": intent_id,
                    "claim_token": uuid4(),
                    "worker_id": worker_id,
                    "claimed_at": now,
                    "expires_at": expires,
                },
            )
            _insert(
                connection,
                "formal_dispatch_claim_events",
                {
                    "intent_id": intent_id,
                    "event_type": "claimed",
                },
            )
            return dict(
                connection.execute(
                    "SELECT * FROM formal_dispatch_claims WHERE intent_id = %s", (intent_id,)
                ).fetchone()
            )

    def mark_expired(self, intent_id: UUID) -> bool:
        """Persist timeout for this deployment; never release resources or requeue."""
        if not self.enabled:
            raise Conflict("Formal claiming is disabled")
        repository = self.dispatcher.repository
        with repository.connection() as connection:
            row = connection.execute(
                "SELECT * FROM formal_operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None or repository._formal_start_intent(row).service_identity != (
                self.dispatcher.coordinator.service_identity
            ):
                raise Conflict("Formal claim belongs to another deployment or is unavailable")
            changed = connection.execute(
                "UPDATE formal_dispatch_claims SET state = 'recovery_required', "
                "recovery_at = clock_timestamp() WHERE intent_id = %s AND state = 'claimed' "
                "AND expires_at <= clock_timestamp() RETURNING intent_id",
                (intent_id,),
            ).fetchone()
            if changed is None:
                return False
            _insert(
                connection,
                "formal_dispatch_claim_events",
                {
                    "intent_id": intent_id,
                    "event_type": "recovery_required",
                },
            )
            return True

    def request_stop(
        self,
        intent_id: UUID,
        *,
        requested_by: str,
        reason: str = "operator_request",
    ) -> dict[str, Any]:
        """Trusted deployment operation, not a Web capability or cleanup action.

        Capture the owner from storage, never from an untrusted request. Replays
        return the original fact; changed actor/reason conflicts rather than
        rewriting audit history. This remains available after claim expiry.
        """
        if not self.enabled:
            raise Conflict("Formal claiming is disabled")
        if not isinstance(requested_by, str) or not requested_by.strip() or len(requested_by) > 128:
            raise ValueError("requested_by must contain 1-128 characters")
        if reason not in {"operator_request", "deployment_shutdown"}:
            raise ValueError("unsupported Formal stop reason")
        repository = self.dispatcher.repository
        with repository.connection() as connection:
            self._lock_deployment_intent(connection, intent_id)
            previous = connection.execute(
                "SELECT * FROM formal_dispatch_stop_requests WHERE intent_id = %s", (intent_id,)
            ).fetchone()
            if previous is not None:
                if previous["requested_by"] != requested_by or previous["reason"] != reason:
                    raise Conflict("Formal stop request differs from its recorded audit")
                return dict(previous)
            claim = connection.execute(
                "SELECT * FROM formal_dispatch_claims WHERE intent_id = %s", (intent_id,)
            ).fetchone()
            if claim is None:
                raise Conflict("Unclaimed Formal intent must use queued cancellation")
            _insert(
                connection,
                "formal_dispatch_stop_requests",
                {
                    "intent_id": intent_id,
                    "claim_token": claim["claim_token"],
                    "worker_id": claim["worker_id"],
                    "requested_by": requested_by,
                    "reason": reason,
                },
            )
            return dict(
                connection.execute(
                    "SELECT * FROM formal_dispatch_stop_requests WHERE intent_id = %s", (intent_id,)
                ).fetchone()
            )

    def assert_active(self, intent_id: UUID, worker_id: str, claim_token: UUID) -> None:
        """Checkpoint only: does not authorize or atomically start external work.

        Recheck before each phase. A stop after this check still needs cooperative
        cancellation and B fencing; this method cannot close that external race.
        """
        dispatcher = self.dispatcher
        if not self.enabled or not dispatcher.enabled:
            raise Conflict("Formal claiming is disabled")
        repository = dispatcher.repository
        original = repository.get_formal_start_intent(intent_id)
        if original.service_identity != dispatcher.coordinator.service_identity:
            raise Conflict("Formal claim belongs to another deployment")
        prepared = prepare_formal_round(dispatcher.coordinator, original, repository)
        with repository.connection() as connection:
            current = self._lock_deployment_intent(connection, intent_id)
            if current != prepared.intent:
                raise Conflict("Formal Intent changed before checkpoint")
            claim = connection.execute(
                "SELECT * FROM formal_dispatch_claims WHERE intent_id = %s", (intent_id,)
            ).fetchone()
            if (
                claim is None
                or claim["worker_id"] != worker_id
                or claim["claim_token"] != claim_token
                or claim["state"] != "claimed"
            ):
                raise Conflict("Formal claim owner or state is stale")
            if connection.execute(
                "SELECT 1 FROM formal_dispatch_stop_requests WHERE intent_id = %s", (intent_id,)
            ).fetchone():
                raise Conflict("Formal stop requested; do not start another phase")
            dispatcher._revalidate_locked(connection, prepared)
            now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if now >= claim["expires_at"]:
                raise Conflict("Formal claim expired; recovery required")

    def _lock_deployment_intent(
        self, connection: Connection, intent_id: UUID
    ) -> FormalStartIntentView:
        row = connection.execute(
            "SELECT * FROM formal_operator_start_intents WHERE intent_id = %s FOR UPDATE",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise Conflict("Formal claim is unavailable")
        intent = self.dispatcher.repository._formal_start_intent(row)
        if intent.service_identity != self.dispatcher.coordinator.service_identity:
            raise Conflict("Formal claim belongs to another deployment")
        return intent
