from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hcuopt.contracts.platform_v1 import (
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    ExecutionAttempt,
    ExecutionRequest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.contracts.v1 import (
    BaselineCreate,
    FrameworkSmokeCreate,
    JobCreate,
    Stage0EvidenceRequest,
    TaskCreate,
    WorkerRegister,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobState,
    JobType,
    LeaseScope,
    ProjectMode,
    TaskState,
    WorkerType,
    WorkflowType,
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

    @staticmethod
    def _target_fingerprint(target: TargetSpec) -> str:
        encoded = json.dumps(
            target.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def _upsert_target_snapshot(
        self,
        connection: Connection[dict[str, Any]],
        target: TargetSpec,
        source_path: str,
    ) -> dict[str, Any]:
        fingerprint = self._target_fingerprint(target)
        snapshot_id = uuid5(
            NAMESPACE_URL, f"hcuopt:target:{target.target_id}:{fingerprint}"
        )
        row = connection.execute(
            """
            INSERT INTO target_snapshots (
                target_snapshot_id, target_id, target_fingerprint,
                specification, source_path
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (target_id, target_fingerprint) DO UPDATE
            SET target_fingerprint = EXCLUDED.target_fingerprint
            RETURNING *
            """,
            (
                snapshot_id,
                target.target_id,
                fingerprint,
                Jsonb(target.model_dump(mode="json")),
                source_path,
            ),
        ).fetchone()
        assert row is not None
        if row["specification"] != target.model_dump(mode="json"):
            raise Conflict("target fingerprint was reused with a different specification")
        return row

    def upsert_target_snapshot(
        self, target: TargetSpec, source_path: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            return self._upsert_target_snapshot(connection, target, source_path)

    def create_framework_smoke_task(
        self,
        request: FrameworkSmokeCreate,
        target: TargetSpec,
        source_path: str,
    ) -> dict[str, Any]:
        task_id = uuid4()
        with self.connection() as connection:
            snapshot = self._upsert_target_snapshot(connection, target, source_path)
            row = connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile
                ) VALUES (
                    %s, %s, %s, %s, %s, '{}'::jsonb, FALSE,
                    %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    task_id,
                    request.name,
                    f"framework-smoke:{target.target_id}",
                    request.idempotency_key,
                    TaskState.SOURCE_PREPARING.value,
                    WorkflowType.FRAMEWORK_SMOKE.value,
                    target.target_id,
                    snapshot["target_snapshot_id"],
                    request.adapter_profile,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = %s FOR UPDATE",
                    (request.idempotency_key,),
                ).fetchone()
                assert row is not None
                expected = {
                    "name": request.name,
                    "workflow_type": WorkflowType.FRAMEWORK_SMOKE.value,
                    "target_id": target.target_id,
                    "target_snapshot_id": snapshot["target_snapshot_id"],
                    "adapter_profile": request.adapter_profile,
                }
                if any(row[name] != value for name, value in expected.items()):
                    raise Conflict(
                        "framework smoke idempotency_key was reused with different inputs"
                    )
                return row

            job_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:source-prepare:v1")
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    adapter_profile, lease_scope, payload, idempotency_key,
                    priority, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, 'none', %s, %s, 0, 3)
                """,
                (
                    job_id,
                    task_id,
                    JobType.SOURCE_PREPARE.value,
                    WorkerType.BUILD.value,
                    request.adapter_profile,
                    Jsonb(
                        {
                            "task_id": str(task_id),
                            "target": target.model_dump(mode="json"),
                            "target_snapshot_id": str(snapshot["target_snapshot_id"]),
                        }
                    ),
                    f"{task_id}:source-prepare:v1",
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'framework_smoke_created', %s)
                """,
                (
                    task_id,
                    Jsonb(
                        {
                            "target_id": target.target_id,
                            "target_fingerprint": snapshot["target_fingerprint"],
                            "adapter_profile": request.adapter_profile,
                            "first_job_id": str(job_id),
                        }
                    ),
                ),
            )
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

    def get_target_snapshot(self, task_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT snapshot.*
                FROM tasks AS task
                JOIN target_snapshots AS snapshot
                  ON snapshot.target_snapshot_id = task.target_snapshot_id
                WHERE task.task_id = %s
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"target snapshot not found for task: {task_id}")
        return row

    def ensure_framework_baseline(
        self,
        task_id: UUID,
        target: TargetSpec,
        source: SourceSnapshot,
    ) -> dict[str, Any]:
        epoch_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:framework-baseline:v1")
        hardware_payload = {
            "host": target.execution_host.name,
            "accelerator": target.execution_host.accelerator.model,
            "architecture": target.execution_host.accelerator.architecture,
            "device": target.execution_host.accelerator.device_index,
        }
        software_payload = {
            "image": target.inference_image.registry_digest,
            "source": source.source_hash,
            "dtk": target.inference_image.dtk_version,
            "sglang": target.inference_image.sglang_package_version,
        }

        def digest(value: dict[str, Any]) -> str:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            return "sha256:" + hashlib.sha256(encoded).hexdigest()

        expected = {
            "hardware_fingerprint": digest(hardware_payload),
            "software_fingerprint": digest(software_payload),
            "workload_id": f"framework-smoke:{target.target_id}",
            "configuration_hash": self._target_fingerprint(target),
            "baseline_kind": WorkflowType.FRAMEWORK_SMOKE.value,
        }
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO baseline_epochs (
                    baseline_epoch_id, task_id, hardware_fingerprint,
                    software_fingerprint, workload_id, configuration_hash,
                    baseline_kind
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id) DO NOTHING
                RETURNING *
                """,
                (
                    epoch_id,
                    task_id,
                    expected["hardware_fingerprint"],
                    expected["software_fingerprint"],
                    expected["workload_id"],
                    expected["configuration_hash"],
                    expected["baseline_kind"],
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM baseline_epochs WHERE task_id = %s", (task_id,)
                ).fetchone()
        assert row is not None
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("task already has a different immutable framework baseline")
        return row

    def record_source_snapshot(
        self,
        task_id: UUID,
        source: SourceSnapshot,
        provenance: list[dict[str, Any]],
        synthetic: bool,
        idempotency_key: str,
        candidate_id: UUID | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO source_snapshots (
                    snapshot_id, task_id, candidate_id, kind, repository,
                    commit, tree_hash, source_hash, worktree_uri, clean,
                    parent_snapshot_id, idempotency_key, adapter_provenance,
                    synthetic, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    source.snapshot_id,
                    task_id,
                    candidate_id,
                    source.kind,
                    source.repository,
                    source.commit,
                    source.tree_hash,
                    source.source_hash,
                    source.worktree_uri,
                    source.clean,
                    source.parent_snapshot_id,
                    idempotency_key,
                    Jsonb(provenance),
                    synthetic,
                    source.created_at,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "snapshot_id": source.snapshot_id,
            "task_id": task_id,
            "candidate_id": candidate_id,
            "source_hash": source.source_hash,
            "adapter_provenance": provenance,
            "synthetic": synthetic,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("source snapshot idempotency_key was reused with different inputs")
        return row

    def create_noop_candidate(
        self,
        task_id: UUID,
        baseline_epoch_id: UUID,
        source: SourceSnapshot,
    ) -> dict[str, Any]:
        candidate_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:framework-noop:v1")
        round_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:framework-round:v1")
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO candidates (
                    candidate_id, task_id, round_id, baseline_epoch_id,
                    source_hash, variant, state, ordinal, metadata
                ) VALUES (%s, %s, %s, %s, %s, 'framework-noop', %s, 0, %s)
                ON CONFLICT (candidate_id) DO UPDATE
                SET candidate_id = EXCLUDED.candidate_id
                RETURNING *
                """,
                (
                    candidate_id,
                    task_id,
                    round_id,
                    baseline_epoch_id,
                    source.source_hash,
                    CandidateState.PROPOSED.value,
                    Jsonb(
                        {
                            "workflow_type": WorkflowType.FRAMEWORK_SMOKE.value,
                            "baseline_source_snapshot_id": str(source.snapshot_id),
                            "no_op": True,
                        }
                    ),
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "task_id": task_id,
            "baseline_epoch_id": baseline_epoch_id,
            "source_hash": source.source_hash,
            "variant": "framework-noop",
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("deterministic no-op candidate conflicts with persisted state")
        return row

    def register_worker(self, request: WorkerRegister) -> dict[str, Any]:
        declared_profile = request.adapter_profile
        capability_profile = request.capabilities.get("adapter_profile")
        if declared_profile is not None and capability_profile not in {None, declared_profile}:
            raise Conflict("worker adapter_profile conflicts with capabilities metadata")
        adapter_profile = declared_profile or capability_profile
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO workers (
                    worker_id, worker_type, contract_version, adapter_profile, capabilities
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (worker_id) DO UPDATE
                SET worker_type = EXCLUDED.worker_type,
                    contract_version = EXCLUDED.contract_version,
                    adapter_profile = EXCLUDED.adapter_profile,
                    capabilities = EXCLUDED.capabilities,
                    state = 'online',
                    last_heartbeat_at = now()
                RETURNING *
                """,
                (
                    request.worker_id,
                    request.worker_type.value,
                    request.contract_version,
                    adapter_profile,
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
                    job_id, task_id, job_type, accepted_worker_type, adapter_profile, lease_scope,
                    payload, idempotency_key, priority, max_attempts
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    job_id,
                    request.task_id,
                    request.job_type.value,
                    request.accepted_worker_type.value,
                    request.adapter_profile,
                    request.lease_scope.value,
                    Jsonb(request.payload),
                    request.idempotency_key,
                    request.priority,
                    request.max_attempts,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "task_id": request.task_id,
            "job_type": request.job_type.value,
            "accepted_worker_type": request.accepted_worker_type.value,
            "adapter_profile": request.adapter_profile,
            "lease_scope": request.lease_scope.value,
            "payload": request.payload,
            "priority": request.priority,
            "max_attempts": request.max_attempts,
        }
        if any(row[name] != value for name, value in expected.items()):
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
                  AND (adapter_profile IS NULL OR adapter_profile = %s)
                ORDER BY priority DESC, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (worker_type.value, worker["adapter_profile"]),
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
            existing = connection.execute(
                "SELECT * FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if existing is None:
                raise NotFound(f"job not found: {job_id}")
            if existing["state"] == JobState.SUCCEEDED.value:
                if existing["claim_token"] != claim_token:
                    raise StaleClaimToken("completion replay used a stale claim token")
                if existing["fencing_token"] != fencing_token:
                    raise StaleFencingToken("completion replay used a stale fencing token")
                if existing["result"] != result:
                    raise Conflict("completed job was replayed with a different result")
                return existing
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

    def record_artifact_manifest(
        self,
        task_id: UUID,
        manifest: ArtifactManifest,
        provenance: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, candidate_id, kind, uri, content_hash,
                    source_snapshot_id, build_recipe, metadata, sbom_uri,
                    signature_uri, adapter_provenance, synthetic, idempotency_key
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    manifest.artifact_id,
                    task_id,
                    manifest.candidate_id,
                    manifest.kind,
                    manifest.uri,
                    manifest.content_hash,
                    manifest.source_snapshot_id,
                    Jsonb(manifest.build_recipe),
                    Jsonb(manifest.metadata),
                    manifest.sbom_uri,
                    manifest.signature_uri,
                    Jsonb(provenance),
                    manifest.synthetic,
                    idempotency_key,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "artifact_id": manifest.artifact_id,
            "task_id": task_id,
            "candidate_id": manifest.candidate_id,
            "source_snapshot_id": manifest.source_snapshot_id,
            "content_hash": manifest.content_hash,
            "adapter_provenance": provenance,
            "synthetic": manifest.synthetic,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("artifact idempotency_key was reused with different inputs")
        return row

    def record_execution_request(
        self,
        task_id: UUID,
        candidate_id: UUID,
        evaluation_run_id: UUID,
        request: ExecutionRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request_data = request.model_dump(mode="json")
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO execution_requests (
                    request_id, task_id, candidate_id, evaluation_run_id,
                    target_id, request, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    request.request_id,
                    task_id,
                    candidate_id,
                    evaluation_run_id,
                    request.target_id,
                    Jsonb(request_data),
                    idempotency_key,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "request_id": request.request_id,
            "task_id": task_id,
            "candidate_id": candidate_id,
            "evaluation_run_id": evaluation_run_id,
            "target_id": request.target_id,
            "request": request_data,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("execution request idempotency_key was reused with different inputs")
        return row

    def record_evidence_bundle(
        self,
        bundle: EvidenceBundle,
        evaluation_run_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        provenance = [item.model_dump(mode="json") for item in bundle.adapter_provenance]
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO evidence_bundles (
                    evidence_id, task_id, candidate_id, baseline_epoch_id,
                    evaluation_run_id,
                    target_id, evidence_type, protocol_version, artifact_ids,
                    measurement_ids, summary, raw_uris, adapter_provenance,
                    synthetic, idempotency_key, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    bundle.evidence_id,
                    bundle.task_id,
                    bundle.candidate_id,
                    bundle.baseline_epoch_id,
                    evaluation_run_id,
                    bundle.target_id,
                    bundle.evidence_type,
                    bundle.protocol_version,
                    Jsonb([str(item) for item in bundle.artifact_ids]),
                    Jsonb([str(item) for item in bundle.measurement_ids]),
                    Jsonb(bundle.summary),
                    Jsonb(bundle.raw_uris),
                    Jsonb(provenance),
                    bundle.synthetic,
                    idempotency_key,
                    bundle.created_at,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "evidence_id": bundle.evidence_id,
            "task_id": bundle.task_id,
            "candidate_id": bundle.candidate_id,
            "baseline_epoch_id": bundle.baseline_epoch_id,
            "evaluation_run_id": evaluation_run_id,
            "summary": bundle.summary,
            "adapter_provenance": provenance,
            "synthetic": bundle.synthetic,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("evidence idempotency_key was reused with different inputs")
        return row

    def enqueue_framework_execution(
        self,
        task_id: UUID,
        *,
        retest: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["workflow_type"] != WorkflowType.FRAMEWORK_SMOKE.value:
                raise Conflict("task is not a Framework Smoke workflow")

            current = TaskState(task["state"])
            if retest:
                if current is not TaskState.AWAITING_SIGNOFF:
                    raise Conflict("Framework Smoke retest requires awaiting_signoff")
                target_state = TaskState.FRAMEWORK_RETESTING
                retest_ordinal = int(task["retest_count"]) + 1
            else:
                if current not in {
                    TaskState.ARTIFACT_PREPARING,
                    TaskState.FRAMEWORK_EXECUTING,
                }:
                    raise Conflict("initial Framework Smoke execution requires prepared artifact")
                target_state = TaskState.FRAMEWORK_EXECUTING
                retest_ordinal = 0
                if current is TaskState.FRAMEWORK_EXECUTING:
                    existing_job = connection.execute(
                        "SELECT * FROM jobs WHERE idempotency_key = %s",
                        (f"{task_id}:framework-smoke:0:v1",),
                    ).fetchone()
                    if existing_job is None:
                        raise Conflict(
                            "Framework Smoke task is executing without its durable job"
                        )
                    return existing_job

            data = connection.execute(
                """
                SELECT candidate.*, baseline.hardware_fingerprint,
                       baseline.software_fingerprint, baseline.configuration_hash,
                       artifact.artifact_id, artifact.kind AS artifact_kind,
                       artifact.uri AS artifact_uri,
                       artifact.content_hash AS artifact_content_hash,
                       artifact.source_snapshot_id,
                       artifact.build_recipe, artifact.metadata AS artifact_metadata,
                       artifact.sbom_uri, artifact.signature_uri,
                       artifact.synthetic AS artifact_synthetic,
                       snapshot.specification AS target_specification,
                       snapshot.target_fingerprint
                FROM candidates AS candidate
                JOIN baseline_epochs AS baseline
                  ON baseline.baseline_epoch_id = candidate.baseline_epoch_id
                JOIN artifacts AS artifact
                  ON artifact.candidate_id = candidate.candidate_id
                JOIN target_snapshots AS snapshot
                  ON snapshot.target_snapshot_id = %s
                WHERE candidate.task_id = %s
                  AND candidate.variant = 'framework-noop'
                ORDER BY artifact.created_at DESC
                LIMIT 1
                """,
                (task["target_snapshot_id"], task_id),
            ).fetchone()
            if data is None:
                raise Conflict("Framework Smoke execution requires candidate and artifact")

            candidate_state = CandidateState(data["state"])
            if candidate_state is not CandidateState.FRAMEWORK_SMOKE_RUNNING:
                transition_candidate(
                    candidate_state, CandidateState.FRAMEWORK_SMOKE_RUNNING
                )
                connection.execute(
                    """
                    UPDATE candidates SET state = %s, updated_at = now()
                    WHERE candidate_id = %s
                    """,
                    (
                        CandidateState.FRAMEWORK_SMOKE_RUNNING.value,
                        data["candidate_id"],
                    ),
                )

            if current is not target_state:
                transition_task(current, target_state)
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, retest_count = %s,
                    version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (target_state.value, retest_ordinal, task_id),
            )

            run_key = f"{task_id}:framework-smoke:{retest_ordinal}:v1"
            evaluation_run_id = uuid5(NAMESPACE_URL, run_key + ":evaluation")
            execution_request_id = uuid5(NAMESPACE_URL, run_key + ":request")
            evidence_id = uuid5(NAMESPACE_URL, run_key + ":evidence")
            job_id = uuid5(NAMESPACE_URL, run_key + ":job")
            payload = {
                "task_id": str(task_id),
                "candidate_id": str(data["candidate_id"]),
                "round_id": str(data["round_id"]),
                "baseline_epoch_id": str(data["baseline_epoch_id"]),
                "target": data["target_specification"],
                "target_fingerprint": data["target_fingerprint"],
                "artifact": {
                    "artifact_id": str(data["artifact_id"]),
                    "candidate_id": str(data["candidate_id"]),
                    "kind": data["artifact_kind"],
                    "uri": data["artifact_uri"],
                    "content_hash": data["artifact_content_hash"],
                    "source_snapshot_id": str(data["source_snapshot_id"]),
                    "build_recipe": data["build_recipe"],
                    "metadata": data["artifact_metadata"],
                    "sbom_uri": data["sbom_uri"],
                    "signature_uri": data["signature_uri"],
                    "synthetic": data["artifact_synthetic"],
                },
                "evaluation_run_id": str(evaluation_run_id),
                "execution_request_id": str(execution_request_id),
                "evidence_id": str(evidence_id),
                "retest_ordinal": retest_ordinal,
            }
            job = connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    adapter_profile, lease_scope, payload, idempotency_key,
                    priority, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 3)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    job_id,
                    task_id,
                    JobType.FRAMEWORK_SMOKE.value,
                    WorkerType.GPU.value,
                    task["adapter_profile"],
                    LeaseScope.EXCLUSIVE.value,
                    Jsonb(payload),
                    run_key,
                ),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, %s, %s)
                """,
                (
                    task_id,
                    "framework_smoke_retest_enqueued"
                    if retest
                    else "framework_smoke_execution_enqueued",
                    Jsonb(
                        {
                            "job_id": str(job_id),
                            "retest_ordinal": retest_ordinal,
                            "reason": reason,
                        }
                    ),
                ),
            )
        assert job is not None
        return job

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

    def list_source_snapshots(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM source_snapshots WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()

    def list_execution_requests(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM execution_requests WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()

    def list_execution_attempts_for_task(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT attempt.*
                FROM execution_attempts AS attempt
                JOIN evaluation_runs AS run
                  ON run.evaluation_run_id = attempt.evaluation_run_id
                WHERE run.task_id = %s
                ORDER BY run.created_at, attempt.attempt_number
                """,
                (task_id,),
            ).fetchall()

    def list_evidence_bundles(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM evidence_bundles WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()

    def list_task_events(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = %s ORDER BY created_at, event_id
                """,
                (task_id,),
            ).fetchall()

    def record_task_event(
        self, task_id: UUID, event_type: str, details: dict[str, Any]
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, %s, %s) RETURNING *
                """,
                (task_id, event_type, Jsonb(details)),
            ).fetchone()
        assert row is not None
        return row

    def cancel_framework_task(self, task_id: UUID, reason: str) -> dict[str, Any]:
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["workflow_type"] != WorkflowType.FRAMEWORK_SMOKE.value:
                raise Conflict("task is not a Framework Smoke workflow")
            if task["state"] == TaskState.CANCELLED.value:
                return task
            if task["state"] in {TaskState.COMPLETED.value, TaskState.REJECTED.value}:
                raise Conflict("terminal Framework Smoke task cannot be cancelled")
            transition_task(TaskState(task["state"]), TaskState.CANCELLED)
            jobs = connection.execute(
                """
                SELECT * FROM jobs
                WHERE task_id = %s AND state IN ('queued', 'running')
                FOR UPDATE
                """,
                (task_id,),
            ).fetchall()
            for job in jobs:
                if job["state"] == JobState.RUNNING.value:
                    self._release_resource(connection, job, "task_cancelled")
            connection.execute(
                """
                UPDATE jobs
                SET state = 'cancelled', last_error = %s, claimed_by = NULL,
                    claim_token = NULL, claimed_at = NULL, heartbeat_at = NULL,
                    resource_id = NULL, fencing_token = NULL, finished_at = now(),
                    updated_at = now()
                WHERE task_id = %s AND state IN ('queued', 'running')
                """,
                (Jsonb({"code": "task_cancelled", "message": reason}), task_id),
            )
            connection.execute(
                """
                UPDATE candidates SET state = %s, updated_at = now()
                WHERE task_id = %s AND state <> %s
                """,
                (CandidateState.REJECTED.value, task_id, CandidateState.REJECTED.value),
            )
            updated = connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s RETURNING *
                """,
                (TaskState.CANCELLED.value, task_id),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'framework_smoke_cancelled', %s)
                """,
                (task_id, Jsonb({"reason": reason})),
            )
        assert updated is not None
        return updated

    def framework_smoke_summary(self, task_id: UUID) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task.get("workflow_type") != WorkflowType.FRAMEWORK_SMOKE.value:
            raise Conflict("task is not a Framework Smoke workflow")
        target = self.get_target_snapshot(task_id)
        return {
            "task": task,
            "target": target["specification"],
            "baseline": self.get_baseline(task_id),
            "source_snapshots": self.list_source_snapshots(task_id),
            "candidates": self.list_candidates(task_id),
            "artifacts": self.list_artifacts(task_id),
            "execution_requests": self.list_execution_requests(task_id),
            "execution_attempts": self.list_execution_attempts_for_task(task_id),
            "evaluations": self.list_evaluations(task_id),
            "evidence_bundles": self.list_evidence_bundles(task_id),
            "events": self.list_task_events(task_id),
        }

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

    def unadvanced_succeeded_jobs(
        self, workflow_type: WorkflowType | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            workflow_filter = (
                "AND task.workflow_type = %s" if workflow_type is not None else ""
            )
            parameters: tuple[Any, ...] = (
                (workflow_type.value, limit)
                if workflow_type is not None
                else (limit,)
            )
            return connection.execute(
                f"""
                SELECT job.* FROM jobs AS job
                JOIN tasks AS task ON task.task_id = job.task_id
                WHERE job.state = 'succeeded'
                  AND job.workflow_advanced_at IS NULL
                  {workflow_filter}
                ORDER BY job.finished_at
                LIMIT %s
                """,
                parameters,
            ).fetchall()
