from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hcuopt.contracts.platform_v1 import EvaluationRun, ExecutionAttempt
from hcuopt.contracts.v1 import (
    BaselineCreate,
    JobCreate,
    Stage0EvidenceRequest,
    TaskCreate,
    WorkerRegister,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobState,
    LeaseScope,
    ProjectMode,
    TaskState,
    WorkerType,
)
from hcuopt.domain.errors import Conflict, NotFound, StaleClaimToken, StaleFencingToken
from hcuopt.domain.transitions import transition_candidate, transition_task
from hcuopt.storage.migrations import migration_plan


class PostgresRepository:
    """Synchronous PostgreSQL boundary shared by API and maintenance commands.

    Each public method owns one short transaction. Long-running work happens in
    workers and never holds a database transaction open.
    """

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @contextmanager
    def connection(self) -> Iterator[Connection[dict[str, Any]]]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def migrate(self) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for version, sql in migration_plan():
                if version not in applied:
                    connection.execute(sql)

    def create_task(self, request: TaskCreate) -> dict[str, Any]:
        if request.automatic_release_allowed:
            raise Conflict("MVP forbids automatic production release")
        task_id = uuid4()
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed
                )
                VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    task_id,
                    request.name,
                    request.workload_id,
                    request.idempotency_key,
                    TaskState.STAGE0_PENDING.value,
                    Jsonb(request.budget),
                ),
            ).fetchone()
        assert row is not None
        if row["name"] != request.name or row["workload_id"] != request.workload_id:
            raise Conflict("idempotency_key was already used with a different task")
        return row

    def get_task(self, task_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s", (task_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"task not found: {task_id}")
        return row

    def get_job(self, job_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"job not found: {job_id}")
        return row

    def get_candidate(self, candidate_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = %s", (candidate_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"candidate not found: {candidate_id}")
        return row

    def _transition_task(
        self,
        connection: Connection[dict[str, Any]],
        task_id: UUID,
        target: TaskState,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"task not found: {task_id}")
        current = TaskState(row["state"])
        transition_task(current, target)
        updated = connection.execute(
            """
            UPDATE tasks
            SET state = %s, version = version + 1, updated_at = now()
            WHERE task_id = %s AND version = %s
            RETURNING *
            """,
            (target.value, task_id, row["version"]),
        ).fetchone()
        if updated is None:
            raise Conflict("task was changed concurrently")
        return updated

    def save_stage0(
        self,
        task_id: UUID,
        evidence: Stage0EvidenceRequest,
        mode: ProjectMode,
        reasons: tuple[str, ...],
    ) -> dict[str, Any]:
        if mode is ProjectMode.STOPPED_MEASUREMENT:
            target = TaskState.STOPPED_MEASUREMENT
        elif mode in {ProjectMode.DEGRADED_MANUAL_INTAKE, ProjectMode.CONFIG_ONLY}:
            target = TaskState.DEGRADED
        else:
            target = TaskState.BASELINE_PENDING
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["state"] != TaskState.STAGE0_PENDING.value:
                existing = connection.execute(
                    "SELECT report FROM stage0_evidence WHERE task_id = %s", (task_id,)
                ).fetchone()
                if existing is not None:
                    return existing["report"]
                raise Conflict("Stage 0 can only run from stage0_pending")
            transition_task(TaskState(task["state"]), target)
            report = {
                "task_id": str(task_id),
                "mode": mode.value,
                "reasons": list(reasons),
                "automatic_release_allowed": False,
            }
            connection.execute(
                """
                INSERT INTO stage0_evidence (task_id, evidence, report)
                VALUES (%s, %s, %s)
                """,
                (
                    task_id,
                    Jsonb(evidence.model_dump(mode="json")),
                    Jsonb(report),
                ),
            )
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, project_mode = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (target.value, mode.value, task_id),
            )
        return report

    def freeze_baseline(self, task_id: UUID, request: BaselineCreate) -> dict[str, Any]:
        epoch_id = uuid4()
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            existing = connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s", (task_id,)
            ).fetchone()
            if existing is not None:
                same = all(
                    existing[name] == getattr(request, name)
                    for name in (
                        "hardware_fingerprint",
                        "software_fingerprint",
                        "workload_id",
                        "configuration_hash",
                    )
                )
                if not same:
                    raise Conflict("task already has a different frozen baseline")
                return existing
            current = TaskState(task["state"])
            if current is TaskState.DEGRADED:
                transition_task(current, TaskState.BASELINE_PENDING)
                current = TaskState.BASELINE_PENDING
            if current is not TaskState.BASELINE_PENDING:
                raise Conflict("baseline requires a passed or degraded Stage 0")
            transition_task(current, TaskState.PROFILING)
            row = connection.execute(
                """
                INSERT INTO baseline_epochs (
                    baseline_epoch_id, task_id, hardware_fingerprint,
                    software_fingerprint, workload_id, configuration_hash
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    epoch_id,
                    task_id,
                    request.hardware_fingerprint,
                    request.software_fingerprint,
                    request.workload_id,
                    request.configuration_hash,
                ),
            ).fetchone()
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.PROFILING.value, task_id),
            )
        assert row is not None
        return row

    def get_baseline(self, task_id: UUID) -> dict[str, Any] | None:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s", (task_id,)
            ).fetchone()

    def register_worker(self, request: WorkerRegister) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO workers (
                    worker_id, worker_type, contract_version, capabilities
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (worker_id) DO UPDATE
                SET worker_type = EXCLUDED.worker_type,
                    contract_version = EXCLUDED.contract_version,
                    capabilities = EXCLUDED.capabilities,
                    state = 'online',
                    last_heartbeat_at = now()
                RETURNING *
                """,
                (
                    request.worker_id,
                    request.worker_type.value,
                    request.contract_version,
                    Jsonb(request.capabilities),
                ),
            ).fetchone()
            if request.worker_type is WorkerType.GPU:
                resource_id = str(request.capabilities.get("resource_id", "fake-hcu-0"))
                connection.execute(
                    """
                    INSERT INTO resources (resource_id)
                    VALUES (%s)
                    ON CONFLICT (resource_id) DO NOTHING
                    """,
                    (resource_id,),
                )
        assert row is not None
        return row

    def enqueue_job(self, request: JobCreate) -> dict[str, Any]:
        job_id = uuid4()
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type, lease_scope,
                    payload, idempotency_key, priority, max_attempts
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    job_id,
                    request.task_id,
                    request.job_type.value,
                    request.accepted_worker_type.value,
                    request.lease_scope.value,
                    Jsonb(request.payload),
                    request.idempotency_key,
                    request.priority,
                    request.max_attempts,
                ),
            ).fetchone()
        assert row is not None
        if row["task_id"] != request.task_id or row["job_type"] != request.job_type.value:
            raise Conflict("idempotency_key was already used with a different job")
        return row

    def claim_job(self, worker_id: str) -> dict[str, Any] | None:
        claim_token = uuid4()
        with self.connection() as connection:
            worker = connection.execute(
                "SELECT * FROM workers WHERE worker_id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker is None:
                raise NotFound(f"worker not registered: {worker_id}")
            worker_type = WorkerType(worker["worker_type"])
            job = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'queued'
                  AND available_at <= now()
                  AND accepted_worker_type = %s
                ORDER BY priority DESC, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (worker_type.value,),
            ).fetchone()
            if job is None:
                connection.execute(
                    "UPDATE workers SET last_heartbeat_at = now() WHERE worker_id = %s",
                    (worker_id,),
                )
                return None

            resource_id: str | None = None
            fencing_token: int | None = None
            if (
                worker_type is WorkerType.GPU
                and job["lease_scope"] == LeaseScope.EXCLUSIVE.value
            ):
                requested_resource = worker["capabilities"].get("resource_id")
                if requested_resource:
                    resource = connection.execute(
                        """
                        SELECT * FROM resources
                        WHERE resource_id = %s AND state = 'available'
                        FOR UPDATE SKIP LOCKED
                        """,
                        (requested_resource,),
                    ).fetchone()
                else:
                    resource = connection.execute(
                        """
                        SELECT * FROM resources WHERE state = 'available'
                        ORDER BY resource_id FOR UPDATE SKIP LOCKED LIMIT 1
                        """
                    ).fetchone()
                if resource is None:
                    return None
                resource_id = resource["resource_id"]
                fencing_token = int(resource["fencing_token"]) + 1
                connection.execute(
                    """
                    UPDATE resources
                    SET state = 'active', owner_job_id = %s, lease_id = %s,
                        fencing_token = %s, expires_at = now() + interval '90 seconds',
                        updated_at = now()
                    WHERE resource_id = %s
                    """,
                    (job["job_id"], uuid4(), fencing_token, resource_id),
                )
            claimed = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', claimed_by = %s, claim_token = %s,
                    claimed_at = now(), heartbeat_at = now(), attempts = attempts + 1,
                    resource_id = %s, fencing_token = %s, updated_at = now()
                WHERE job_id = %s AND state = 'queued'
                RETURNING *
                """,
                (worker_id, claim_token, resource_id, fencing_token, job["job_id"]),
            ).fetchone()
            connection.execute(
                "UPDATE workers SET last_heartbeat_at = now() WHERE worker_id = %s",
                (worker_id,),
            )
            connection.execute(
                """
                INSERT INTO job_events (job_id, event_type, details)
                VALUES (%s, 'claimed', %s)
                """,
                (job["job_id"], Jsonb({"worker_id": worker_id})),
            )
        return claimed

    def heartbeat_job(
        self,
        worker_id: str,
        job_id: UUID,
        claim_token: UUID,
        fencing_token: int | None,
    ) -> None:
        with self.connection() as connection:
            self._assert_job_owner(connection, job_id, claim_token, fencing_token)
            updated = connection.execute(
                """
                UPDATE jobs SET heartbeat_at = now(), updated_at = now()
                WHERE job_id = %s AND claimed_by = %s AND state = 'running'
                """,
                (job_id, worker_id),
            )
            if updated.rowcount != 1:
                raise StaleClaimToken("worker no longer owns this running job")
            connection.execute(
                "UPDATE workers SET last_heartbeat_at = now() WHERE worker_id = %s",
                (worker_id,),
            )
            if fencing_token is not None:
                connection.execute(
                    """
                    UPDATE resources
                    SET expires_at = now() + interval '90 seconds', updated_at = now()
                    WHERE owner_job_id = %s AND fencing_token = %s
                    """,
                    (job_id, fencing_token),
                )

    def _assert_job_owner(
        self,
        connection: Connection[dict[str, Any]],
        job_id: UUID,
        claim_token: UUID,
        fencing_token: int | None,
    ) -> dict[str, Any]:
        job = connection.execute(
            "SELECT * FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,)
        ).fetchone()
        if job is None:
            raise NotFound(f"job not found: {job_id}")
        if job["state"] != JobState.RUNNING.value or job["claim_token"] != claim_token:
            raise StaleClaimToken("claim token is stale or job is not running")
        if job["resource_id"] is not None:
            resource = connection.execute(
                "SELECT * FROM resources WHERE resource_id = %s FOR UPDATE",
                (job["resource_id"],),
            ).fetchone()
            if (
                resource is None
                or resource["owner_job_id"] != job_id
                or resource["fencing_token"] != fencing_token
            ):
                raise StaleFencingToken("GPU lease fencing token is stale")
        return job

    def _release_resource(
        self,
        connection: Connection[dict[str, Any]],
        job: dict[str, Any],
        reason: str,
    ) -> None:
        resource_id = job["resource_id"]
        if resource_id is None:
            return
        evidence = {"reason": reason, "cleaner": "fake-resource-cleaner-v1", "healthy": True}
        connection.execute(
            """
            UPDATE resources SET state = 'fencing', updated_at = now()
            WHERE resource_id = %s AND owner_job_id = %s AND fencing_token = %s
            """,
            (resource_id, job["job_id"], job["fencing_token"]),
        )
        connection.execute(
            """
            UPDATE resources SET state = 'health_check', cleanup_evidence = %s,
                updated_at = now()
            WHERE resource_id = %s
            """,
            (Jsonb(evidence), resource_id),
        )
        connection.execute(
            """
            UPDATE resources SET state = 'available', owner_job_id = NULL,
                lease_id = NULL, expires_at = NULL, updated_at = now()
            WHERE resource_id = %s
            """,
            (resource_id,),
        )

    def complete_job(
        self,
        job_id: UUID,
        claim_token: UUID,
        fencing_token: int | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        with self.connection() as connection:
            job = self._assert_job_owner(connection, job_id, claim_token, fencing_token)
            row = connection.execute(
                """
                UPDATE jobs SET state = 'succeeded', result = %s, finished_at = now(),
                    updated_at = now()
                WHERE job_id = %s
                RETURNING *
                """,
                (Jsonb(result), job_id),
            ).fetchone()
            self._release_resource(connection, job, "job_completed")
            connection.execute(
                "INSERT INTO job_events (job_id, event_type) VALUES (%s, 'succeeded')",
                (job_id,),
            )
        assert row is not None
        return row

    def fail_job(
        self,
        job_id: UUID,
        claim_token: UUID,
        fencing_token: int | None,
        error: dict[str, Any],
        retryable: bool,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            job = self._assert_job_owner(connection, job_id, claim_token, fencing_token)
            retry = retryable and job["attempts"] < job["max_attempts"]
            state = JobState.QUEUED.value if retry else JobState.FAILED.value
            row = connection.execute(
                """
                UPDATE jobs
                SET state = %s, last_error = %s,
                    available_at = CASE
                        WHEN %s THEN now() + interval '1 second' ELSE available_at
                    END,
                    claimed_by = NULL, claim_token = NULL, claimed_at = NULL,
                    heartbeat_at = NULL, resource_id = NULL, fencing_token = NULL,
                    finished_at = CASE WHEN %s THEN NULL ELSE now() END, updated_at = now()
                WHERE job_id = %s
                RETURNING *
                """,
                (state, Jsonb(error), retry, retry, job_id),
            ).fetchone()
            self._release_resource(connection, job, "job_failed")
            connection.execute(
                """
                INSERT INTO job_events (job_id, event_type, details)
                VALUES (%s, %s, %s)
                """,
                (job_id, "requeued" if retry else "failed", Jsonb(error)),
            )
        assert row is not None
        return row

    def recover_stale_jobs(self, stale_after_seconds: int = 120) -> list[UUID]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        recovered: list[UUID] = []
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'running' AND heartbeat_at < %s
                ORDER BY heartbeat_at
                FOR UPDATE SKIP LOCKED
                """,
                (cutoff,),
            ).fetchall()
            for job in rows:
                self._release_resource(connection, job, "worker_heartbeat_expired")
                retry = job["attempts"] < job["max_attempts"]
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = %s, claimed_by = NULL, claim_token = NULL,
                        claimed_at = NULL, heartbeat_at = NULL, resource_id = NULL,
                        fencing_token = NULL, available_at = now(),
                        last_error = %s, finished_at = CASE WHEN %s THEN NULL ELSE now() END,
                        updated_at = now()
                    WHERE job_id = %s
                    """,
                    (
                        JobState.QUEUED.value if retry else JobState.FAILED.value,
                        Jsonb({"code": "worker_lost", "message": "heartbeat expired"}),
                        retry,
                        job["job_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (job_id, event_type, details)
                    VALUES (%s, 'fenced_after_worker_loss', %s)
                    """,
                    (job["job_id"], Jsonb({"old_fencing_token": job["fencing_token"]})),
                )
                recovered.append(job["job_id"])
        return recovered

    def record_hotspot(
        self, task_id: UUID, baseline_epoch_id: UUID, result: dict[str, Any]
    ) -> dict[str, Any]:
        hotspot_id = uuid4()
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO hotspots (
                    hotspot_id, task_id, baseline_epoch_id, symbol, share_ratio,
                    opportunity_score, patchability, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, symbol) DO UPDATE
                SET evidence = EXCLUDED.evidence
                RETURNING *
                """,
                (
                    hotspot_id,
                    task_id,
                    baseline_epoch_id,
                    result["symbol"],
                    result["share_ratio"],
                    result["opportunity_score"],
                    result["patchability"],
                    Jsonb(result),
                ),
            ).fetchone()
        assert row is not None
        return row

    def create_candidates(
        self,
        task_id: UUID,
        baseline_epoch_id: UUID,
        round_id: UUID,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        with self.connection() as connection:
            for item in candidates:
                candidate_id = UUID(item["candidate_id"])
                row = connection.execute(
                    """
                    INSERT INTO candidates (
                        candidate_id, task_id, round_id, baseline_epoch_id,
                        source_hash, variant, state, ordinal, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (candidate_id) DO UPDATE
                    SET candidate_id = EXCLUDED.candidate_id
                    RETURNING *
                    """,
                    (
                        candidate_id,
                        task_id,
                        round_id,
                        baseline_epoch_id,
                        item["source_hash"],
                        item["variant"],
                        CandidateState.PROPOSED.value,
                        item["ordinal"],
                        Jsonb(item.get("metadata", {})),
                    ),
                ).fetchone()
                assert row is not None
                created.append(row)
        return created

    def transition_candidate(
        self, candidate_id: UUID, target: CandidateState
    ) -> dict[str, Any]:
        with self.connection() as connection:
            current = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = %s FOR UPDATE", (candidate_id,)
            ).fetchone()
            if current is None:
                raise NotFound(f"candidate not found: {candidate_id}")
            transition_candidate(CandidateState(current["state"]), target)
            row = connection.execute(
                """
                UPDATE candidates SET state = %s, updated_at = now()
                WHERE candidate_id = %s RETURNING *
                """,
                (target.value, candidate_id),
            ).fetchone()
        assert row is not None
        return row

    def transition_task(self, task_id: UUID, target: TaskState) -> dict[str, Any]:
        with self.connection() as connection:
            return self._transition_task(connection, task_id, target)

    def record_artifact(
        self, task_id: UUID, candidate_id: UUID, result: dict[str, Any]
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, candidate_id, kind, uri, content_hash, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (candidate_id, content_hash) DO UPDATE
                SET content_hash = EXCLUDED.content_hash
                RETURNING *
                """,
                (
                    uuid4(),
                    task_id,
                    candidate_id,
                    result["kind"],
                    result["uri"],
                    result["content_hash"],
                    Jsonb(result.get("metadata", {})),
                ),
            ).fetchone()
        assert row is not None
        return row

    def record_evaluation(self, run: EvaluationRun) -> dict[str, Any]:
        evidence_uri = run.evidence_uris[0] if run.evidence_uris else None
        measurement = (
            Jsonb(run.measurement.model_dump(mode="json"))
            if run.measurement is not None
            else None
        )
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO evaluation_runs (
                    evaluation_run_id, task_id, candidate_id, round_id,
                    baseline_epoch_id, phase, passed, protocol_version,
                    target_fingerprint, idempotency_key, metrics, measurement,
                    evidence_uri, evidence_uris, adapter_provenance, synthetic,
                    created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    run.evaluation_run_id,
                    run.task_id,
                    run.candidate_id,
                    run.round_id,
                    run.baseline_epoch_id,
                    run.phase,
                    run.passed,
                    run.protocol_version,
                    run.target_fingerprint,
                    run.idempotency_key,
                    Jsonb(run.metrics),
                    measurement,
                    evidence_uri,
                    Jsonb(run.evidence_uris),
                    Jsonb(
                        [item.model_dump(mode="json") for item in run.adapter_provenance]
                    ),
                    run.synthetic,
                    run.created_at,
                ),
            ).fetchone()
        assert row is not None
        expected_measurement = (
            run.measurement.model_dump(mode="json")
            if run.measurement is not None
            else None
        )
        expected_provenance = [
            item.model_dump(mode="json") for item in run.adapter_provenance
        ]
        if any(
            (
                row["task_id"] != run.task_id,
                row["candidate_id"] != run.candidate_id,
                row["round_id"] != run.round_id,
                row["baseline_epoch_id"] != run.baseline_epoch_id,
                row["phase"] != run.phase,
                row["protocol_version"] != run.protocol_version,
                row["target_fingerprint"] != run.target_fingerprint,
                row["passed"] != run.passed,
                row["metrics"] != run.metrics,
                row["measurement"] != expected_measurement,
                row["evidence_uris"] != run.evidence_uris,
                row["adapter_provenance"] != expected_provenance,
                row["synthetic"] != run.synthetic,
            )
        ):
            raise Conflict("evaluation idempotency_key was reused with different inputs")
        return row

    def record_execution_attempt(self, attempt: ExecutionAttempt) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO execution_attempts (
                    execution_attempt_id, evaluation_run_id, request_id,
                    attempt_number, status, exit_code, started_at, finished_at,
                    stdout_uri, stderr_uri, result_metadata, adapter_provenance,
                    synthetic
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (evaluation_run_id, attempt_number) DO UPDATE
                SET attempt_number = EXCLUDED.attempt_number
                RETURNING *
                """,
                (
                    attempt.execution_attempt_id,
                    attempt.evaluation_run_id,
                    attempt.request_id,
                    attempt.attempt_number,
                    attempt.status,
                    attempt.exit_code,
                    attempt.started_at,
                    attempt.finished_at,
                    attempt.stdout_uri,
                    attempt.stderr_uri,
                    Jsonb(attempt.result_metadata),
                    Jsonb(attempt.adapter_provenance.model_dump(mode="json")),
                    attempt.synthetic,
                ),
            ).fetchone()
        assert row is not None
        expected_provenance = attempt.adapter_provenance.model_dump(mode="json")
        if any(
            (
                row["request_id"] != attempt.request_id,
                row["status"] != attempt.status,
                row["exit_code"] != attempt.exit_code,
                row["started_at"] != attempt.started_at,
                row["finished_at"] != attempt.finished_at,
                row["stdout_uri"] != attempt.stdout_uri,
                row["stderr_uri"] != attempt.stderr_uri,
                row["result_metadata"] != attempt.result_metadata,
                row["adapter_provenance"] != expected_provenance,
                row["synthetic"] != attempt.synthetic,
            )
        ):
            raise Conflict("execution attempt number was reused with a different request")
        return row

    def list_candidates(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM candidates WHERE task_id = %s ORDER BY ordinal", (task_id,)
            ).fetchall()

    def list_artifacts(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM artifacts WHERE task_id = %s ORDER BY created_at", (task_id,)
            ).fetchall()

    def list_evaluations(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM evaluation_runs WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()

    def list_execution_attempts(self, evaluation_run_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM execution_attempts
                WHERE evaluation_run_id = %s
                ORDER BY attempt_number
                """,
                (evaluation_run_id,),
            ).fetchall()

    def cancel_job(self, job_id: UUID, reason: str) -> dict[str, Any]:
        with self.connection() as connection:
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFound(f"job not found: {job_id}")
            if job["state"] in {
                JobState.SUCCEEDED.value,
                JobState.FAILED.value,
                JobState.CANCELLED.value,
            }:
                return job
            if job["state"] == JobState.RUNNING.value:
                self._release_resource(connection, job, "job_cancelled")
            row = connection.execute(
                """
                UPDATE jobs
                SET state = 'cancelled', last_error = %s, claimed_by = NULL,
                    claim_token = NULL, resource_id = NULL, fencing_token = NULL,
                    finished_at = now(), updated_at = now()
                WHERE job_id = %s RETURNING *
                """,
                (Jsonb({"code": "cancelled", "message": reason}), job_id),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO job_events (job_id, event_type, details)
                VALUES (%s, 'cancelled', %s)
                """,
                (job_id, Jsonb({"reason": reason})),
            )
        assert row is not None
        return row

    def task_summary(self, task_id: UUID) -> dict[str, Any]:
        task = self.get_task(task_id)
        with self.connection() as connection:
            baseline = connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s", (task_id,)
            ).fetchone()
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE task_id = %s ORDER BY created_at", (task_id,)
            ).fetchall()
            candidates = connection.execute(
                "SELECT * FROM candidates WHERE task_id = %s ORDER BY ordinal", (task_id,)
            ).fetchall()
            artifacts = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = %s ORDER BY created_at", (task_id,)
            ).fetchall()
            evaluations = connection.execute(
                "SELECT * FROM evaluation_runs WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return {
            "task": task,
            "baseline": baseline,
            "jobs": jobs,
            "candidates": candidates,
            "artifacts": artifacts,
            "evaluations": evaluations,
        }

    def list_resources(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute("SELECT * FROM resources ORDER BY resource_id").fetchall()

    def mark_job_advanced(self, job_id: UUID) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE jobs SET workflow_advanced_at = now(), updated_at = now()
                WHERE job_id = %s
                """,
                (job_id,),
            )

    def unadvanced_succeeded_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'succeeded' AND workflow_advanced_at IS NULL
                ORDER BY finished_at
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
