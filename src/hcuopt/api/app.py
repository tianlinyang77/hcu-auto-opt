from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from hcuopt.adapters.profiles import AdapterProfileCatalog
from hcuopt.contracts.platform_v1 import TargetSpec
from hcuopt.contracts.v1 import (
    AdapterProfileView,
    BaselineCreate,
    BaselineView,
    FrameworkSmokeAction,
    FrameworkSmokeCreate,
    FrameworkSmokeSignoffRequest,
    FrameworkSmokeSignoffView,
    FrameworkSmokeSummary,
    FrameworkSmokeTaskView,
    JobClaim,
    JobComplete,
    JobCreate,
    JobFail,
    JobHeartbeat,
    ManualCandidateCreate,
    ManualCandidateSignoffRequest,
    ManualCandidateSignoffView,
    ManualCandidateSummary,
    ManualCandidateTaskCreate,
    ManualCandidateTaskView,
    ManualCandidateView,
    ManualHotspotIntakeCreate,
    ManualHotspotIntakeView,
    ReapResult,
    ResourceCleanupReport,
    Stage0EvidenceRequest,
    Stage0ReportView,
    Stage0RunCreate,
    Stage0RunSummary,
    Stage0RunView,
    TaskCreate,
    TaskSummary,
    TaskView,
    WorkerRegister,
    WorkerView,
)
from hcuopt.domain.enums import Stage0RunMode
from hcuopt.domain.errors import (
    AdapterUnavailable,
    Conflict,
    ContractError,
    NotFound,
    StaleClaimToken,
    StaleFencingToken,
    TargetConfigError,
    TargetNotReady,
)
from hcuopt.domain.models import Stage0Evidence
from hcuopt.evaluation.stage0_finalizer import FileStage0Finalizer
from hcuopt.orchestrator.framework_smoke import FrameworkSmokeCoordinator
from hcuopt.orchestrator.router import WorkflowRouter
from hcuopt.stage0 import evaluate_stage0
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import TargetCatalog
from hcuopt.workflows.interfaces import WorkflowCoordinator, WorkflowFactory

LOGGER = logging.getLogger(__name__)
DEFAULT_DATABASE_URL = "postgresql://hcuopt:hcuopt@localhost:5432/hcuopt"


