# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in atomic Formal creation. The outbox has no execution consumer yet."""

from typing import Any
from uuid import UUID

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from hcuopt.contracts.formal_dispatch_v1 import FormalDispatchStatus
from hcuopt.domain.enums import SearchRoundRunMode
from hcuopt.domain.errors import Conflict, NotFound
from hcuopt.operator.formal_dispatch import PreparedFormalRound, prepare_formal_round
from hcuopt.operator.formal_start import FormalStartCoordinator
from hcuopt.storage.repository import PostgresRepository


def _insert(connection: Connection, table: str, payload: dict[str, Any]) -> None:
    """Only internal, fixed model field maps; no client-supplied table/column names."""
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(map(sql.Identifier, payload)),
            sql.SQL(", ").join(sql.Placeholder() for _ in payload),
        ),
        tuple(Jsonb(value) if isinstance(value, dict) else value for value in payload.values()),
    )


class PostgresFormalDispatcher:
    """Explicit deployment service; not installed in default API or Worker.

    Creates the full intake plus one durable outbox row, never a generic Job.
    Production role/credential management remains the deployment boundary.
    """

    def __init__(
        self,
        repository: PostgresRepository,
        coordinator: FormalStartCoordinator,
        *,
        enabled: bool = False,
    ) -> None:
        self.repository = repository
        self.coordinator = coordinator
        self.enabled = enabled

    def read_status(self, intent_id: UUID) -> FormalDispatchStatus:
        intent = self.repository.get_formal_start_intent(intent_id)
        if intent.service_identity != self.coordinator.service_identity:
            raise Conflict("Formal dispatch belongs to another deployment")
        with self.repository.connection() as connection:
            row = connection.execute(
                "SELECT * FROM formal_round_dispatches WHERE intent_id = %s", (intent_id,)
            ).fetchone()
        if row is not None and (
            row["round_id"] != intent.round_id
            or row["request_digest"] != intent.request_digest
            or row["resolved_plan_hash"] != intent.resolved_plan_hash
            or row["service_identity"] != intent.service_identity.model_dump(mode="json")
        ):
            raise Conflict("Formal dispatch stored bindings differ")
        return FormalDispatchStatus(
            intent_id=intent_id,
            round_id=intent.round_id,
            resolved_plan_hash=intent.resolved_plan_hash,
            state="not_created" if row is None else row["state"],
            service_identity=intent.service_identity,
        )

    def create(self, intent_id: UUID) -> dict[str, Any]:
        if not self.enabled:
            raise Conflict("Formal dispatch is disabled")
        original = self.repository.get_formal_start_intent(intent_id)
        if original.service_identity != self.coordinator.service_identity:
            raise Conflict("Formal dispatch belongs to another deployment")
        # Return an already-created fact even after expiry. No new execution.
        with self.repository.connection() as connection:
            previous = connection.execute(
                "SELECT * FROM formal_round_dispatches WHERE intent_id = %s", (intent_id,)
            ).fetchone()
            if previous is not None:
                return {**previous, "replayed": True}
        prepared = prepare_formal_round(self.coordinator, original, self.repository)
        with self.repository.connection() as connection:
            row = connection.execute(
                "SELECT * FROM formal_operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound("Formal Intent no longer exists")
            current = self.repository._formal_start_intent(row)
            previous = connection.execute(
                "SELECT * FROM formal_round_dispatches WHERE intent_id = %s", (intent_id,)
            ).fetchone()
            if previous is not None:
                return {**previous, "replayed": True}
            if current != prepared.intent:
                raise Conflict("Formal Intent changed while preparing dispatch")
            self._revalidate_locked(connection, prepared)
            self._write_round(connection, prepared)
            # Recheck time after potentially blocking inserts, immediately before publication.
            self._require_current_window(connection, prepared)
            _insert(
                connection,
                "formal_round_dispatches",
                {
                    "intent_id": intent_id,
                    "task_id": current.task_id,
                    "round_id": current.round_id,
                    "intent_version": current.version,
                    "request_digest": current.request_digest,
                    "resolved_plan_hash": current.resolved_plan_hash,
                    "candidate_family_hash": prepared.round.candidate_family_hash,
                    "service_identity": current.service_identity.model_dump(mode="json"),
                    "valid_from": prepared.valid_from,
                    "valid_until": prepared.valid_until,
                },
            )
            _insert(
                connection,
                "formal_round_dispatch_events",
                {
                    "intent_id": intent_id,
                    "event_type": "queued",
                },
            )
            _insert(
                connection,
                "task_events",
                {
                    "task_id": current.task_id,
                    "event_type": "formal_round_queued",
                    "details": {"intent_id": str(intent_id), "round_id": str(current.round_id)},
                },
            )
            result = connection.execute(
                "SELECT * FROM formal_round_dispatches WHERE intent_id = %s", (intent_id,)
            ).fetchone()
            assert result is not None
            return {**result, "replayed": False}

    @staticmethod
    def _require_current_window(connection: Connection, prepared: PreparedFormalRound) -> None:
        now = connection.execute("SELECT clock_timestamp() AS current_time").fetchone()[
            "current_time"
        ]
        if not prepared.valid_from <= now < prepared.valid_until:
            raise Conflict("Formal dispatch authorization window expired or has not started")

    def _revalidate_locked(self, connection: Connection, prepared: PreparedFormalRound) -> None:
        self._require_current_window(connection, prepared)
        plan = prepared.preview.resolved_plan
        catalog = self.coordinator.compiler.profiles
        target = catalog.require(plan.target_profile, SearchRoundRunMode.FORMAL).authority_refs
        workload = catalog.require(plan.workload_profile, SearchRoundRunMode.FORMAL).authority_refs
        catalog.require(plan.measurement_profile, SearchRoundRunMode.FORMAL)
        actual = self.repository.resolve_formal_operator_authority(
            target,
            workload,
            plan.candidate_family,
            connection=connection,
        )
        if actual.authority != plan.authority or actual.hotspot != plan.hotspot:
            raise Conflict("Formal database authority changed while preparing dispatch")

    @staticmethod
    def _write_round(connection: Connection, prepared: PreparedFormalRound) -> None:
        round_ = prepared.round
        target = connection.execute(
            "SELECT target_id FROM target_snapshots WHERE target_snapshot_id = %s",
            (round_.target_snapshot_id,),
        ).fetchone()
        if target is None:
            raise NotFound("Formal target snapshot is unavailable")
        _insert(
            connection,
            "tasks",
            {
                "task_id": round_.task_id,
                "name": f"Formal SearchRound {round_.round_id}",
                "workload_id": round_.workload_id,
                "idempotency_key": round_.idempotency_key,
                "state": "created",
                "budget": round_.budget.model_dump(mode="json"),
                "workflow_type": "search_round",
                "target_id": target["target_id"],
                "target_snapshot_id": round_.target_snapshot_id,
                "adapter_profile": round_.adapter_profile,
                "stage0_run_id": round_.stage0_run_id,
                "stage0_authority": "formal",
                "project_mode": "degraded_manual_intake",
                "automatic_release_allowed": False,
            },
        )
        _insert(connection, "search_rounds", round_.model_dump(mode="python"))
        for member in prepared.members:
            metadata = {
                "workflow_type": "search_round",
                "round_candidate_id": str(member.round_candidate_id),
                "source_package_store_id": member.source_package_store_id,
                "source_package_store_hash": member.source_package_store_hash,
                "source_package_hash": member.source_package_hash,
                "source_manifest_version": member.source_manifest_version,
                "source_manifest_hash": member.source_manifest_hash,
                "baseline_source_hash": member.baseline_source_hash,
            }
            _insert(
                connection,
                "candidates",
                {
                    "candidate_id": member.candidate_id,
                    "task_id": round_.task_id,
                    "round_id": round_.round_id,
                    "baseline_epoch_id": round_.baseline_epoch_id,
                    "source_hash": member.candidate_source_hash,
                    "variant": "m2-formal-business",
                    "state": "proposed",
                    "ordinal": member.ordinal,
                    "metadata": metadata,
                    "track": member.track,
                    "release_mode": member.release_mode,
                    "candidate_kind": "business",
                    "optimization_intent": member.optimization_intent,
                    "replacement_point": member.replacement_point,
                    "idempotency_key": member.idempotency_key,
                    "hotspot_id": round_.hotspot_id,
                },
            )
            _insert(connection, "round_candidates", member.model_dump(mode="python"))
