# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from hcuopt.adapters.resource_cleaner import cleanup_is_healthy
from hcuopt.contracts.endpoint_control_v1 import (
    EndpointValidationJobResult,
    EndpointValidationRunCreate,
    endpoint_create_plan_hash,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobState,
    JobType,
    LeaseScope,
    TaskState,
    WorkerType,
    WorkflowType,
)
from hcuopt.domain.errors import Conflict, NotFound
from hcuopt.domain.transitions import transition_task


class EndpointValidationRepositoryMixin:
    """Durable endpoint Run authority layered on the generic Job/Lease queue."""

    @staticmethod
    def _verify_signed_m1(connection: Any, request: EndpointValidationRunCreate) -> None:
        signed = request.signed_m1
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = %s FOR SHARE", (signed.task_id,)
        ).fetchone()
        if task is None:
            raise NotFound(f"signed M1 task not found: {signed.task_id}")
        if (
            task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value
            or task["state"] != TaskState.COMPLETED.value
            or task["target_id"] != signed.target_id
            or task["target_snapshot_id"] != signed.target_snapshot_id
            or task["automatic_release_allowed"] is not False
        ):
            raise Conflict("endpoint Run requires the completed non-releasing M1 task")
        target = connection.execute(
            "SELECT * FROM target_snapshots WHERE target_snapshot_id = %s FOR SHARE",
            (signed.target_snapshot_id,),
        ).fetchone()
        candidate = connection.execute(
            "SELECT * FROM candidates WHERE candidate_id = %s FOR SHARE",
            (signed.candidate_id,),
        ).fetchone()
        baseline = connection.execute(
            "SELECT * FROM baseline_epochs WHERE baseline_epoch_id = %s FOR SHARE",
            (signed.baseline_epoch_id,),
        ).fetchone()
        source = connection.execute(
            """
            SELECT * FROM source_snapshots
            WHERE task_id = %s AND candidate_id = %s AND kind = 'candidate'
            FOR SHARE
            """,
            (signed.task_id, signed.candidate_id),
        ).fetchone()
        artifact = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = %s FOR SHARE",
            (signed.artifact_id,),
        ).fetchone()
        evidence = connection.execute(
            "SELECT * FROM evidence_bundles WHERE evidence_id = %s FOR SHARE",
            (signed.evidence_bundle_id,),
        ).fetchone()
        signoff = connection.execute(
            "SELECT * FROM manual_candidate_signoffs WHERE signoff_id = %s FOR SHARE",
            (signed.signoff_id,),
        ).fetchone()
        if target is None or (
            target["target_id"] != signed.target_id
            or target["target_fingerprint"] != signed.target_fingerprint
        ):
            raise Conflict("signed M1 Target Snapshot identity differs")
        if candidate is None or (
            candidate["task_id"] != signed.task_id
            or candidate["baseline_epoch_id"] != signed.baseline_epoch_id
            or candidate["state"] != CandidateState.ACCEPTED.value
            or candidate["evidence_bundle_id"] != signed.evidence_bundle_id
        ):
            raise Conflict("signed M1 Candidate identity or accepted state differs")
        if baseline is None or baseline["task_id"] != signed.task_id:
            raise Conflict("signed M1 Baseline Epoch differs")
        if source is None or source["source_hash"] != signed.candidate_source_hash:
            raise Conflict("signed M1 Candidate Source Hash differs")
        if artifact is None or (
            artifact["task_id"] != signed.task_id
            or artifact["candidate_id"] != signed.candidate_id
            or artifact["content_hash"] != signed.artifact_hash
            or artifact["synthetic"]
        ):
            raise Conflict("signed M1 Artifact identity differs")
        if evidence is None or (
            evidence["task_id"] != signed.task_id
            or evidence["candidate_id"] != signed.candidate_id
            or evidence["baseline_epoch_id"] != signed.baseline_epoch_id
            or evidence["synthetic"]
        ):
            raise Conflict("signed M1 EvidenceBundle identity differs")
        if signoff is None or (
            signoff["task_id"] != signed.task_id
            or signoff["candidate_id"] != signed.candidate_id
            or signoff["evidence_bundle_id"] != signed.evidence_bundle_id
            or signoff["decision"] != "approved"
        ):
            raise Conflict("signed M1 approval identity differs")

    def create_endpoint_validation_run(
        self, request: EndpointValidationRunCreate
    ) -> dict[str, Any]:
        endpoint_run_id = uuid5(
            NAMESPACE_URL, f"hcuopt:endpoint-run:{request.idempotency_key}"
        )
        task_id = uuid5(NAMESPACE_URL, f"hcuopt:endpoint-task:{request.idempotency_key}")
        job_id = uuid5(NAMESPACE_URL, f"hcuopt:endpoint-job:{request.idempotency_key}")
        plan_hash = endpoint_create_plan_hash(request)
        request_payload = request.model_dump(mode="json")
        with self.connection() as connection:
            replay = connection.execute(
                """
                SELECT * FROM endpoint_validation_runs
                WHERE idempotency_key = %s FOR UPDATE
                """,
                (request.idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay["request_payload"] != request_payload:
                    raise Conflict(
                        "endpoint idempotency_key was reused with different inputs"
                    )
                return replay
            self._verify_signed_m1(connection, request)
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s
                )
                """,
                (
                    task_id,
                    request.name,
                    request.workload.workload_id,
                    f"endpoint-task:{request.idempotency_key}",
                    TaskState.ENDPOINT_PROBING.value,
                    Jsonb(
                        {
                            "run_mode": request.plan.run_mode,
                            "acquisition_count": len(request.plan.acquisition_order),
                            "warmup_requests": request.plan.warmup_requests,
                            "measured_requests_per_acquisition": (
                                request.plan.measured_requests_per_acquisition
                            ),
                        }
                    ),
                    WorkflowType.ENDPOINT_VALIDATION.value,
                    request.workload.target_id,
                    request.signed_m1.target_snapshot_id,
                    request.adapter_profile,
                ),
            )
            payload = {
                "endpoint_run_id": str(endpoint_run_id),
                "task_id": str(task_id),
                "target_snapshot_id": str(request.signed_m1.target_snapshot_id),
                "adapter_profile": request.adapter_profile,
                "environment_fingerprint": request.environment_fingerprint,
                "signed_m1": request.signed_m1.model_dump(mode="json"),
                "workload": request.workload.model_dump(mode="json"),
                "plan": request.plan.model_dump(mode="json"),
                "plan_hash": plan_hash,
                "producer_verdict": None,
                "automatic_release_allowed": False,
            }
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    adapter_profile, lease_scope, payload, idempotency_key,
                    priority, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 1)
                """,
                (
                    job_id,
                    task_id,
                    JobType.ENDPOINT_VALIDATION.value,
                    WorkerType.GPU.value,
                    request.adapter_profile,
                    LeaseScope.EXCLUSIVE.value,
                    Jsonb(payload),
                    f"{endpoint_run_id}:provisional:v1",
                ),
            )
            row = connection.execute(
                """
                INSERT INTO endpoint_validation_runs (
                    endpoint_run_id, task_id, job_id, signed_m1_task_id,
                    target_snapshot_id, adapter_profile, environment_fingerprint,
                    workload, plan, plan_hash, state, request_payload,
                    idempotency_key, automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'queued', %s, %s, FALSE
                ) RETURNING *
                """,
                (
                    endpoint_run_id,
                    task_id,
                    job_id,
                    request.signed_m1.task_id,
                    request.signed_m1.target_snapshot_id,
                    request.adapter_profile,
                    request.environment_fingerprint,
                    Jsonb(request.workload.model_dump(mode="json")),
                    Jsonb(request.plan.model_dump(mode="json")),
                    plan_hash,
                    Jsonb(request_payload),
                    request.idempotency_key,
                ),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'endpoint_validation_created', %s)
                """,
                (
                    task_id,
                    Jsonb(
                        {
                            "endpoint_run_id": str(endpoint_run_id),
                            "job_id": str(job_id),
                            "signed_m1_task_id": str(request.signed_m1.task_id),
                            "plan_hash": plan_hash,
                            "lease_scope": LeaseScope.EXCLUSIVE.value,
                            "producer_verdict": None,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        assert row is not None
        return row

    def get_endpoint_validation_run(self, endpoint_run_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM endpoint_validation_runs WHERE endpoint_run_id = %s",
                (endpoint_run_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"endpoint validation run not found: {endpoint_run_id}")
        return row

    def endpoint_validation_summary(self, endpoint_run_id: UUID) -> dict[str, Any]:
        run = self.get_endpoint_validation_run(endpoint_run_id)
        return {
            "run": run,
            "task": self.get_task(run["task_id"]),
            "job": self.get_job(run["job_id"]),
            "events": self.list_task_events(run["task_id"]),
        }

    def record_endpoint_validation_result(
        self, job: dict[str, Any], result: EndpointValidationJobResult
    ) -> dict[str, Any]:
        if (
            job["job_type"] != JobType.ENDPOINT_VALIDATION.value
            or job["state"] != JobState.SUCCEEDED.value
            or job["lease_scope"] != LeaseScope.EXCLUSIVE.value
            or any(job.get(name) is None for name in ("lease_id", "resource_id", "fencing_token"))
        ):
            raise Conflict("endpoint evidence requires one succeeded exclusive-Lease Job")
        if not cleanup_is_healthy(result.cleanup_evidence):
            raise Conflict("endpoint evidence requires healthy fenced cleanup")
        payload = job["payload"]
        if (
            str(result.endpoint_run_id) != payload.get("endpoint_run_id")
            or result.plan_hash != payload.get("plan_hash")
            or any(
                item.profile != payload.get("adapter_profile")
                for item in result.adapter_provenance
            )
        ):
            raise Conflict("endpoint result differs from the frozen Job binding")
        with self.connection() as connection:
            run = connection.execute(
                """
                SELECT * FROM endpoint_validation_runs
                WHERE endpoint_run_id = %s FOR UPDATE
                """,
                (result.endpoint_run_id,),
            ).fetchone()
            if run is None:
                raise NotFound(f"endpoint validation run not found: {result.endpoint_run_id}")
            if run["job_id"] != job["job_id"] or run["task_id"] != job["task_id"]:
                raise Conflict("endpoint result belongs to another Run")
            encoded = result.model_dump(mode="json")
            if run["state"] == "provisional_passed":
                if run["result"] != encoded:
                    raise Conflict("endpoint result replay differs")
                return run
            if run["state"] not in {"queued", "running"}:
                raise Conflict("terminal endpoint Run cannot accept evidence")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (run["task_id"],)
            ).fetchone()
            assert task is not None
            transition_task(
                TaskState(task["state"]), TaskState.ENDPOINT_PROVISIONAL_PASSED
            )
            row = connection.execute(
                """
                UPDATE endpoint_validation_runs
                SET state = 'provisional_passed', result = %s, updated_at = now()
                WHERE endpoint_run_id = %s RETURNING *
                """,
                (Jsonb(encoded), result.endpoint_run_id),
            ).fetchone()
            connection.execute(
                """
                UPDATE tasks SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.ENDPOINT_PROVISIONAL_PASSED.value, run["task_id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'endpoint_provisional_passed', %s)
                """,
                (
                    run["task_id"],
                    Jsonb(
                        {
                            "endpoint_run_id": str(result.endpoint_run_id),
                            "job_id": str(job["job_id"]),
                            "plan_hash": result.plan_hash,
                            "producer_verdict": None,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        assert row is not None
        return row

    def _fail_endpoint_validation_run_after_job_failure(
        self,
        connection: Any,
        job: dict[str, Any],
        error: dict[str, Any],
        cleanup_evidence: dict[str, Any] | None = None,
    ) -> None:
        """Converge a terminal endpoint Job failure without inventing a verdict."""
        if job["job_type"] != JobType.ENDPOINT_VALIDATION.value:
            return
        run = connection.execute(
            """
            SELECT * FROM endpoint_validation_runs
            WHERE job_id = %s FOR UPDATE
            """,
            (job["job_id"],),
        ).fetchone()
        if run is None:
            return
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE",
            (job["task_id"],),
        ).fetchone()
        if run["state"] in {"queued", "running"}:
            connection.execute(
                """
                UPDATE endpoint_validation_runs
                SET state = 'failed', failure_error = %s, cleanup_evidence = %s,
                    updated_at = now()
                WHERE endpoint_run_id = %s
                """,
                (
                    Jsonb(error),
                    Jsonb(cleanup_evidence) if cleanup_evidence is not None else None,
                    run["endpoint_run_id"],
                ),
            )
        if task is not None and task["state"] == TaskState.ENDPOINT_PROBING.value:
            transition_task(TaskState(task["state"]), TaskState.REJECTED)
            connection.execute(
                """
                UPDATE tasks SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.REJECTED.value, job["task_id"]),
            )
        connection.execute(
            """
            INSERT INTO task_events (task_id, event_type, details)
            VALUES (%s, 'endpoint_validation_failed', %s)
            """,
            (
                job["task_id"],
                Jsonb(
                    {
                        "endpoint_run_id": str(run["endpoint_run_id"]),
                        "job_id": str(job["job_id"]),
                        "error": error,
                        "cleanup_evidence_recorded": cleanup_evidence is not None,
                        "automatic_release_allowed": False,
                    }
                ),
            ),
        )


__all__ = ["EndpointValidationRepositoryMixin"]
