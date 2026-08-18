from __future__ import annotations

from typing import Any
from uuid import UUID

from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.contracts.v1 import (
    FrameworkSmokeResult,
    JobCreate,
    NoopBuildResult,
    PairedFrameworkSmokeResult,
    SourcePreparationResult,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobType,
    TaskState,
    WorkerType,
    WorkflowType,
)
from hcuopt.storage.repository import PostgresRepository


class FrameworkSmokeCoordinator:
    """Durable F1 orchestration; concrete machine work remains in adapters."""

    name = "framework-smoke-v1"

    def __init__(self, repository: PostgresRepository) -> None:
        self.repository = repository

    def advance(self, job: dict[str, Any]) -> None:
        if job.get("workflow_advanced_at") is not None:
            return
        task = self.repository.get_task(job["task_id"])
        if TaskState(task["state"]) is TaskState.CANCELLED:
            self.repository.mark_job_advanced(job["job_id"])
            return
        handlers = {
            JobType.SOURCE_PREPARE: self._after_source_prepare,
            JobType.NOOP_BUILD: self._after_noop_build,
            JobType.FRAMEWORK_SMOKE: self._after_framework_smoke,
        }
        job_type = JobType(job["job_type"])
        try:
            handler = handlers[job_type]
        except KeyError as exc:
            raise ValueError(f"not a Framework Smoke job: {job_type.value}") from exc
        handler(job)
        self.repository.mark_job_advanced(job["job_id"])

    def reconcile(self) -> list[UUID]:
        advanced: list[UUID] = []
        for job in self.repository.unadvanced_succeeded_jobs(
            WorkflowType.FRAMEWORK_SMOKE
        ):
            self.advance(job)
            advanced.append(job["job_id"])
        return advanced

    def enqueue_retest(self, task_id: UUID, reason: str) -> dict[str, Any]:
        return self.repository.enqueue_framework_execution(
            task_id, retest=True, reason=reason
        )

    def _after_source_prepare(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        result = SourcePreparationResult.model_validate(job["result"])
        target = TargetSpec.model_validate(job["payload"]["target"])
        provenance = [item.model_dump(mode="json") for item in result.adapter_provenance]
        self.repository.record_source_snapshot(
            task_id,
            result.source,
            provenance,
            result.synthetic,
            f"job:{job['job_id']}:baseline-source",
        )
        baseline = self.repository.ensure_framework_baseline(
            task_id, target, result.source
        )
        candidate = self.repository.create_noop_candidate(
            task_id, baseline["baseline_epoch_id"], result.source
        )
        self._ensure_candidate_state(candidate["candidate_id"], CandidateState.BUILDING)
        self._ensure_task_state(task_id, TaskState.ARTIFACT_PREPARING)
        task = self.repository.get_task(task_id)
        self.repository.enqueue_job(
            JobCreate(
                task_id=task_id,
                job_type=JobType.NOOP_BUILD,
                accepted_worker_type=WorkerType.BUILD,
                adapter_profile=task["adapter_profile"],
                idempotency_key=f"{candidate['candidate_id']}:framework-noop-build:v1",
                payload={
                    "task_id": str(task_id),
                    "candidate_id": str(candidate["candidate_id"]),
                    "baseline_source": result.source.model_dump(mode="json"),
                },
            )
        )

    def _after_noop_build(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        candidate_id = UUID(job["payload"]["candidate_id"])
        result = NoopBuildResult.model_validate(job["result"])
        provenance = [item.model_dump(mode="json") for item in result.adapter_provenance]
        self.repository.record_source_snapshot(
            task_id,
            result.source,
            provenance,
            result.synthetic,
            f"job:{job['job_id']}:candidate-source",
            candidate_id,
        )
        self.repository.record_artifact_manifest(
            task_id,
            result.artifact,
            provenance,
            f"job:{job['job_id']}:artifact",
        )
        self._ensure_candidate_state(candidate_id, CandidateState.BUILT)
        self.repository.enqueue_framework_execution(task_id)

    def _after_framework_smoke(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        raw_result = job["result"]
        if raw_result.get("result_kind", "single") == "paired":
            result = PairedFrameworkSmokeResult.model_validate(raw_result)
        else:
            result = FrameworkSmokeResult.model_validate(raw_result)
        evaluation = self.repository.record_evaluation(result.evaluation)
        if isinstance(result, PairedFrameworkSmokeResult):
            for execution in result.executions:
                self.repository.record_execution_request(
                    task_id,
                    result.evaluation.candidate_id,
                    evaluation["evaluation_run_id"],
                    execution.execution_request,
                    f"job:{job['job_id']}:execution-request:{execution.variant}",
                    execution.variant,
                )
                self.repository.record_execution_attempt(execution.execution_attempt)
        else:
            self.repository.record_execution_request(
                task_id,
                result.evaluation.candidate_id,
                evaluation["evaluation_run_id"],
                result.execution_request,
                f"job:{job['job_id']}:execution-request",
            )
            self.repository.record_execution_attempt(result.execution_attempt)
        self.repository.record_evidence_bundle(
            result.evidence,
            evaluation["evaluation_run_id"],
            f"job:{job['job_id']}:evidence",
        )

        task = self.repository.get_task(task_id)
        current = TaskState(task["state"])
        if current in {
            TaskState.AWAITING_SIGNOFF,
            TaskState.REJECTED,
            TaskState.CANCELLED,
        }:
            return
        candidate_id = result.evaluation.candidate_id
        passed = bool(result.evaluation.passed)
        if passed:
            self._ensure_candidate_state(
                candidate_id, CandidateState.FRAMEWORK_SMOKE_PASSED
            )
            if current is TaskState.FRAMEWORK_EXECUTING:
                self._ensure_task_state(task_id, TaskState.OUTPUT_VALIDATING)
            self._ensure_task_state(task_id, TaskState.AWAITING_SIGNOFF)
        else:
            self._ensure_candidate_state(candidate_id, CandidateState.REJECTED)
            self._ensure_task_state(task_id, TaskState.REJECTED)
        self.repository.record_task_event(
            task_id,
            "framework_smoke_evaluated",
            {
                "job_id": str(job["job_id"]),
                "evaluation_run_id": str(result.evaluation.evaluation_run_id),
                "retest_ordinal": job["payload"]["retest_ordinal"],
                "passed": passed,
                "synthetic": result.synthetic,
                "performance_conclusion": "not_measured",
            },
        )

    def _ensure_task_state(self, task_id: UUID, target: TaskState) -> None:
        current = TaskState(self.repository.get_task(task_id)["state"])
        if current is target:
            return
        if current in {TaskState.REJECTED, TaskState.CANCELLED, TaskState.COMPLETED}:
            return
        happy_path = [
            TaskState.SOURCE_PREPARING,
            TaskState.ARTIFACT_PREPARING,
            TaskState.FRAMEWORK_EXECUTING,
            TaskState.OUTPUT_VALIDATING,
            TaskState.AWAITING_SIGNOFF,
        ]
        if current in happy_path and target in happy_path:
            if happy_path.index(current) > happy_path.index(target):
                return
        self.repository.transition_task(task_id, target)

    def _ensure_candidate_state(
        self, candidate_id: UUID, target: CandidateState
    ) -> None:
        current = CandidateState(self.repository.get_candidate(candidate_id)["state"])
        if current is target:
            return
        if current in {CandidateState.REJECTED, CandidateState.BUILD_FAILED}:
            return
        happy_path = [
            CandidateState.PROPOSED,
            CandidateState.BUILDING,
            CandidateState.BUILT,
            CandidateState.FRAMEWORK_SMOKE_RUNNING,
            CandidateState.FRAMEWORK_SMOKE_PASSED,
        ]
        if current in happy_path and target in happy_path:
            if happy_path.index(current) > happy_path.index(target):
                return
        self.repository.transition_candidate(candidate_id, target)
