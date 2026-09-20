from __future__ import annotations

from typing import Any
from uuid import UUID

from hcuopt.contracts.endpoint_adjudication_v1 import (
    EndpointFormalAdjudicationResult,
)
from hcuopt.contracts.endpoint_control_v1 import EndpointValidationJobResult
from hcuopt.domain.enums import JobType
from hcuopt.storage.repository import PostgresRepository


class EndpointValidationCoordinator:
    """Persist a completed provisional endpoint Job without creating a verdict."""

    name = "endpoint-validation-v1"

    def __init__(self, repository: PostgresRepository) -> None:
        self.repository = repository

    def advance(self, job: dict[str, Any]) -> None:
        if job.get("workflow_advanced_at") is not None:
            return
        job_type = job.get("job_type")
        if job_type == JobType.ENDPOINT_VALIDATION.value:
            result = EndpointValidationJobResult.model_validate(job.get("result"))
            self.repository.record_endpoint_validation_result(job, result)
        elif job_type == JobType.ENDPOINT_ADJUDICATE.value:
            adjudication = EndpointFormalAdjudicationResult.model_validate(
                job.get("result")
            )
            self.repository.record_endpoint_adjudication_result(job, adjudication)
        else:
            raise ValueError("not an Endpoint Validation workflow job")
        self.repository.mark_job_advanced(job["job_id"])

    def reconcile(self) -> list[UUID]:
        advanced = []
        for job in self.repository.unadvanced_succeeded_jobs():
            if job["job_type"] in {
                JobType.ENDPOINT_VALIDATION.value,
                JobType.ENDPOINT_ADJUDICATE.value,
            }:
                self.advance(job)
                advanced.append(job["job_id"])
        return advanced


__all__ = ["EndpointValidationCoordinator"]
