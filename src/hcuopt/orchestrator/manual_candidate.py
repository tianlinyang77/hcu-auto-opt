from __future__ import annotations

from typing import Any
from uuid import UUID

from hcuopt.contracts.v1 import (
    JobCreate,
    ManualCandidateAdjudicationResult,
    ManualCandidateBuildResult,
    ManualCorrectnessResult,
    ManualPerformanceEvidenceResult,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobType,
    LeaseScope,
    ManualCandidateVerdict,
    TaskState,
    WorkerType,
    WorkflowType,
)
from hcuopt.domain.errors import Conflict
from hcuopt.storage.repository import PostgresRepository


class ManualCandidateCoordinator:
    """M1 single-Candidate orchestration; B/C/D own concrete execution logic."""

    name = "manual-candidate-v1"

    def __init__(self, repository: PostgresRepository) -> None:
        self.repository = repository

    def advance(self, job: dict[str, Any]) -> None:
        if job.get("workflow_advanced_at") is not None:
            return
        task = self.repository.get_task(job["task_id"])
        if task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value:
            raise Conflict("job does not belong to an M1 Manual Candidate workflow")
        if TaskState(task["state"]) is TaskState.CANCELLED:
            self.repository.mark_job_advanced(job["job_id"])
            return
        handlers = {
            JobType.MANUAL_BUILD: self._after_build,
            JobType.MANUAL_CORRECTNESS: self._after_correctness,
            JobType.MANUAL_PERFORMANCE: self._after_performance,
            JobType.MANUAL_ADJUDICATE: self._after_adjudication,
        }
        job_type = JobType(job["job_type"])
        try:
            handler = handlers[job_type]
        except KeyError as exc:
            raise ValueError(f"not an M1 Manual Candidate job: {job_type.value}") from exc
        handler(job)
        self.repository.mark_job_advanced(job["job_id"])

    def reconcile(self) -> list[UUID]:
        advanced: list[UUID] = []
        for job in self.repository.unadvanced_succeeded_jobs(
            WorkflowType.MANUAL_CANDIDATE
        ):
            self.advance(job)
            advanced.append(job["job_id"])
        return advanced

    def _after_build(self, job: dict[str, Any]) -> None:
        result = ManualCandidateBuildResult.model_validate(job["result"])
        candidate_id = UUID(str(job["payload"]["candidate_id"]))
        if result.candidate_id != candidate_id:
            raise Conflict("M1 build result is bound to another Candidate")
        candidate = self.repository.get_candidate(candidate_id)
        baseline = self.repository.get_baseline(job["task_id"])
        if baseline is None:
            raise Conflict("M1 build cannot find its Baseline Epoch")
        if result.source.parent_snapshot_id != baseline["source_snapshot_id"]:
            raise Conflict("Candidate SourceSnapshot is not a child of the M1 Baseline")
        if result.source.source_hash != candidate["source_hash"]:
            raise Conflict("Candidate SourceSnapshot Hash differs from Candidate intake")
        if result.artifact.kind != "python_overlay":
            raise Conflict("M1 currently accepts only a Python/Triton startup Overlay")

        provenance = [item.model_dump(mode="json") for item in result.adapter_provenance]
        self.repository.record_source_snapshot(
            job["task_id"],
            result.source,
            provenance,
            False,
            f"job:{job['job_id']}:m1-candidate-source",
            candidate_id,
        )
        self.repository.record_artifact_manifest(
            job["task_id"],
            result.artifact,
            provenance,
            f"job:{job['job_id']}:m1-artifact",
        )
        self._ensure_candidate_state(candidate_id, CandidateState.BUILT)
        self._ensure_candidate_state(candidate_id, CandidateState.CORRECTNESS_RUNNING)
        self._ensure_task_state(job["task_id"], TaskState.MANUAL_CORRECTNESS)
        task = self.repository.get_task(job["task_id"])
        payload = {
            **job["payload"],
            "candidate_source": result.source.model_dump(mode="json"),
            "artifact": result.artifact.model_dump(mode="json"),
            "build_job_id": str(job["job_id"]),
        }
        self.repository.enqueue_job(
            JobCreate(
                task_id=job["task_id"],
                job_type=JobType.MANUAL_CORRECTNESS,
                accepted_worker_type=WorkerType.GPU,
                adapter_profile=task["adapter_profile"],
                idempotency_key=f"{candidate_id}:m1-correctness:v1",
                payload=payload,
                lease_scope=LeaseScope.SHARED,
            )
        )

    def _after_correctness(self, job: dict[str, Any]) -> None:
        result = ManualCorrectnessResult.model_validate(job["result"])
        candidate_id = UUID(str(job["payload"]["candidate_id"]))
        if result.candidate_id != candidate_id:
            raise Conflict("M1 correctness result is bound to another Candidate")
        self.repository.record_task_event(
            job["task_id"],
            "manual_candidate_correctness_evaluated",
            {
                "candidate_id": str(candidate_id),
                "job_id": str(job["job_id"]),
                "verdict": result.verdict,
                "protocol_version": result.protocol_version,
                "raw_evidence_uri": result.raw_evidence_uri,
                "raw_evidence_hash": result.raw_evidence_hash,
            },
        )
        if result.verdict != "correct":
            self._ensure_candidate_state(candidate_id, CandidateState.REJECTED)
            self._ensure_task_state(job["task_id"], TaskState.REJECTED)
            return

        self._ensure_candidate_state(candidate_id, CandidateState.PERFORMANCE_RUNNING)
        self._ensure_task_state(job["task_id"], TaskState.MANUAL_PERFORMANCE)
        task = self.repository.get_task(job["task_id"])
        self.repository.enqueue_job(
            JobCreate(
                task_id=job["task_id"],
                job_type=JobType.MANUAL_PERFORMANCE,
                accepted_worker_type=WorkerType.GPU,
                adapter_profile=task["adapter_profile"],
                idempotency_key=f"{candidate_id}:m1-performance:v1",
                payload={
                    **job["payload"],
                    "correctness_job_id": str(job["job_id"]),
                    "correctness_evidence_uri": result.raw_evidence_uri,
                    "correctness_evidence_hash": result.raw_evidence_hash,
                },
                lease_scope=LeaseScope.EXCLUSIVE,
            )
        )

    def _after_performance(self, job: dict[str, Any]) -> None:
        result = ManualPerformanceEvidenceResult.model_validate(job["result"])
        candidate_id = UUID(str(job["payload"]["candidate_id"]))
        if result.candidate_id != candidate_id:
            raise Conflict("M1 performance evidence is bound to another Candidate")
        self._ensure_candidate_state(candidate_id, CandidateState.ADJUDICATING)
        self._ensure_task_state(job["task_id"], TaskState.MANUAL_ADJUDICATING)
        task = self.repository.get_task(job["task_id"])
        self.repository.enqueue_job(
            JobCreate(
                task_id=job["task_id"],
                job_type=JobType.MANUAL_ADJUDICATE,
                accepted_worker_type=WorkerType.EVALUATION,
                adapter_profile=task["adapter_profile"],
                idempotency_key=f"{candidate_id}:m1-adjudicate:v1",
                payload={
                    **job["payload"],
                    "performance_job_id": str(job["job_id"]),
                    "performance_evidence": result.model_dump(mode="json"),
                },
                lease_scope=LeaseScope.NONE,
            )
        )

    def _after_adjudication(self, job: dict[str, Any]) -> None:
        result = ManualCandidateAdjudicationResult.model_validate(job["result"])
        performance = ManualPerformanceEvidenceResult.model_validate(
            job["payload"]["performance_evidence"]
        )
        candidate_id = UUID(str(job["payload"]["candidate_id"]))
        if result.candidate_id != candidate_id:
            raise Conflict("M1 adjudication is bound to another Candidate")
        candidate = self.repository.get_candidate(candidate_id)
        task = self.repository.get_task(job["task_id"])
        target = self.repository.get_target_snapshot(job["task_id"])
        if (
            result.evaluation.task_id != job["task_id"]
            or result.evaluation.round_id != candidate["round_id"]
            or result.evaluation.baseline_epoch_id != candidate["baseline_epoch_id"]
            or result.evaluation.target_fingerprint != target["target_fingerprint"]
            or result.evidence.target_id != task["target_id"]
            or result.evaluation.measurement != performance.measurement
        ):
            raise Conflict(
                "M1 adjudication has mismatched Task/Baseline/Target/Measurement bindings"
            )
        required_raw_uris = {
            str(job["payload"]["correctness_evidence_uri"]),
            str(performance.measurement.raw_samples_uri),
        }
        artifact_id = UUID(str(job["payload"]["artifact"]["artifact_id"]))
        if (
            not required_raw_uris.issubset(result.evidence.raw_uris)
            or result.evidence.artifact_ids != [artifact_id]
        ):
            raise Conflict("M1 EvidenceBundle omits required Artifact or raw evidence")

        evaluation = self.repository.record_evaluation(result.evaluation)
        evidence = self.repository.record_evidence_bundle(
            result.evidence,
            evaluation["evaluation_run_id"],
            f"job:{job['job_id']}:m1-evidence",
        )
        self.repository.set_manual_candidate_verdict(
            candidate_id, result.verdict, evidence["evidence_id"]
        )
        self.repository.record_task_event(
            job["task_id"],
            "manual_candidate_adjudicated",
            {
                "candidate_id": str(candidate_id),
                "job_id": str(job["job_id"]),
                "verdict": result.verdict.value,
                "evaluation_run_id": str(evaluation["evaluation_run_id"]),
                "evidence_bundle_id": str(evidence["evidence_id"]),
                "automatic_release_allowed": False,
            },
        )
        if result.verdict is ManualCandidateVerdict.INVALID:
            self._ensure_candidate_state(candidate_id, CandidateState.REJECTED)
            self._ensure_task_state(job["task_id"], TaskState.REJECTED)
            return
        self._ensure_candidate_state(candidate_id, CandidateState.AWAITING_SIGNOFF)
        self._ensure_task_state(job["task_id"], TaskState.AWAITING_SIGNOFF)

    def _ensure_task_state(self, task_id: UUID, target: TaskState) -> None:
        current = TaskState(self.repository.get_task(task_id)["state"])
        if current is target:
            return
        if current in {
            TaskState.REJECTED,
            TaskState.CANCELLED,
            TaskState.COMPLETED,
        }:
            return
        happy_path = [
            TaskState.MANUAL_CANDIDATE_PENDING,
            TaskState.MANUAL_BUILDING,
            TaskState.MANUAL_CORRECTNESS,
            TaskState.MANUAL_PERFORMANCE,
            TaskState.MANUAL_ADJUDICATING,
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
        if current in {
            CandidateState.REJECTED,
            CandidateState.BUILD_FAILED,
            CandidateState.ACCEPTED,
        }:
            return
        happy_path = [
            CandidateState.PROPOSED,
            CandidateState.BUILDING,
            CandidateState.BUILT,
            CandidateState.CORRECTNESS_RUNNING,
            CandidateState.PERFORMANCE_RUNNING,
            CandidateState.ADJUDICATING,
            CandidateState.AWAITING_SIGNOFF,
        ]
        if current in happy_path and target in happy_path:
            if happy_path.index(current) > happy_path.index(target):
                return
        self.repository.transition_candidate(candidate_id, target)
