from __future__ import annotations

from typing import Any
from uuid import UUID

from hcuopt.contracts.v1 import Stage0ProbeResult
from hcuopt.domain.enums import JobType, WorkflowType
from hcuopt.storage.repository import PostgresRepository


class Stage0Coordinator:
    """Persist typed probe jobs and open the seven-probe barrier."""

    name = "target-bound-stage0-v1"

    def __init__(self, repository: PostgresRepository) -> None:
        self.repository = repository

    def advance(self, job: dict[str, Any]) -> None:
        if job.get("workflow_advanced_at") is not None:
            return
        if JobType(job["job_type"]) is not JobType.STAGE0_PROBE:
            raise ValueError(f"not a Stage 0 job: {job['job_type']}")
        result = Stage0ProbeResult.model_validate(job["result"])
        self.repository.record_stage0_probe(job, result)
        self.repository.mark_job_advanced(job["job_id"])

    def reconcile(self) -> list[UUID]:
        advanced: list[UUID] = []
        for job in self.repository.unadvanced_succeeded_jobs(WorkflowType.STAGE0):
            self.advance(job)
            advanced.append(job["job_id"])
        return advanced
