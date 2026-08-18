from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from hcuopt.domain.enums import JobType
from hcuopt.orchestrator.framework_smoke import FrameworkSmokeCoordinator
from hcuopt.orchestrator.walking import WalkingSkeletonCoordinator
from hcuopt.storage.repository import PostgresRepository

FRAMEWORK_JOB_TYPES = frozenset(
    {JobType.SOURCE_PREPARE, JobType.NOOP_BUILD, JobType.FRAMEWORK_SMOKE}
)


class WorkflowRouter:
    name = "workflow-router-v1"

    def __init__(self, repository: PostgresRepository) -> None:
        self.walking = WalkingSkeletonCoordinator(repository)
        self.framework_smoke = FrameworkSmokeCoordinator(repository)

    def start_after_baseline(
        self, task_id: UUID, baseline: Mapping[str, Any]
    ) -> Any:
        return self.walking.start_after_baseline(task_id, dict(baseline))

    def advance(self, job: Mapping[str, Any]) -> None:
        materialized = dict(job)
        job_type = JobType(materialized["job_type"])
        if job_type in FRAMEWORK_JOB_TYPES:
            self.framework_smoke.advance(materialized)
        else:
            self.walking.advance(materialized)

    def reconcile(self) -> list[UUID]:
        return [*self.walking.reconcile(), *self.framework_smoke.reconcile()]
