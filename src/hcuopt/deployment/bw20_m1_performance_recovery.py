# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Authorize one audited retry for the exact BW20 namespace infrastructure fault."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from hcuopt.domain.enums import CandidateState, JobState, JobType, TaskState
from hcuopt.storage.repository import PostgresRepository

TASK_ID = UUID("73f6f07d-14ed-5614-a8e5-75e77c4356f0")
CANDIDATE_ID = UUID("bbcdc4f0-0369-54d0-b4cd-57763203c272")
PERFORMANCE_JOB_ID = UUID("cae78bc9-122c-4d4e-89a9-18f6eaccbbb8")
RESOURCE_ID = "bw20-sglang-0.5.12:hcu:7"
FIX_COMMIT = "912b19032681f648f8a0efe7886b2de96208b8c2"


def _validate_snapshot(
    *,
    task: Mapping[str, Any],
    candidate: Mapping[str, Any],
    job: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    resource: Mapping[str, Any],
    signoff_exists: bool,
    evaluation_count: int,
) -> None:
    expected_jobs = {
        JobType.MANUAL_BUILD.value: JobState.SUCCEEDED.value,
        JobType.MANUAL_CORRECTNESS.value: JobState.SUCCEEDED.value,
        JobType.MANUAL_PERFORMANCE.value: JobState.FAILED.value,
    }
    error = job.get("last_error") or {}
    cleanup = resource.get("cleanup_evidence") or {}
    if (
        task.get("task_id") != TASK_ID
        or task.get("workflow_type") != "manual_candidate"
        or task.get("state") != TaskState.REJECTED.value
        or task.get("automatic_release_allowed") is not False
        or candidate.get("candidate_id") != CANDIDATE_ID
        or candidate.get("task_id") != TASK_ID
        or candidate.get("state") != CandidateState.REJECTED.value
        or candidate.get("verdict") is not None
        or candidate.get("evidence_bundle_id") is not None
        or job.get("job_id") != PERFORMANCE_JOB_ID
        or job.get("task_id") != TASK_ID
        or job.get("job_type") != JobType.MANUAL_PERFORMANCE.value
        or job.get("state") != JobState.FAILED.value
        or job.get("attempts") != 3
        or job.get("max_attempts") != 3
        or job.get("result") is not None
        or {row["job_type"]: row["state"] for row in jobs} != expected_jobs
        or error != {"code": "RuntimeError", "message": "host namespace unavailable"}
        or signoff_exists
        or evaluation_count != 0
        or resource.get("resource_id") != RESOURCE_ID
        or resource.get("state") != "available"
        or resource.get("owner_job_id") is not None
        or resource.get("lease_id") is not None
        or resource.get("fencing_token") != 46
        or cleanup.get("healthy") is not True
        or cleanup.get("fence", {}).get("fenced") is not True
        or cleanup.get("health", {}).get("healthy") is not True
    ):
        raise RuntimeError(
            "BW20 M1 retry is allowed only for the exact cleaned namespace infrastructure fault"
        )


def authorize_namespace_retry(repository: PostgresRepository) -> dict[str, Any]:
    """Preserve three failed attempts and grant one bounded post-fix retry."""

    with repository.connection() as connection:
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id=%s FOR UPDATE", (TASK_ID,)
        ).fetchone()
        candidate = connection.execute(
            "SELECT * FROM candidates WHERE candidate_id=%s FOR UPDATE", (CANDIDATE_ID,)
        ).fetchone()
        job = connection.execute(
            "SELECT * FROM jobs WHERE job_id=%s FOR UPDATE", (PERFORMANCE_JOB_ID,)
        ).fetchone()
        resource = connection.execute(
            "SELECT * FROM resources WHERE resource_id=%s FOR UPDATE", (RESOURCE_ID,)
        ).fetchone()
        if any(value is None for value in (task, candidate, job, resource)):
            raise RuntimeError("BW20 M1 retry cannot find the frozen authority rows")
        if (
            job["state"] == JobState.QUEUED.value
            and job["attempts"] == 3
            and job["max_attempts"] == 4
            and task["state"] == TaskState.MANUAL_PERFORMANCE.value
            and candidate["state"] == CandidateState.PERFORMANCE_RUNNING.value
        ):
            return {
                "status": "already_authorized",
                "job_id": str(PERFORMANCE_JOB_ID),
                "attempts": 3,
                "max_attempts": 4,
            }
        jobs = connection.execute(
            "SELECT job_type, state FROM jobs WHERE task_id=%s ORDER BY created_at",
            (TASK_ID,),
        ).fetchall()
        signoff_exists = connection.execute(
            "SELECT 1 FROM manual_candidate_signoffs WHERE task_id=%s", (TASK_ID,)
        ).fetchone() is not None
        evaluation_count = connection.execute(
            "SELECT count(*) AS value FROM evaluation_runs WHERE task_id=%s", (TASK_ID,)
        ).fetchone()["value"]
        _validate_snapshot(
            task=task,
            candidate=candidate,
            job=job,
            jobs=jobs,
            resource=resource,
            signoff_exists=signoff_exists,
            evaluation_count=evaluation_count,
        )
        details = {
            "actor": "codex-assisted-formal-operator",
            "reason": (
                "one bounded retry after the non-owner M1 container reached ready but the "
                "host could not read its procfs PID namespace link"
            ),
            "job_id": str(PERFORMANCE_JOB_ID),
            "candidate_id": str(CANDIDATE_ID),
            "previous_attempts": 3,
            "previous_max_attempts": 3,
            "previous_error": job["last_error"],
            "resource_fencing_token": resource["fencing_token"],
            "fix_commit": FIX_COMMIT,
            "automatic_release_allowed": False,
        }
        connection.execute(
            """
            UPDATE jobs
            SET state='queued', max_attempts=4, available_at=now() + interval '1 second',
                claimed_by=NULL, claim_token=NULL, claimed_at=NULL, heartbeat_at=NULL,
                lease_id=NULL, resource_id=NULL, fencing_token=NULL,
                finished_at=NULL, updated_at=now()
            WHERE job_id=%s
            """,
            (PERFORMANCE_JOB_ID,),
        )
        connection.execute(
            "UPDATE tasks SET state=%s, version=version + 1, updated_at=now() WHERE task_id=%s",
            (TaskState.MANUAL_PERFORMANCE.value, TASK_ID),
        )
        connection.execute(
            "UPDATE candidates SET state=%s, updated_at=now() WHERE candidate_id=%s",
            (CandidateState.PERFORMANCE_RUNNING.value, CANDIDATE_ID),
        )
        for table, identifier, event_type in (
            ("job_events", PERFORMANCE_JOB_ID, "manual_infrastructure_retry_authorized"),
            ("task_events", TASK_ID, "manual_infrastructure_retry_authorized"),
        ):
            key = "job_id" if table == "job_events" else "task_id"
            connection.execute(
                f"INSERT INTO {table} ({key}, event_type, details) VALUES (%s, %s, %s)",
                (identifier, event_type, Jsonb(details)),
            )
    return {
        "status": "authorized",
        "job_id": str(PERFORMANCE_JOB_ID),
        "attempts": 3,
        "max_attempts": 4,
        "fix_commit": FIX_COMMIT,
        "automatic_release_allowed": False,
    }


def main() -> int:
    database_url = os.getenv("HCUOPT_DATABASE_URL")
    if not database_url:
        raise SystemExit("HCUOPT_DATABASE_URL is required")
    print(json.dumps(authorize_namespace_retry(PostgresRepository(database_url)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["authorize_namespace_retry", "main"]
