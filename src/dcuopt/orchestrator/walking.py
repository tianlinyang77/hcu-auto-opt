from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from dcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    EvaluationRun,
    MeasurementSeries,
)
from dcuopt.contracts.v1 import MEASUREMENT_PROTOCOL_VERSION, JobCreate
from dcuopt.domain.enums import (
    CandidateState,
    JobType,
    LeaseScope,
    TaskState,
    WorkerType,
)
from dcuopt.storage.repository import PostgresRepository


class WalkingSkeletonCoordinator:
    """Advances only the Fake Walking Skeleton workflow.

    Real optimization policy will replace this deterministic coordinator after
    Stage 0. Job idempotency and workflow_advanced_at make completion replayable.
    """

    name = "fake-walking-v1-control-flow-only"

    def __init__(self, repository: PostgresRepository) -> None:
        self.repository = repository

    def _evaluation_run(
        self,
        job: dict[str, Any],
        phase: str,
        result: dict[str, Any],
    ) -> EvaluationRun:
        candidate_id = UUID(job["payload"]["candidate_id"])
        candidate = self.repository.get_candidate(candidate_id)
        baseline = self.repository.get_baseline(job["task_id"])
        if baseline is None:
            raise ValueError("evaluation requires a frozen baseline")
        fingerprint_payload = {
            "hardware": baseline["hardware_fingerprint"],
            "software": baseline["software_fingerprint"],
            "workload": baseline["workload_id"],
            "configuration": baseline["configuration_hash"],
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode()
        ).hexdigest()
        provenance = [
            AdapterProvenance.model_validate(item)
            for item in result["adapter_provenance"]
        ]
        measurement = (
            MeasurementSeries.model_validate(result["measurement"])
            if result.get("measurement") is not None
            else None
        )
        metrics = {
            key: value
            for key, value in result.items()
            if key not in {"adapter_provenance", "measurement", "passed", "synthetic"}
        }
        return EvaluationRun(
            task_id=job["task_id"],
            candidate_id=candidate_id,
            round_id=candidate["round_id"],
            baseline_epoch_id=candidate["baseline_epoch_id"],
            phase=phase,
            protocol_version=MEASUREMENT_PROTOCOL_VERSION,
            target_fingerprint=f"sha256:{fingerprint}",
            idempotency_key=f"job:{job['job_id']}:evaluation:{phase}",
            passed=(
                None
                if bool(result["synthetic"]) and phase in {"performance", "e2e"}
                else bool(result["passed"])
            ),
            metrics=metrics,
            measurement=measurement,
            evidence_uris=[f"fake://evaluations/{candidate_id}/{phase}"],
            adapter_provenance=provenance,
            synthetic=bool(result["synthetic"]),
        )

    def start_after_baseline(self, task_id: UUID, baseline: dict[str, Any]) -> dict[str, Any]:
        return self.repository.enqueue_job(
            JobCreate(
                task_id=task_id,
                job_type=JobType.PROFILE,
                accepted_worker_type=WorkerType.GPU,
                lease_scope=LeaseScope.EXCLUSIVE,
                idempotency_key=f"{task_id}:profile:v1",
                payload={
                    "task_id": str(task_id),
                    "baseline_epoch_id": str(baseline["baseline_epoch_id"]),
                    "workload_id": baseline["workload_id"],
                    "synthetic": True,
                },
            )
        )

    def advance(self, job: dict[str, Any]) -> None:
        if job.get("workflow_advanced_at") is not None:
            return
        job_type = JobType(job["job_type"])
        handlers = {
            JobType.PROFILE: self._after_profile,
            JobType.CANDIDATE_GENERATE: self._after_candidate_generate,
            JobType.BUILD: self._after_build,
            JobType.CORRECTNESS: self._after_correctness,
            JobType.PERFORMANCE: self._after_performance,
            JobType.E2E: self._after_e2e,
        }
        handlers[job_type](job)
        self.repository.mark_job_advanced(job["job_id"])

    def reconcile(self) -> list[UUID]:
        advanced: list[UUID] = []
        for job in self.repository.unadvanced_succeeded_jobs():
            self.advance(job)
            advanced.append(job["job_id"])
        return advanced

    def _after_profile(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        payload = job["payload"]
        result = job["result"]
        self.repository.record_hotspot(
            task_id, UUID(payload["baseline_epoch_id"]), result
        )
        self._ensure_task_state(task_id, TaskState.SEARCHING)
        self.repository.enqueue_job(
            JobCreate(
                task_id=task_id,
                job_type=JobType.CANDIDATE_GENERATE,
                accepted_worker_type=WorkerType.AGENT,
                idempotency_key=f"{task_id}:candidate-generate:round-0:v1",
                payload={
                    "task_id": str(task_id),
                    "baseline_epoch_id": payload["baseline_epoch_id"],
                    "hotspot": result,
                    "synthetic": True,
                },
            )
        )

    def _after_candidate_generate(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        payload = job["payload"]
        result = job["result"]
        round_id = UUID(result["round_id"])
        candidates = self.repository.create_candidates(
            task_id,
            UUID(payload["baseline_epoch_id"]),
            round_id,
            result["candidates"],
        )
        self._ensure_task_state(task_id, TaskState.EVALUATING)
        for candidate in candidates:
            self._ensure_candidate_state(candidate["candidate_id"], CandidateState.BUILDING)
            self.repository.enqueue_job(
                JobCreate(
                    task_id=task_id,
                    job_type=JobType.BUILD,
                    accepted_worker_type=WorkerType.BUILD,
                    idempotency_key=f"{candidate['candidate_id']}:build:v1",
                    payload={
                        "candidate_id": str(candidate["candidate_id"]),
                        "source_hash": candidate["source_hash"],
                        "variant": candidate["variant"],
                        "synthetic": True,
                    },
                )
            )

    def _after_build(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        candidate_id = UUID(job["payload"]["candidate_id"])
        self.repository.record_artifact(task_id, candidate_id, job["result"])
        self._ensure_candidate_state(candidate_id, CandidateState.CORRECTNESS_RUNNING)
        candidate = self.repository.get_candidate(candidate_id)
        self.repository.enqueue_job(
            JobCreate(
                task_id=task_id,
                job_type=JobType.CORRECTNESS,
                accepted_worker_type=WorkerType.GPU,
                lease_scope=LeaseScope.SHARED,
                idempotency_key=f"{candidate_id}:correctness:v1",
                payload={
                    "candidate_id": str(candidate_id),
                    "round_id": str(candidate["round_id"]),
                    "variant": candidate["variant"],
                    "synthetic": True,
                },
            )
        )

    def _after_correctness(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        candidate_id = UUID(job["payload"]["candidate_id"])
        result = job["result"]
        self.repository.record_evaluation(
            self._evaluation_run(job, "correctness", result)
        )
        if result["passed"]:
            self._ensure_candidate_state(candidate_id, CandidateState.PERFORMANCE_RUNNING)
            self.repository.enqueue_job(
                JobCreate(
                    task_id=task_id,
                    job_type=JobType.PERFORMANCE,
                    accepted_worker_type=WorkerType.GPU,
                    lease_scope=LeaseScope.EXCLUSIVE,
                    idempotency_key=f"{candidate_id}:performance:v1",
                    payload={
                        "candidate_id": str(candidate_id),
                        "round_id": job["payload"]["round_id"],
                        "variant": job["payload"]["variant"],
                        "synthetic": True,
                    },
                )
            )
        else:
            self._ensure_candidate_state(candidate_id, CandidateState.REJECTED)
        self._maybe_finish_round(task_id)

    def _after_performance(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        candidate_id = UUID(job["payload"]["candidate_id"])
        result = job["result"]
        self.repository.record_evaluation(
            self._evaluation_run(job, "performance", result)
        )
        target = CandidateState.ROUND_WAITING if result["passed"] else CandidateState.REJECTED
        self._ensure_candidate_state(candidate_id, target)
        self._maybe_finish_round(task_id)

    def _maybe_finish_round(self, task_id: UUID) -> None:
        candidates = self.repository.list_candidates(task_id)
        if not candidates:
            return
        terminal = {CandidateState.ROUND_WAITING.value, CandidateState.REJECTED.value}
        if any(candidate["state"] not in terminal for candidate in candidates):
            return
        winners = [c for c in candidates if c["state"] == CandidateState.ROUND_WAITING.value]
        if not winners:
            return
        winner = sorted(winners, key=lambda item: item["ordinal"])[0]
        candidate_id = winner["candidate_id"]
        self._ensure_candidate_state(candidate_id, CandidateState.E2E_RUNNING)
        self._ensure_task_state(task_id, TaskState.E2E_VALIDATING)
        self.repository.enqueue_job(
            JobCreate(
                task_id=task_id,
                job_type=JobType.E2E,
                accepted_worker_type=WorkerType.GPU,
                lease_scope=LeaseScope.EXCLUSIVE,
                idempotency_key=f"{candidate_id}:e2e:v1",
                payload={
                    "candidate_id": str(candidate_id),
                    "variant": winner["variant"],
                    "synthetic": True,
                },
            )
        )

    def _after_e2e(self, job: dict[str, Any]) -> None:
        task_id = job["task_id"]
        candidate_id = UUID(job["payload"]["candidate_id"])
        result = job["result"]
        self.repository.record_evaluation(self._evaluation_run(job, "e2e", result))
        if result["passed"]:
            self._ensure_candidate_state(candidate_id, CandidateState.RELEASE_CANDIDATE)
            self._ensure_task_state(task_id, TaskState.AWAITING_SIGNOFF)
        else:
            self._ensure_candidate_state(candidate_id, CandidateState.REJECTED)
            self._ensure_task_state(task_id, TaskState.REJECTED)

    def _ensure_task_state(self, task_id: UUID, target: TaskState) -> None:
        paths = {
            TaskState.SEARCHING: [TaskState.SEARCHING],
            TaskState.EVALUATING: [TaskState.EVALUATING],
            TaskState.E2E_VALIDATING: [TaskState.MODEL_VALIDATING, TaskState.E2E_VALIDATING],
            TaskState.AWAITING_SIGNOFF: [TaskState.AWAITING_SIGNOFF],
            TaskState.REJECTED: [TaskState.REJECTED],
        }
        order = list(TaskState)
        current = TaskState(self.repository.get_task(task_id)["state"])
        if current is target or order.index(current) > order.index(target):
            return
        for state in paths[target]:
            current = TaskState(self.repository.get_task(task_id)["state"])
            if current is state:
                continue
            self.repository.transition_task(task_id, state)

    def _ensure_candidate_state(self, candidate_id: UUID, target: CandidateState) -> None:
        happy_path = [
            CandidateState.PROPOSED,
            CandidateState.BUILDING,
            CandidateState.BUILT,
            CandidateState.CORRECTNESS_RUNNING,
            CandidateState.PERFORMANCE_RUNNING,
            CandidateState.ROUND_WAITING,
            CandidateState.MODEL_VALIDATING,
            CandidateState.STAGED,
            CandidateState.E2E_RUNNING,
            CandidateState.RELEASE_CANDIDATE,
        ]
        current = CandidateState(self.repository.get_candidate(candidate_id)["state"])
        if current is target:
            return
        if target is CandidateState.REJECTED:
            self.repository.transition_candidate(candidate_id, target)
            return
        if current in {CandidateState.REJECTED, CandidateState.BUILD_FAILED}:
            return
        current_index = happy_path.index(current)
        target_index = happy_path.index(target)
        if current_index >= target_index:
            return
        for state in happy_path[current_index + 1 : target_index + 1]:
            self.repository.transition_candidate(candidate_id, state)