def create_app(
    repository: PostgresRepository | None = None,
    workflow_factory: WorkflowFactory = WorkflowRouter,
    target_catalog: TargetCatalog | None = None,
    adapter_profiles: AdapterProfileCatalog | None = None,
) -> FastAPI:
    default_target_root = Path(__file__).resolve().parents[3] / "config" / "targets"
    targets = target_catalog or TargetCatalog(
        Path(os.getenv("HCUOPT_TARGET_ROOT", str(default_target_root)))
    )
    profiles = adapter_profiles or AdapterProfileCatalog()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if repository is None:
            evidence_root = os.getenv("HCUOPT_STAGE0_EVIDENCE_ROOT")
            finalizer = (
                FileStage0Finalizer(Path(evidence_root))
                if evidence_root is not None
                else None
            )
            repo = PostgresRepository(
                os.getenv("HCUOPT_DATABASE_URL", DEFAULT_DATABASE_URL),
                stage0_finalizer=finalizer,
            )
        else:
            repo = repository
        application.state.repository = repo
        if os.getenv("HCUOPT_AUTO_MIGRATE", "true").lower() == "true":
            repo.migrate()
        yield

    application = FastAPI(
        title="HCU Auto Opt Control Plane",
        version="0.5.0-m1-control-plane",
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
            TargetConfigError: "target_config_error",
            TargetNotReady: "target_not_ready",
            AdapterUnavailable: "adapter_unavailable",
        }
        status_codes = {
            TargetConfigError: 422,
            TargetNotReady: 409,
            AdapterUnavailable: 409,
        }
        return JSONResponse(
            status_code=status_codes.get(type(exc), 409),
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

    def framework_workflow(request: Request) -> FrameworkSmokeCoordinator:
        selected = workflow(request)
        if isinstance(selected, WorkflowRouter):
            return selected.framework_smoke
        return FrameworkSmokeCoordinator(repo(request))

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "contract_version": "v1"}

    @application.get("/v1/adapter-profiles", response_model=list[AdapterProfileView])
    def list_adapter_profiles() -> list[AdapterProfileView]:
        return profiles.list()

    @application.get("/v1/targets", response_model=list[TargetSpec])
    def list_targets() -> list[TargetSpec]:
        return targets.list()

    @application.get("/v1/targets/{target_id}", response_model=TargetSpec)
    def get_target(target_id: str) -> TargetSpec:
        return targets.load(target_id)

    @application.post(
        "/v1/framework-smoke/tasks",
        response_model=FrameworkSmokeTaskView,
        status_code=201,
    )
    def create_framework_smoke_task(
        payload: FrameworkSmokeCreate, request: Request
    ) -> dict[str, Any]:
        target = targets.load(payload.target_id)
        profile = profiles.require(payload.adapter_profile)
        profile.validate_target(target)
        return repo(request).create_framework_smoke_task(
            payload, target, str(targets.source_path(payload.target_id))
        )

    @application.get(
        "/v1/framework-smoke/tasks/{task_id}",
        response_model=FrameworkSmokeTaskView,
    )
    def get_framework_smoke_task(
        task_id: UUID, request: Request
    ) -> dict[str, Any]:
        task = repo(request).get_task(task_id)
        if task.get("workflow_type") != "framework_smoke":
            raise Conflict("task is not a Framework Smoke workflow")
        FrameworkSmokeTaskView.model_validate(task)
        return task

    @application.get(
        "/v1/framework-smoke/tasks/{task_id}/summary",
        response_model=FrameworkSmokeSummary,
    )
    def framework_smoke_summary(
        task_id: UUID, request: Request
    ) -> dict[str, Any]:
        summary = repo(request).framework_smoke_summary(task_id)
        profile = profiles.require(summary["task"]["adapter_profile"])
        summary["adapter_mode"] = profile.implementation_kind
        return summary

    @application.post(
        "/v1/framework-smoke/tasks/{task_id}/cancel",
        response_model=FrameworkSmokeTaskView,
    )
    def cancel_framework_smoke_task(
        task_id: UUID, payload: FrameworkSmokeAction, request: Request
    ) -> dict[str, Any]:
        return repo(request).cancel_framework_task(task_id, payload.reason)

    @application.post(
        "/v1/framework-smoke/tasks/{task_id}/retest",
        status_code=202,
    )
    def retest_framework_smoke_task(
        task_id: UUID, payload: FrameworkSmokeAction, request: Request
    ) -> dict[str, Any]:
        return framework_workflow(request).enqueue_retest(task_id, payload.reason)

    @application.post(
        "/v1/framework-smoke/tasks/{task_id}/signoff",
        response_model=FrameworkSmokeSignoffView,
    )
    def signoff_framework_smoke_task(
        task_id: UUID,
        payload: FrameworkSmokeSignoffRequest,
        request: Request,
    ) -> dict[str, Any]:
        return repo(request).signoff_framework_task(task_id, payload)

    @application.post("/v1/stage0-runs", response_model=Stage0RunView, status_code=201)
    def create_stage0_run(
        payload: Stage0RunCreate, request: Request
    ) -> dict[str, Any]:
        target = targets.load(payload.target_id)
        profile = profiles.require(payload.adapter_profile)
        profile.require_stage0()
        if (
            payload.mode is Stage0RunMode.FORMAL
            and profile.implementation_kind != "real"
        ):
            raise Conflict("formal Stage 0 requires a real Adapter Profile")
        profile.validate_target(
            target,
            scope=(
                "stage0"
                if payload.mode is Stage0RunMode.FORMAL
                else "framework_smoke"
            ),
        )
        return repo(request).create_stage0_run(
            payload, target, str(targets.source_path(payload.target_id))
        )

    @application.get(
        "/v1/stage0-runs/{stage0_run_id}", response_model=Stage0RunSummary
    )
    def get_stage0_run(
        stage0_run_id: UUID, request: Request
    ) -> dict[str, Any]:
        return repo(request).stage0_run_summary(stage0_run_id)

    @application.post(
        "/v1/stage0-runs/{stage0_run_id}/finalize",
        response_model=Stage0ReportView,
    )
    def finalize_stage0_run(
        stage0_run_id: UUID, request: Request
    ) -> dict[str, Any]:
        return repo(request).finalize_stage0_run(stage0_run_id)

    @application.post("/v1/tasks", response_model=TaskView, status_code=201)
    def create_task(payload: TaskCreate, request: Request) -> dict[str, Any]:
        return repo(request).create_task(payload)

    @application.get("/v1/tasks/{task_id}", response_model=TaskView)
    def get_task(task_id: UUID, request: Request) -> dict[str, Any]:
        return repo(request).get_task(task_id)

    @application.post(
        "/v1/manual-candidate/tasks",
        response_model=ManualCandidateTaskView,
        status_code=201,
    )
    def create_manual_candidate_task(
        payload: ManualCandidateTaskCreate, request: Request
    ) -> dict[str, Any]:
        repository = repo(request)
        stage0 = repository.stage0_run_summary(payload.stage0_run_id)
        target = TargetSpec.model_validate(stage0["target"])
        profile = profiles.require(payload.adapter_profile)
        if profile.implementation_kind != "real":
            raise Conflict("M1 Manual Candidate requires a real Adapter Profile")
        profile.require_manual_candidate()
        profile.validate_target(target, scope="optimization")
        return repository.create_manual_candidate_task(payload)

    @application.get(
        "/v1/manual-candidate/tasks/{task_id}",
        response_model=ManualCandidateTaskView,
    )
    def get_manual_candidate_task(
        task_id: UUID, request: Request
    ) -> dict[str, Any]:
        task = repo(request).get_task(task_id)
        if task.get("workflow_type") != "manual_candidate":
            raise Conflict("task is not an M1 Manual Candidate workflow")
        return task

    @application.get(
        "/v1/manual-candidate/tasks/{task_id}/summary",
        response_model=ManualCandidateSummary,
    )
    def get_manual_candidate_summary(
        task_id: UUID, request: Request
    ) -> dict[str, Any]:
        return repo(request).manual_candidate_summary(task_id)

    @application.post(
        "/v1/manual-candidate/tasks/{task_id}/hotspots",
        response_model=ManualHotspotIntakeView,
        status_code=201,
    )
    def create_manual_hotspot_intake(
        task_id: UUID,
        payload: ManualHotspotIntakeCreate,
        request: Request,
    ) -> dict[str, Any]:
        return repo(request).create_manual_hotspot_intake(task_id, payload)

    @application.get(
        "/v1/manual-candidate/tasks/{task_id}/hotspots",
        response_model=list[ManualHotspotIntakeView],
    )
    def list_manual_hotspot_intakes(
        task_id: UUID,
        request: Request,
    ) -> list[dict[str, Any]]:
        return repo(request).list_manual_hotspot_intakes(task_id)

    @application.post(
        "/v1/manual-candidate/tasks/{task_id}/candidates",
        response_model=ManualCandidateView,
        status_code=201,
    )
    def create_manual_candidate(
        task_id: UUID,
        payload: ManualCandidateCreate,
        request: Request,
    ) -> dict[str, Any]:
        return repo(request).create_manual_candidate(task_id, payload)

    @application.post(
        "/v1/manual-candidate/tasks/{task_id}/signoff",
        response_model=ManualCandidateSignoffView,
    )
    def signoff_manual_candidate(
        task_id: UUID,
        payload: ManualCandidateSignoffRequest,
        request: Request,
    ) -> dict[str, Any]:
        return repo(request).signoff_manual_candidate_task(task_id, payload)

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
            payload.cleanup_evidence,
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

    @application.post("/v1/resources/{resource_id}/cleanup")
    def report_resource_cleanup(
        resource_id: str,
        payload: ResourceCleanupReport,
        request: Request,
    ) -> dict[str, Any]:
        return repo(request).report_resource_cleanup(
            resource_id,
            payload.fencing_token,
            payload.cleanup_evidence,
        )

    @application.get("/v1/leases")
    def list_leases(request: Request) -> list[dict[str, Any]]:
        return repo(request).list_resources()

    return application


app = create_app()
