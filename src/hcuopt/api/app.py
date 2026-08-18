from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from hcuopt.contracts.v1 import (
    BaselineCreate,
    BaselineView,
    JobClaim,
    JobComplete,
    JobCreate,
    JobFail,
    JobHeartbeat,
    ReapResult,
    Stage0EvidenceRequest,
    Stage0ReportView,
    TaskCreate,
    TaskSummary,
    TaskView,
    WorkerRegister,
    WorkerView,
)
from hcuopt.domain.errors import (
    Conflict,
    ContractError,
    NotFound,
    StaleClaimToken,
    StaleFencingToken,
)
from hcuopt.domain.models import Stage0Evidence
from hcuopt.orchestrator.walking import WalkingSkeletonCoordinator
from hcuopt.stage0 import evaluate_stage0
from hcuopt.storage.repository import PostgresRepository
from hcuopt.workflows.interfaces import WorkflowCoordinator, WorkflowFactory

LOGGER = logging.getLogger(__name__)
DEFAULT_DATABASE_URL = "postgresql://hcuopt:hcuopt@localhost:5432/hcuopt"


def create_app(
    repository: PostgresRepository | None = None,
    workflow_factory: WorkflowFactory = WalkingSkeletonCoordinator,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        repo = repository or PostgresRepository(
            os.getenv("HCUOPT_DATABASE_URL", DEFAULT_DATABASE_URL)
        )
        application.state.repository = repo
        if os.getenv("HCUOPT_AUTO_MIGRATE", "true").lower() == "true":
            repo.migrate()
        yield

    application = FastAPI(
        title="HCU Auto Opt Control Plane",
        version="0.2.0-walking-skeleton",
        lifespan=lifespan,
    )

    @application.exception_handler(NotFound)
    async def not_found_handler(_request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"code": "not_found", "message": str(exc)})

    @application.exception_handler(Conflict)
    async def conflict_handler(_request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"code": "conflict", "message": str(exc)})

    @application.exception_handler(ContractError)
    async def contract_handler(_request: Request, exc: ContractError) -> JSONResponse:
        error_codes = {
            StaleClaimToken: "stale_claim_token",
            StaleFencingToken: "stale_fencing_token",
        }
        return JSONResponse(
            status_code=409,
            content={
                "code": error_codes.get(type(exc), "contract_error"),
                "message": str(exc),
                "retryable": False,
            },
        )

    def repo(request: Request) -> PostgresRepository:
        return request.app.state.repository

    def workflow(request: Request) -> WorkflowCoordinator:
        return workflow_factory(repo(request))

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "contract_version": "v1"}

    @application.post("/v1/tasks", response_model=TaskView, status_code=201)
    def create_task(payload: TaskCreate, request: Request) -> dict[str, Any]:
        return repo(request).create_task(payload)

    @application.get("/v1/tasks/{task_id}", response_model=TaskView)
    def get_task(task_id: UUID, request: Request) -> dict[str, Any]:
        return repo(request).get_task(task_id)

    @application.post("/v1/tasks/{task_id}/stage0", response_model=Stage0ReportView)
    def submit_stage0(
        task_id: UUID, payload: Stage0EvidenceRequest, request: Request
    ) -> dict[str, Any]:
        evidence = Stage0Evidence(
            measurement=payload.measurement,
            profiler=payload.profiler,
            hot_patch=payload.hot_patch,
            hardware_fingerprint=payload.hardware_fingerprint,
            software_fingerprint=payload.software_fingerprint,
            timer_resolution_ns=payload.timer_resolution_ns,
            noise_sigma_ns=payload.noise_sigma_ns,
            noise_cv=payload.noise_cv,
            mde_ratio=payload.mde_ratio,
            evidence_uri=payload.evidence_uri,
        )
        report = evaluate_stage0(evidence)
        return repo(request).save_stage0(task_id, payload, report.mode, report.reasons)

    @application.post("/v1/tasks/{task_id}/baseline", response_model=BaselineView)
    def freeze_baseline(
        task_id: UUID, payload: BaselineCreate, request: Request
    ) -> dict[str, Any]:
        repository = repo(request)
        baseline = repository.freeze_baseline(task_id, payload)
        workflow(request).start_after_baseline(task_id, baseline)
        return baseline

    @application.get("/v1/tasks/{task_id}/baseline", response_model=BaselineView | None)
    def get_baseline(task_id: UUID, request: Request) -> dict[str, Any] | None:
        repo(request).get_task(task_id)
        return repo(request).get_baseline(task_id)

    @application.get("/v1/tasks/{task_id}/summary", response_model=TaskSummary)
    def task_summary(task_id: UUID, request: Request) -> dict[str, Any]:
        return repo(request).task_summary(task_id)

    @application.get("/v1/tasks/{task_id}/candidates")
    def list_candidates(task_id: UUID, request: Request) -> list[dict[str, Any]]:
        repo(request).get_task(task_id)
        return repo(request).list_candidates(task_id)

    @application.get("/v1/tasks/{task_id}/artifacts")
    def list_artifacts(task_id: UUID, request: Request) -> list[dict[str, Any]]:
        repo(request).get_task(task_id)
        return repo(request).list_artifacts(task_id)

    @application.get("/v1/tasks/{task_id}/evaluations")
    def list_evaluations(task_id: UUID, request: Request) -> list[dict[str, Any]]:
        repo(request).get_task(task_id)
        return repo(request).list_evaluations(task_id)

    @application.post("/v1/workers", response_model=WorkerView)
    def register_worker(payload: WorkerRegister, request: Request) -> dict[str, Any]:
        return repo(request).register_worker(payload)

    @application.post("/v1/workers/{worker_id}/claim", response_model=None)
    def claim_job(worker_id: str, request: Request) -> Response | dict[str, Any]:
        repository = repo(request)
        workflow(request).reconcile()
        job = repository.claim_job(worker_id)
        if job is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JobClaim.model_validate(job).model_dump(mode="json")

    @application.post("/v1/workers/{worker_id}/jobs/{job_id}/heartbeat")
    def heartbeat_job(
        worker_id: str,
        job_id: UUID,
        payload: JobHeartbeat,
        request: Request,
    ) -> dict[str, str]:
        repo(request).heartbeat_job(
            worker_id, job_id, payload.claim_token, payload.fencing_token
        )
        return {"status": "ok"}

    @application.post("/v1/jobs", status_code=201)
    def enqueue_job(payload: JobCreate, request: Request) -> dict[str, Any]:
        return repo(request).enqueue_job(payload)

    @application.get("/v1/jobs/{job_id}")
    def get_job(job_id: UUID, request: Request) -> dict[str, Any]:
        return repo(request).get_job(job_id)

    @application.post("/v1/jobs/{job_id}/complete")
    def complete_job(
        job_id: UUID, payload: JobComplete, request: Request
    ) -> dict[str, Any]:
        repository = repo(request)
        job = repository.complete_job(
            job_id, payload.claim_token, payload.fencing_token, payload.result
        )
        try:
            workflow(request).advance(job)
            job["workflow_advanced"] = True
        except Exception:
            LOGGER.exception("job succeeded but workflow advance will need reconciliation")
            job["workflow_advanced"] = False
        return job

    @application.post("/v1/jobs/{job_id}/fail")
    def fail_job(job_id: UUID, payload: JobFail, request: Request) -> dict[str, Any]:
        return repo(request).fail_job(
            job_id,
            payload.claim_token,
            payload.fencing_token,
            {"code": payload.error_code, "message": payload.message},
            payload.retryable,
        )

    @application.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(
        job_id: UUID,
        request: Request,
        reason: str = "operator request",
    ) -> dict[str, Any]:
        return repo(request).cancel_job(job_id, reason)

    @application.post("/v1/maintenance/reap", response_model=ReapResult)
    def reap_stale_workers(request: Request, stale_after_seconds: int = 120) -> dict[str, Any]:
        recovered = repo(request).recover_stale_jobs(stale_after_seconds)
        return {"recovered_job_ids": recovered}

    @application.post("/v1/maintenance/reconcile")
    def reconcile_workflow(request: Request) -> dict[str, Any]:
        advanced = workflow(request).reconcile()
        return {"advanced_job_ids": advanced}

    @application.get("/v1/resources")
    def list_resources(request: Request) -> list[dict[str, Any]]:
        return repo(request).list_resources()

    @application.get("/v1/leases")
    def list_leases(request: Request) -> list[dict[str, Any]]:
        return repo(request).list_resources()

    return application


app = create_app()
