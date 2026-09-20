from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from hcuopt.domain.enums import JobType
from hcuopt.orchestrator.endpoint_validation import EndpointValidationCoordinator
from hcuopt.orchestrator.framework_smoke import FrameworkSmokeCoordinator
from hcuopt.orchestrator.manual_candidate import ManualCandidateCoordinator
from hcuopt.orchestrator.stage0 import Stage0Coordinator
from hcuopt.orchestrator.walking import WalkingSkeletonCoordinator
from hcuopt.storage.repository import PostgresRepository

FRAMEWORK_JOB_TYPES = frozenset(
    {JobType.SOURCE_PREPARE, JobType.NOOP_BUILD, JobType.FRAMEWORK_SMOKE}
)
STAGE0_JOB_TYPES = frozenset({JobType.STAGE0_PROBE})
MANUAL_CANDIDATE_JOB_TYPES = frozenset(
    {
        JobType.MANUAL_BUILD,
        JobType.MANUAL_CORRECTNESS,
        JobType.MANUAL_PERFORMANCE,
        JobType.MANUAL_ADJUDICATE,
    }
)
ENDPOINT_VALIDATION_JOB_TYPES = frozenset(
    {JobType.ENDPOINT_VALIDATION, JobType.ENDPOINT_ADJUDICATE}
)


class WorkflowRouter:
    name = "workflow-router-v1"

    def __init__(self, repository: PostgresRepository) -> None:
        self.repository = repository
        self.walking = WalkingSkeletonCoordinator(repository)
        self.framework_smoke = FrameworkSmokeCoordinator(repository)
        self.stage0 = Stage0Coordinator(repository)
        self.manual_candidate = ManualCandidateCoordinator(repository)
        self.endpoint_validation = EndpointValidationCoordinator(repository)

    def start_after_baseline(
        self, task_id: UUID, baseline: Mapping[str, Any]
    ) -> Any:
        return self.walking.start_after_baseline(task_id, dict(baseline))

    def advance(self, job: Mapping[str, Any]) -> None:
        materialized = dict(job)
        job_type = JobType(materialized["job_type"])
        if job_type in FRAMEWORK_JOB_TYPES:
            self.framework_smoke.advance(materialized)
        elif job_type in STAGE0_JOB_TYPES:
            self.stage0.advance(materialized)
        elif job_type in MANUAL_CANDIDATE_JOB_TYPES:
            self.manual_candidate.advance(materialized)
        elif job_type in ENDPOINT_VALIDATION_JOB_TYPES:
            self.endpoint_validation.advance(materialized)
        else:
            self.walking.advance(materialized)

    def reconcile(self) -> list[UUID]:
        advanced: list[UUID] = []
        # A Stage 0 task deliberately continues into the optimization state
        # machine after a formal pass. Route recovery by durable JobType rather
        # than by the task's original workflow_type.
        for job in self.repository.unadvanced_succeeded_jobs():
            self.advance(job)
            advanced.append(job["job_id"])
        return advanced
