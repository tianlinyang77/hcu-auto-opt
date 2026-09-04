# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from hcuopt.agent.authority import (
    AgentAuthorityError,
    actual_usage_for_runner_receipt,
    build_pending_attempt,
    conservative_failure_usage,
    finalize_proposal_dispositions,
    generation_budget_entry_id_for,
    generation_budget_idempotency_key,
    proposal_refs_for_batch,
    sum_usage,
    usage_is_within_reservation,
    validate_runner_receipt,
)
from hcuopt.agent.identity import candidate_proposal_batch_hash
from hcuopt.contracts.agent_read_model_v1 import AgentGenerationEvidencePublication
from hcuopt.contracts.agent_runner_v1 import (
    RunnerExecutionReceipt,
    RunnerExecutionReceiptRef,
    runner_execution_receipt_hash,
)
from hcuopt.contracts.agent_v1 import (
    CandidateProposalBatch,
    CandidateProposalRef,
    GenerationAttemptClaim,
    GenerationBudgetLedgerEntry,
    GenerationBudgetUsage,
    GenerationRun,
    GenerationRunStatusView,
    GeneratorAttempt,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.errors import Conflict, NotFound, StaleClaimToken


class RunnerExecutionReceiptReader(Protocol):
    def load(self, reference: RunnerExecutionReceiptRef) -> RunnerExecutionReceipt: ...


class AgentGenerationRepositoryMixin:
    """PostgreSQL Generation Authority methods mixed into PostgresRepository."""

    @staticmethod
    def _generation_run(row: dict[str, Any]) -> GenerationRun:
        return GenerationRun.model_validate(
            {
                name: row[name]
                for name in GenerationRun.model_fields
                if name != "schema_version"
            }
        )

    @staticmethod
    def _generator_attempt(row: dict[str, Any]) -> GeneratorAttempt:
        return GeneratorAttempt.model_validate(
            {
                name: row[name]
                for name in GeneratorAttempt.model_fields
                if name != "schema_version"
            }
        )

    @staticmethod
    def _proposal_ref(row: dict[str, Any]) -> CandidateProposalRef:
        return CandidateProposalRef.model_validate(
            {
                name: row[name]
                for name in CandidateProposalRef.model_fields
                if name != "schema_version"
            }
        )

    @staticmethod
    def _budget_entry(row: dict[str, Any]) -> GenerationBudgetLedgerEntry:
        return GenerationBudgetLedgerEntry.model_validate(
            {
                name: row[name]
                for name in GenerationBudgetLedgerEntry.model_fields
                if name != "schema_version"
            }
        )

    @staticmethod
    def _evidence_publication(row: dict[str, Any]) -> AgentGenerationEvidencePublication:
        return AgentGenerationEvidencePublication.model_validate(row["publication"])

    @staticmethod
    def _run_insert_values(run: GenerationRun) -> tuple[Any, ...]:
        return (
            run.generation_run_id,
            run.request.request_id,
            run.plan.plan_id,
            run.request_hash,
            run.plan_hash,
            Jsonb(run.request.model_dump(mode="json")),
            Jsonb(run.plan.model_dump(mode="json")),
            run.actor,
            run.idempotency_key,
            run.state,
            run.planned_generator_count,
            run.attempt_count,
            run.terminal_attempt_count,
            run.terminal_generator_count,
            run.proposal_count,
            run.retained_proposal_count,
            Jsonb(run.budget_reserved.model_dump(mode="json")),
            Jsonb(run.budget_consumed.model_dump(mode="json")),
            run.version,
            run.created_at,
            run.updated_at,
        )

    @staticmethod
    def _insert_attempt(connection: Any, attempt: GeneratorAttempt) -> None:
        connection.execute(
            """
            INSERT INTO agent_generator_attempts (
                attempt_id, generation_run_id, plan_id, request_id,
                generator_id, generator_ordinal, attempt_number, adapter_profile,
                state, reserved, actual, dev_only, hcu_access_allowed,
                measurement_access_allowed, automatic_release_allowed,
                version, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                'pending', %s, %s, TRUE, FALSE, FALSE, FALSE, %s, %s, %s
            )
            """,
            (
                attempt.attempt_id,
                attempt.generation_run_id,
                attempt.plan_id,
                attempt.request_id,
                attempt.generator_id,
                attempt.generator_ordinal,
                attempt.attempt_number,
                attempt.adapter_profile,
                Jsonb(attempt.reserved.model_dump(mode="json")),
                Jsonb(attempt.actual.model_dump(mode="json")),
                attempt.version,
                attempt.created_at,
                attempt.updated_at,
            ),
        )

    @staticmethod
    def _insert_budget_entry(connection: Any, entry: GenerationBudgetLedgerEntry) -> None:
        connection.execute(
            """
            INSERT INTO agent_generation_budget_ledger (
                ledger_entry_id, generation_run_id, attempt_id, entry_type,
                reserved, actual, idempotency_key, automatic_release_allowed, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, %s)
            ON CONFLICT (ledger_entry_id) DO NOTHING
            """,
            (
                entry.ledger_entry_id,
                entry.generation_run_id,
                entry.attempt_id,
                entry.entry_type,
                Jsonb(entry.reserved.model_dump(mode="json")),
                Jsonb(entry.actual.model_dump(mode="json")),
                entry.idempotency_key,
                entry.created_at,
            ),
        )

    def create_generation_run(
        self,
        run: GenerationRun,
        attempts: tuple[GeneratorAttempt, ...],
        ledger_entries: tuple[GenerationBudgetLedgerEntry, ...],
    ) -> tuple[GenerationRun, bool]:
        if run.state != "created" or not run.dev_only:
            raise Conflict("new Agent Generation Run must be dev-only and created")
        if not len(attempts) == len(ledger_entries) == run.planned_generator_count:
            raise Conflict("Agent Generation Run initial Attempt family is incomplete")
        with self.connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                """
                INSERT INTO agent_generation_runs (
                    generation_run_id, request_id, plan_id, request_hash, plan_hash,
                    request, plan, actor, idempotency_key, state,
                    planned_generator_count, attempt_count, terminal_attempt_count,
                    terminal_generator_count, proposal_count, retained_proposal_count,
                    budget_reserved, budget_consumed, dev_only, formal_intake_allowed,
                    hcu_access_allowed, measurement_access_allowed,
                    automatic_release_allowed, version, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    TRUE, FALSE, FALSE, FALSE, FALSE, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                self._run_insert_values(run),
            ).fetchone()
            created = row is not None
            if created:
                for attempt, entry in zip(attempts, ledger_entries, strict=True):
                    self._insert_attempt(connection, attempt)
                    self._insert_budget_entry(connection, entry)
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM agent_generation_runs
                    WHERE generation_run_id = %s OR request_id = %s OR plan_id = %s
                       OR idempotency_key = %s
                    FOR UPDATE
                    """,
                    (
                        run.generation_run_id,
                        run.request.request_id,
                        run.plan.plan_id,
                        run.idempotency_key,
                    ),
                ).fetchall()
                row = rows[0] if len(rows) == 1 else None
            if row is None:
                raise Conflict("Agent Generation identity or idempotency key was reused")
            stored = self._generation_run(row)
            immutable = (
                stored.generation_run_id == run.generation_run_id
                and stored.request == run.request
                and stored.request_hash == run.request_hash
                and stored.plan == run.plan
                and stored.plan_hash == run.plan_hash
                and stored.actor == run.actor
                and stored.idempotency_key == run.idempotency_key
            )
            if not immutable:
                raise Conflict("Agent Generation idempotency key was reused with different inputs")
        return stored, created

    def get_generation_run(self, generation_run_id: UUID) -> GenerationRun:
        with self.connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                "SELECT * FROM agent_generation_runs WHERE generation_run_id = %s",
                (generation_run_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"Agent Generation Run not found: {generation_run_id}")
        return self._generation_run(row)

    def list_generation_attempts(
        self,
        generation_run_id: UUID,
    ) -> tuple[GeneratorAttempt, ...]:
        with self.connection() as connection:  # type: ignore[attr-defined]
            rows = connection.execute(
                """
                SELECT * FROM agent_generator_attempts
                WHERE generation_run_id = %s
                ORDER BY generator_ordinal, attempt_number
                """,
                (generation_run_id,),
            ).fetchall()
        return tuple(self._generator_attempt(row) for row in rows)

    def list_candidate_proposal_refs(
        self,
        generation_run_id: UUID,
    ) -> tuple[CandidateProposalRef, ...]:
        with self.connection() as connection:  # type: ignore[attr-defined]
            rows = connection.execute(
                """
                SELECT * FROM agent_candidate_proposal_refs
                WHERE generation_run_id = %s
                ORDER BY generator_ordinal, proposal_ordinal, proposal_id
                """,
                (generation_run_id,),
            ).fetchall()
        return tuple(self._proposal_ref(row) for row in rows)

    def list_generation_budget_ledger(
        self,
        generation_run_id: UUID,
    ) -> tuple[GenerationBudgetLedgerEntry, ...]:
        with self.connection() as connection:  # type: ignore[attr-defined]
            rows = connection.execute(
                """
                SELECT * FROM agent_generation_budget_ledger
                WHERE generation_run_id = %s
                ORDER BY created_at, attempt_id, entry_type, ledger_entry_id
                """,
                (generation_run_id,),
            ).fetchall()
        return tuple(self._budget_entry(row) for row in rows)

    def generation_run_status(self, generation_run_id: UUID) -> GenerationRunStatusView:
        run = self.get_generation_run(generation_run_id)
        return GenerationRunStatusView(
            run=run,
            attempts=self.list_generation_attempts(generation_run_id),
            proposals=self.list_candidate_proposal_refs(generation_run_id),
            budget_ledger=self.list_generation_budget_ledger(generation_run_id),
        )

    @staticmethod
    def _outstanding_reserved(connection: Any, generation_run_id: UUID) -> GenerationBudgetUsage:
        rows = connection.execute(
            """
            SELECT reserved FROM agent_generator_attempts
            WHERE generation_run_id = %s AND state IN ('pending', 'running')
            """,
            (generation_run_id,),
        ).fetchall()
        return sum_usage(
            tuple(GenerationBudgetUsage.model_validate(row["reserved"]) for row in rows)
        )

    @staticmethod
    def _consumed_budget(connection: Any, generation_run_id: UUID) -> GenerationBudgetUsage:
        rows = connection.execute(
            """
            SELECT actual FROM agent_generation_budget_ledger
            WHERE generation_run_id = %s AND entry_type = 'settle'
            """,
            (generation_run_id,),
        ).fetchall()
        return sum_usage(tuple(GenerationBudgetUsage.model_validate(row["actual"]) for row in rows))

    @staticmethod
    def _settle_entry(
        attempt: GeneratorAttempt,
        actual: GenerationBudgetUsage,
        now: datetime,
    ) -> GenerationBudgetLedgerEntry:
        return GenerationBudgetLedgerEntry(
            ledger_entry_id=generation_budget_entry_id_for(attempt.attempt_id, "settle"),
            generation_run_id=attempt.generation_run_id,
            attempt_id=attempt.attempt_id,
            entry_type="settle",
            reserved=attempt.reserved,
            actual=actual,
            idempotency_key=generation_budget_idempotency_key(attempt.attempt_id, "settle"),
            created_at=now,
        )

    def _fail_stale_attempt(
        self,
        connection: Any,
        attempt: GeneratorAttempt,
        now: datetime,
    ) -> None:
        actual = conservative_failure_usage(attempt.reserved)
        connection.execute(
            """
            UPDATE agent_generator_attempts
            SET state = 'failed', actual = %s,
                error_code = 'attempt_lease_expired',
                error_message = 'generator Attempt lease expired before settlement',
                version = version + 1, updated_at = %s, finished_at = %s
            WHERE attempt_id = %s AND state = 'running'
            """,
            (
                Jsonb(actual.model_dump(mode="json")),
                now,
                now,
                attempt.attempt_id,
            ),
        )
        self._insert_budget_entry(
            connection,
            self._settle_entry(attempt, actual, now),
        )

    def _insert_retry(
        self,
        connection: Any,
        run: GenerationRun,
        generator_ordinal: int,
        attempt_number: int,
        now: datetime,
    ) -> None:
        generator = run.plan.generators[generator_ordinal]
        attempt, reserve = build_pending_attempt(
            run,
            generator,
            generator_ordinal,
            attempt_number,
            now,
        )
        self._insert_attempt(connection, attempt)
        self._insert_budget_entry(connection, reserve)

    def _finalize_proposals(self, connection: Any, generation_run_id: UUID) -> int:
        rows = connection.execute(
            """
            SELECT * FROM agent_candidate_proposal_refs
            WHERE generation_run_id = %s
            ORDER BY generator_ordinal, proposal_ordinal, proposal_id
            FOR UPDATE
            """,
            (generation_run_id,),
        ).fetchall()
        finalized = finalize_proposal_dispositions(
            tuple(self._proposal_ref(row) for row in rows)
        )
        for proposal in (item for item in finalized if item.disposition == "retained"):
            connection.execute(
                """
                UPDATE agent_candidate_proposal_refs
                SET disposition = 'retained', duplicate_of_proposal_id = NULL
                WHERE proposal_id = %s AND disposition = 'pending'
                """,
                (proposal.proposal_id,),
            )
        for proposal in (item for item in finalized if item.disposition == "duplicate"):
            connection.execute(
                """
                UPDATE agent_candidate_proposal_refs
                SET disposition = 'duplicate', duplicate_of_proposal_id = %s
                WHERE proposal_id = %s AND disposition = 'pending'
                """,
                (proposal.duplicate_of_proposal_id, proposal.proposal_id),
            )
        return sum(item.disposition == "retained" for item in finalized)

    def _reconcile_generation_run_locked(
        self,
        connection: Any,
        row: dict[str, Any],
        now: datetime,
    ) -> GenerationRun:
        run = self._generation_run(row)
        if run.state in {"completed", "failed", "cancelled"}:
            return run
        if run.state == "awaiting_review":
            return run
        if run.state == "created":
            row = connection.execute(
                """
                UPDATE agent_generation_runs
                SET state = 'running', version = version + 1, updated_at = %s
                WHERE generation_run_id = %s AND state = 'created'
                RETURNING *
                """,
                (now, run.generation_run_id),
            ).fetchone()
            assert row is not None
            run = self._generation_run(row)

        attempts = tuple(
            self._generator_attempt(item)
            for item in connection.execute(
                """
                SELECT * FROM agent_generator_attempts
                WHERE generation_run_id = %s
                ORDER BY generator_ordinal, attempt_number
                FOR UPDATE
                """,
                (run.generation_run_id,),
            ).fetchall()
        )
        for attempt in attempts:
            if (
                attempt.state == "running"
                and attempt.lease_expires_at is not None
                and attempt.lease_expires_at <= now
            ):
                self._fail_stale_attempt(connection, attempt, now)

        attempts = tuple(
            self._generator_attempt(item)
            for item in connection.execute(
                """
                SELECT * FROM agent_generator_attempts
                WHERE generation_run_id = %s
                ORDER BY generator_ordinal, attempt_number
                FOR UPDATE
                """,
                (run.generation_run_id,),
            ).fetchall()
        )
        by_generator: dict[int, list[GeneratorAttempt]] = {
            ordinal: [] for ordinal in range(run.planned_generator_count)
        }
        for attempt in attempts:
            by_generator[attempt.generator_ordinal].append(attempt)
        for ordinal, generator_attempts in by_generator.items():
            latest = generator_attempts[-1]
            generator = run.plan.generators[ordinal]
            if (
                not any(item.state == "succeeded" for item in generator_attempts)
                and latest.state == "failed"
                and latest.attempt_number < generator.max_attempts
            ):
                self._insert_retry(
                    connection,
                    run,
                    ordinal,
                    latest.attempt_number + 1,
                    now,
                )

        attempts = tuple(
            self._generator_attempt(item)
            for item in connection.execute(
                """
                SELECT * FROM agent_generator_attempts
                WHERE generation_run_id = %s
                ORDER BY generator_ordinal, attempt_number
                """,
                (run.generation_run_id,),
            ).fetchall()
        )
        by_generator = {ordinal: [] for ordinal in range(run.planned_generator_count)}
        for attempt in attempts:
            by_generator[attempt.generator_ordinal].append(attempt)
        terminal_generators = 0
        for ordinal, generator_attempts in by_generator.items():
            generator = run.plan.generators[ordinal]
            latest = generator_attempts[-1]
            if any(item.state == "succeeded" for item in generator_attempts) or (
                latest.state in {"failed", "cancelled"}
                and latest.attempt_number >= generator.max_attempts
            ):
                terminal_generators += 1

        proposal_count = connection.execute(
            """
            SELECT count(*) AS count FROM agent_candidate_proposal_refs
            WHERE generation_run_id = %s
            """,
            (run.generation_run_id,),
        ).fetchone()["count"]
        retained_count = 0
        state = "running"
        error_code = None
        error_message = None
        finished_at = None
        if terminal_generators == run.planned_generator_count:
            retained_count = self._finalize_proposals(connection, run.generation_run_id)
            if retained_count:
                state = "awaiting_review"
            else:
                state = "failed"
                error_code = "no_retained_proposals"
                error_message = "all generator Attempts terminated without a retained Proposal"
                finished_at = now

        terminal_attempt_count = sum(
            item.state in {"succeeded", "failed", "cancelled"} for item in attempts
        )
        reserved = self._outstanding_reserved(connection, run.generation_run_id)
        consumed = self._consumed_budget(connection, run.generation_run_id)
        if not reserved.plus(consumed).fits(run.plan.budget):
            state = "failed"
            error_code = "generation_budget_corrupt"
            error_message = "generation Budget ledger exceeds the immutable Plan"
            finished_at = now
        row = connection.execute(
            """
            UPDATE agent_generation_runs
            SET state = %s, attempt_count = %s, terminal_attempt_count = %s,
                terminal_generator_count = %s, proposal_count = %s,
                retained_proposal_count = %s, budget_reserved = %s,
                budget_consumed = %s, error_code = %s, error_message = %s,
                finished_at = %s, version = version + 1, updated_at = %s
            WHERE generation_run_id = %s
            RETURNING *
            """,
            (
                state,
                len(attempts),
                terminal_attempt_count,
                terminal_generators,
                proposal_count,
                retained_count,
                Jsonb(reserved.model_dump(mode="json")),
                Jsonb(consumed.model_dump(mode="json")),
                error_code,
                error_message,
                finished_at,
                now,
                run.generation_run_id,
            ),
        ).fetchone()
        assert row is not None
        return self._generation_run(row)

    def reconcile_generation_run(
        self,
        generation_run_id: UUID,
        *,
        now: datetime | None = None,
    ) -> GenerationRun:
        effective_now = now or datetime.now(timezone.utc)
        if effective_now.tzinfo is None or effective_now.utcoffset() is None:
            raise ValueError("Agent Generation reconcile time must be timezone-aware")
        with self.connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                """
                SELECT * FROM agent_generation_runs
                WHERE generation_run_id = %s
                FOR UPDATE
                """,
                (generation_run_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Agent Generation Run not found: {generation_run_id}")
            return self._reconcile_generation_run_locked(connection, row, effective_now)

    def claim_generation_attempt(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        generation_run_id: UUID | None = None,
        now: datetime | None = None,
    ) -> GenerationAttemptClaim | None:
        if not worker_id or worker_id.strip() != worker_id:
            raise ValueError("Agent generator worker_id must be normalized")
        if lease_seconds < 1 or lease_seconds > 7200:
            raise ValueError("Agent generator claim lease must be between 1 and 7200 seconds")
        effective_now = now or datetime.now(timezone.utc)
        with self.connection() as connection:  # type: ignore[attr-defined]
            candidate_rows = connection.execute(
                """
                SELECT run.generation_run_id FROM agent_generation_runs AS run
                WHERE run.state = 'running'
                  AND (%s::uuid IS NULL OR run.generation_run_id = %s)
                  AND EXISTS (
                      SELECT 1 FROM agent_generator_attempts AS pending
                      WHERE pending.generation_run_id = run.generation_run_id
                        AND pending.state = 'pending'
                  )
                ORDER BY run.created_at, run.generation_run_id
                """,
                (generation_run_id, generation_run_id),
            ).fetchall()
            run: GenerationRun | None = None
            for candidate_row in candidate_rows:
                run_row = connection.execute(
                    """
                    SELECT run.* FROM agent_generation_runs AS run
                    WHERE run.generation_run_id = %s
                      AND run.state = 'running'
                      AND EXISTS (
                          SELECT 1 FROM agent_generator_attempts AS pending
                          WHERE pending.generation_run_id = run.generation_run_id
                            AND pending.state = 'pending'
                      )
                    FOR UPDATE SKIP LOCKED
                    """,
                    (candidate_row["generation_run_id"],),
                ).fetchone()
                if run_row is None:
                    continue
                locked_run = self._generation_run(run_row)
                active_row = connection.execute(
                    """
                    SELECT count(*) AS active_count
                    FROM agent_generator_attempts
                    WHERE generation_run_id = %s AND state = 'running'
                    """,
                    (locked_run.generation_run_id,),
                ).fetchone()
                assert active_row is not None
                if int(active_row["active_count"]) >= locked_run.plan.max_concurrency:
                    continue
                run = locked_run
                break
            if run is None:
                return None
            attempt_row = connection.execute(
                """
                SELECT * FROM agent_generator_attempts
                WHERE generation_run_id = %s AND state = 'pending'
                ORDER BY generator_ordinal, attempt_number
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (run.generation_run_id,),
            ).fetchone()
            if attempt_row is None:
                return None
            pending = self._generator_attempt(attempt_row)
            generator = run.plan.generators[pending.generator_ordinal]
            if lease_seconds > generator.timeout_seconds:
                raise Conflict("Agent generator claim lease exceeds its immutable timeout")
            claim_token = uuid4()
            lease_expires_at = effective_now + timedelta(seconds=lease_seconds)
            attempt_row = connection.execute(
                """
                UPDATE agent_generator_attempts
                SET state = 'running', worker_id = %s, claim_token = %s,
                    lease_expires_at = %s, started_at = %s, updated_at = %s,
                    version = version + 1
                WHERE attempt_id = %s AND state = 'pending'
                RETURNING *
                """,
                (
                    worker_id,
                    claim_token,
                    lease_expires_at,
                    effective_now,
                    effective_now,
                    pending.attempt_id,
                ),
            ).fetchone()
            assert attempt_row is not None
            attempt = self._generator_attempt(attempt_row)
            return GenerationAttemptClaim(
                run=run,
                attempt=attempt,
                generator=generator,
                request=run.request,
            )

    def renew_generation_attempt_claim(
        self,
        attempt_id: UUID,
        claim_token: UUID,
        *,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> GeneratorAttempt:
        if lease_seconds < 1 or lease_seconds > 7200:
            raise ValueError("Agent generator claim lease must be between 1 and 7200 seconds")
        effective_now = now or datetime.now(timezone.utc)
        stale = False
        result: GeneratorAttempt | None = None
        with self.connection() as connection:  # type: ignore[attr-defined]
            attempt_row = connection.execute(
                "SELECT * FROM agent_generator_attempts WHERE attempt_id = %s",
                (attempt_id,),
            ).fetchone()
            if attempt_row is None:
                raise NotFound(f"Agent Generator Attempt not found: {attempt_id}")
            run_row = connection.execute(
                """
                SELECT * FROM agent_generation_runs
                WHERE generation_run_id = %s FOR UPDATE
                """,
                (attempt_row["generation_run_id"],),
            ).fetchone()
            assert run_row is not None
            attempt_row = connection.execute(
                "SELECT * FROM agent_generator_attempts WHERE attempt_id = %s FOR UPDATE",
                (attempt_id,),
            ).fetchone()
            assert attempt_row is not None
            attempt = self._generator_attempt(attempt_row)
            run = self._generation_run(run_row)
            generator = run.plan.generators[attempt.generator_ordinal]
            if lease_seconds > generator.timeout_seconds:
                raise Conflict("Agent generator claim lease exceeds its immutable timeout")
            if (
                attempt.state != "running"
                or attempt.claim_token != claim_token
                or attempt.lease_expires_at is None
                or attempt.lease_expires_at <= effective_now
            ):
                if attempt.state == "running" and attempt.lease_expires_at is not None:
                    self._reconcile_generation_run_locked(connection, run_row, effective_now)
                stale = True
            else:
                row = connection.execute(
                    """
                    UPDATE agent_generator_attempts
                    SET lease_expires_at = %s, updated_at = %s, version = version + 1
                    WHERE attempt_id = %s AND state = 'running' AND claim_token = %s
                    RETURNING *
                    """,
                    (
                        effective_now + timedelta(seconds=lease_seconds),
                        effective_now,
                        attempt_id,
                        claim_token,
                    ),
                ).fetchone()
                assert row is not None
                result = self._generator_attempt(row)
        if stale:
            raise StaleClaimToken("Agent generator Attempt claim is stale")
        assert result is not None
        return result

    def settle_generation_attempt(
        self,
        attempt_id: UUID,
        claim_token: UUID,
        batch: CandidateProposalBatch | None,
        runner_receipt_ref: RunnerExecutionReceiptRef,
        *,
        runner_receipt_reader: RunnerExecutionReceiptReader,
        batch_uri: str | None = None,
        now: datetime | None = None,
    ) -> GeneratorAttempt:
        effective_now = now or datetime.now(timezone.utc)
        try:
            receipt_ref = RunnerExecutionReceiptRef.model_validate(
                runner_receipt_ref.model_dump(mode="json")
            )
            runner_receipt = runner_receipt_reader.load(receipt_ref)
            runner_receipt = RunnerExecutionReceipt.model_validate(
                runner_receipt.model_dump(mode="json")
            )
        except Exception as error:
            raise Conflict("Agent Runner Receipt could not be independently reread") from error
        execution = runner_receipt.execution
        if (
            runner_execution_receipt_hash(runner_receipt) != receipt_ref.content_hash
            or runner_receipt.receipt_id != receipt_ref.receipt_id
            or execution.attempt_id != receipt_ref.attempt_id
            or execution.generation_run_id != receipt_ref.generation_run_id
            or execution.request_id != receipt_ref.request_id
            or execution.request_hash != receipt_ref.request_hash
            or receipt_ref.attempt_id != attempt_id
        ):
            raise Conflict("Agent Runner Receipt Ref differs from independently reread content")
        batch_hash = candidate_proposal_batch_hash(batch) if batch is not None else None
        if (batch is None) != (batch_uri is None):
            raise Conflict("Agent Proposal Batch and its immutable URI must be atomic")
        stale = False
        result: GeneratorAttempt | None = None
        with self.connection() as connection:  # type: ignore[attr-defined]
            attempt_row = connection.execute(
                "SELECT * FROM agent_generator_attempts WHERE attempt_id = %s",
                (attempt_id,),
            ).fetchone()
            if attempt_row is None:
                raise NotFound(f"Agent Generator Attempt not found: {attempt_id}")
            run_row = connection.execute(
                """
                SELECT * FROM agent_generation_runs
                WHERE generation_run_id = %s FOR UPDATE
                """,
                (attempt_row["generation_run_id"],),
            ).fetchone()
            assert run_row is not None
            attempt_row = connection.execute(
                "SELECT * FROM agent_generator_attempts WHERE attempt_id = %s FOR UPDATE",
                (attempt_id,),
            ).fetchone()
            assert attempt_row is not None
            attempt = self._generator_attempt(attempt_row)
            run = self._generation_run(run_row)
            generator = run.plan.generators[attempt.generator_ordinal]
            try:
                validate_runner_receipt(
                    run,
                    attempt,
                    generator,
                    runner_receipt,
                    batch,
                )
            except AgentAuthorityError as error:
                raise Conflict(
                    "Agent Generation settlement rejected unbound Runner evidence"
                ) from error
            runner_provenance = AdapterProvenance(
                profile=execution.runner_provenance.profile,
                capability=execution.runner_provenance.capability,
                adapter_name=execution.runner_provenance.adapter_name,
                adapter_version=execution.runner_provenance.adapter_version,
                implementation_kind=execution.runner_provenance.implementation_kind,
                source_commit=execution.runner_provenance.source_commit,
            )
            if attempt.state in {"succeeded", "failed"}:
                if (
                    attempt.claim_token == claim_token
                    and attempt.batch_id == (batch.batch_id if batch is not None else None)
                    and attempt.batch_uri == batch_uri
                    and attempt.batch_hash == batch_hash
                    and attempt.batch_status
                    == (batch.status if batch is not None else None)
                    and attempt.raw_output_uri
                    == (batch.raw_output_uri if batch is not None else None)
                    and attempt.raw_output_hash
                    == (batch.raw_output_hash if batch is not None else None)
                    and attempt.adapter_provenance
                    == (batch.adapter_provenance if batch is not None else None)
                    and attempt.runner_receipt_id == receipt_ref.receipt_id
                    and attempt.runner_receipt_uri == receipt_ref.uri
                    and attempt.runner_receipt_hash == receipt_ref.content_hash
                    and attempt.runner_receipt_schema_version == runner_receipt.schema_version
                    and attempt.runner_provenance == runner_provenance
                ):
                    return attempt
                raise StaleClaimToken("Agent generator settlement replay is stale")
            if (
                attempt.state != "running"
                or attempt.claim_token != claim_token
                or attempt.lease_expires_at is None
                or attempt.lease_expires_at <= effective_now
            ):
                if attempt.state == "running" and attempt.lease_expires_at is not None:
                    self._reconcile_generation_run_locked(connection, run_row, effective_now)
                stale = True
            else:
                try:
                    refs = (
                        proposal_refs_for_batch(run, attempt, generator, batch)
                        if batch is not None
                        else ()
                    )
                except AgentAuthorityError as error:
                    raise Conflict(
                        "Agent Generation settlement rejected unbound Runner evidence"
                    ) from error
                if batch is None:
                    actual = conservative_failure_usage(attempt.reserved)
                    state = "failed"
                    error_code = {
                        "failed": "runner_failed",
                        "timed_out": "runner_timeout",
                        "output_limit_exceeded": "runner_output_limit_exceeded",
                        "token_limit_exceeded": "runner_token_limit_exceeded",
                        "invalid_output": "runner_invalid_output",
                        "cleanup_failed": "runner_cleanup_failed",
                    }.get(execution.status, "runner_failed")
                    error_message = "Runner Attempt terminated without an accepted Proposal Batch"
                else:
                    actual = actual_usage_for_runner_receipt(
                        runner_receipt,
                        proposal_count=len(batch.proposals),
                    )
                    state = "succeeded"
                    error_code = None
                    error_message = None
                    if not usage_is_within_reservation(actual, attempt.reserved):
                        state = "failed"
                        actual = conservative_failure_usage(attempt.reserved)
                        refs = ()
                        error_code = "generation_budget_exceeded"
                        error_message = (
                            "Runner output exceeded its immutable Attempt reservation"
                        )
                    elif batch.status == "failed":
                        state = "failed"
                        error_code = batch.error_code or "generator_failed"
                        error_message = (
                            batch.error_message or "generator failed without safe detail"
                        )
                provenance = (
                    batch.adapter_provenance.model_dump(mode="json")
                    if batch is not None
                    else None
                )
                row = connection.execute(
                    """
                    UPDATE agent_generator_attempts
                    SET state = %s, actual = %s, batch_id = %s, batch_uri = %s,
                        batch_hash = %s,
                        batch_status = %s, raw_output_uri = %s, raw_output_hash = %s,
                        adapter_provenance = %s,
                        runner_receipt_id = %s, runner_receipt_uri = %s,
                        runner_receipt_hash = %s, runner_receipt_schema_version = %s,
                        runner_provenance = %s, error_code = %s, error_message = %s,
                        finished_at = %s, updated_at = %s, version = version + 1
                    WHERE attempt_id = %s AND state = 'running' AND claim_token = %s
                    RETURNING *
                    """,
                    (
                        state,
                        Jsonb(actual.model_dump(mode="json")),
                        batch.batch_id if batch is not None else None,
                        batch_uri,
                        batch_hash,
                        batch.status if batch is not None else None,
                        batch.raw_output_uri if batch is not None else None,
                        batch.raw_output_hash if batch is not None else None,
                        Jsonb(provenance) if provenance is not None else None,
                        receipt_ref.receipt_id,
                        receipt_ref.uri,
                        receipt_ref.content_hash,
                        runner_receipt.schema_version,
                        Jsonb(runner_provenance.model_dump(mode="json")),
                        error_code,
                        error_message,
                        effective_now,
                        effective_now,
                        attempt_id,
                        claim_token,
                    ),
                ).fetchone()
                assert row is not None
                self._insert_budget_entry(
                    connection,
                    self._settle_entry(attempt, actual, effective_now),
                )
                for ref in refs:
                    connection.execute(
                        """
                        INSERT INTO agent_candidate_proposal_refs (
                            proposal_id, proposal_hash, generation_run_id, request_id,
                            attempt_id, batch_id, generator_id, generator_ordinal,
                            proposal_ordinal, patch_uri, patch_hash, normalized_patch_hash,
                            disposition, review_required, formal_intake_allowed,
                            performance_conclusion, automatic_release_allowed
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, 'pending', TRUE, FALSE, 'not_measured', FALSE
                        )
                        """,
                        (
                            ref.proposal_id,
                            ref.proposal_hash,
                            ref.generation_run_id,
                            ref.request_id,
                            ref.attempt_id,
                            ref.batch_id,
                            ref.generator_id,
                            ref.generator_ordinal,
                            ref.proposal_ordinal,
                            ref.patch_uri,
                            ref.patch_hash,
                            ref.normalized_patch_hash,
                        ),
                    )
                self._reconcile_generation_run_locked(connection, run_row, effective_now)
                result_row = connection.execute(
                    "SELECT * FROM agent_generator_attempts WHERE attempt_id = %s",
                    (attempt_id,),
                ).fetchone()
                assert result_row is not None
                result = self._generator_attempt(result_row)
        if stale:
            raise StaleClaimToken("Agent generator Attempt claim expired before settlement")
        assert result is not None
        return result

    def complete_generation_review(
        self,
        generation_run_id: UUID,
        *,
        review_evidence_uri: str,
        review_evidence_hash: str,
        now: datetime | None = None,
    ) -> GenerationRun:
        effective_now = now or datetime.now(timezone.utc)
        with self.connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                """
                SELECT * FROM agent_generation_runs
                WHERE generation_run_id = %s FOR UPDATE
                """,
                (generation_run_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Agent Generation Run not found: {generation_run_id}")
            run = self._generation_run(row)
            if run.state == "completed":
                if (
                    run.review_evidence_uri != review_evidence_uri
                    or run.review_evidence_hash != review_evidence_hash
                ):
                    raise Conflict("Agent Generation review evidence changed during replay")
                return run
            if run.state != "awaiting_review":
                raise Conflict("Agent Generation Run is not awaiting Proposal review")
            row = connection.execute(
                """
                UPDATE agent_generation_runs
                SET state = 'completed', review_evidence_uri = %s,
                    review_evidence_hash = %s, finished_at = %s,
                    updated_at = %s, version = version + 1
                WHERE generation_run_id = %s AND state = 'awaiting_review'
                RETURNING *
                """,
                (
                    review_evidence_uri,
                    review_evidence_hash,
                    effective_now,
                    effective_now,
                    generation_run_id,
                ),
            ).fetchone()
            assert row is not None
            return self._generation_run(row)

    def publish_agent_generation_evidence_publication(
        self,
        publication: AgentGenerationEvidencePublication,
    ) -> AgentGenerationEvidencePublication:
        """Publish one immutable D index after the Generation Run is terminal."""

        with self.connection() as connection:  # type: ignore[attr-defined]
            run = connection.execute(
                """
                SELECT state FROM agent_generation_runs
                WHERE generation_run_id = %s FOR SHARE
                """,
                (publication.generation_run_id,),
            ).fetchone()
            if run is None:
                raise NotFound(
                    f"Agent Generation Run not found: {publication.generation_run_id}"
                )
            if run["state"] not in {"completed", "failed", "cancelled"}:
                raise Conflict(
                    "Agent Generation Evidence Read Model requires a terminal Run"
                )
            row = connection.execute(
                """
                INSERT INTO agent_generation_evidence_read_models (
                    generation_run_id, verifier_version, input_digest, publication,
                    published_at, dev_only, formal_readiness, performance_conclusion,
                    formal_intake_allowed, hcu_access_allowed,
                    measurement_access_allowed, automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, TRUE, 'hold', 'not_measured',
                    FALSE, FALSE, FALSE, FALSE
                )
                ON CONFLICT (generation_run_id) DO NOTHING
                RETURNING *
                """,
                (
                    publication.generation_run_id,
                    publication.verifier_version,
                    publication.input_digest,
                    Jsonb(publication.model_dump(mode="json")),
                    publication.published_at,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM agent_generation_evidence_read_models
                    WHERE generation_run_id = %s
                    """,
                    (publication.generation_run_id,),
                ).fetchone()
            assert row is not None
            stored = self._evidence_publication(row)
            if stored != publication:
                raise Conflict(
                    "Agent Generation Evidence publication changed during replay"
                )
            return stored

    def get_agent_generation_evidence_publication(
        self,
        generation_run_id: UUID,
    ) -> AgentGenerationEvidencePublication:
        with self.connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                """
                SELECT * FROM agent_generation_evidence_read_models
                WHERE generation_run_id = %s
                """,
                (generation_run_id,),
            ).fetchone()
        if row is None:
            raise NotFound(
                f"Agent Generation Evidence Read Model not found: {generation_run_id}"
            )
        return self._evidence_publication(row)
