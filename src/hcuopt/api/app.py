from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from hcuopt.adapters.m2_candidate import ScriptedCandidateIntake
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.adapters.profiles import AdapterProfileCatalog
from hcuopt.contracts.agent_verification_v1 import AgentGenerationReadModel
from hcuopt.contracts.endpoint_adjudication_v1 import (
    EndpointCampaignCreate,
    EndpointCampaignView,
)
from hcuopt.contracts.endpoint_control_v1 import (
    EndpointValidationRunCreate,
    EndpointValidationRunView,
)
from hcuopt.contracts.formal_evidence_acceptance_v1 import FormalEvidenceAcceptanceReport
from hcuopt.contracts.m2 import (
    ArtifactFamilyFreezeRequest,
    RoundBudgetFinalizeRequest,
    RoundBudgetMutationResult,
    RoundBudgetReserveRequest,
    RoundCandidate,
    RoundCandidateBuildTerminal,
    RoundCandidateView,
    SearchRound,
    SearchRoundReconcileResult,
    SearchRoundSummary,
    SearchRoundView,
)
from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentView
from hcuopt.contracts.operator_v1 import (
    OperatorCandidateEvidenceWorkspace,
    OperatorEvaluationEvidenceWorkspace,
    OperatorHotspotView,
    OperatorProfileDescriptor,
    OperatorRoundReport,
    OperatorRoundStartRequest,
    OperatorRoundSummary,
    OperatorServiceIdentity,
    OperatorStartIntentView,
    OperatorStartView,
    OperatorWorkloadView,
    RoundPlanPreviewRequest,
    RoundPlanPreviewView,
    TargetOperatorProfileRefs,
)
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
from hcuopt.domain.enums import Stage0RunMode, Stage0RunState
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
from hcuopt.evaluation.agent_generation_inspection import (
    AgentGenerationInspection,
    AgentGenerationInspectionService,
)
from hcuopt.evaluation.agent_generation_read_model import (
    AgentGenerationEvidenceReadService,
    AgentGenerationReadModelError,
)
from hcuopt.evaluation.endpoint_workload import load_endpoint_workload_spec
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.evaluation.formal_evidence_reporting import (
    FormalEvidenceAcceptanceReportError,
    FormalEvidenceAcceptanceReportService,
)
from hcuopt.evaluation.m2_authority import HoldoutRevealResult
from hcuopt.evaluation.m2_models import (
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundEvidenceBundle,
)
from hcuopt.evaluation.m2_statistics import SearchBarrierDecision
from hcuopt.evaluation.stage0_finalizer import FileStage0Finalizer
from hcuopt.operator import (
    OperatorDiscoveryService,
    OperatorProfileCatalog,
    build_operator_service_identity,
    build_scripted_operator_profile_catalog,
)
from hcuopt.operator.errors import OperatorPlanHashMismatch
from hcuopt.operator.plans import OperatorPlanCompiler, operator_preview_request_digest
from hcuopt.operator.read_models import OperatorReadModelService
from hcuopt.operator.start import HmacScriptedPlanAuthority, OperatorStartCoordinator
from hcuopt.orchestrator.framework_smoke import FrameworkSmokeCoordinator
from hcuopt.orchestrator.router import WorkflowRouter
from hcuopt.stage0 import evaluate_stage0
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import TargetCatalog
from hcuopt.workflows.interfaces import WorkflowCoordinator, WorkflowFactory

LOGGER = logging.getLogger(__name__)
DEFAULT_DATABASE_URL = "postgresql://hcuopt:hcuopt@localhost:5432/hcuopt"


@lru_cache(maxsize=1)
def _operator_source_commit() -> str:
    configured = os.getenv("HCUOPT_SOURCE_COMMIT")
    if configured:
        return configured
    repository_root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    source_commit = completed.stdout.strip()
    if completed.returncode != 0 or len(source_commit) != 40:
        raise RuntimeError(
            "HCUOPT_SOURCE_COMMIT is required when Git metadata is unavailable"
        )
    return source_commit


def _operator_scripted_candidate_intake(
    catalog: OperatorProfileCatalog,
) -> ScriptedCandidateIntake | None:
    values = {
        "package_root": os.getenv("HCUOPT_OPERATOR_PACKAGE_ROOT"),
        "overlay_roots": os.getenv("HCUOPT_OPERATOR_OVERLAY_ROOTS_JSON"),
        "mount_targets": os.getenv("HCUOPT_OPERATOR_MOUNT_TARGETS_JSON"),
    }
    if all(value is None for value in values.values()):
        return None
    if any(value is None for value in values.values()):
        raise ValueError(
            "Operator Package Store requires package root, overlay roots, and mount targets"
        )
    try:
        overlay_roots = json.loads(values["overlay_roots"] or "")
        mount_targets = json.loads(values["mount_targets"] or "")
    except json.JSONDecodeError as error:
        raise ValueError("Operator Package Store allowlists must be valid JSON") from error
    if not isinstance(overlay_roots, list) or not all(
        isinstance(item, str) for item in overlay_roots
    ):
        raise ValueError("Operator overlay roots must be a JSON string array")
    if not isinstance(mount_targets, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in mount_targets.items()
    ):
        raise ValueError("Operator mount targets must be a JSON string map")
    target_profile = catalog.get("target", "m2-scripted-target", 1)
    target = TargetOperatorProfileRefs.model_validate(target_profile.authority_refs)
    source_packages = CandidateSourcePackageStore(
        Path(values["package_root"] or ""),
        profile=target.adapter_profile,
        allowed_overlay_roots=tuple(overlay_roots),
        approved_mount_targets=mount_targets,
    )
    return ScriptedCandidateIntake(
        source_packages,
        store_id=target.candidate_package_store_id,
        store_hash=target.candidate_package_store_hash,
    )


def _operator_scripted_plan_authority() -> HmacScriptedPlanAuthority | None:
    encoded = os.getenv("HCUOPT_OPERATOR_PLAN_SECRET_HEX")
    if encoded is None:
        return None
    try:
        secret = bytes.fromhex(encoded)
    except ValueError as error:
        raise ValueError("Operator Plan Authority secret must be hexadecimal") from error
    return HmacScriptedPlanAuthority(secret)


def create_app(
    repository: PostgresRepository | None = None,
    workflow_factory: WorkflowFactory = WorkflowRouter,
    target_catalog: TargetCatalog | None = None,
    adapter_profiles: AdapterProfileCatalog | None = None,
    operator_profiles: OperatorProfileCatalog | None = None,
    operator_service_identity: OperatorServiceIdentity | None = None,
    operator_plan_compiler: OperatorPlanCompiler | None = None,
    operator_start_coordinator: OperatorStartCoordinator | None = None,
    operator_read_models: OperatorReadModelService | None = None,
    operator_discovery: OperatorDiscoveryService | None = None,
    agent_evidence_read_models: AgentGenerationEvidenceReadService | None = None,
    formal_start_read_authorizer: Callable[[Request, UUID], bool] | None = None,
    formal_evidence_reports: FormalEvidenceAcceptanceReportService | None = None,
    formal_evidence_read_authorizer: Callable[[Request, UUID, str], bool] | None = None,
    framework_signoff_authorizer: Callable[[Request, UUID], str | None] | None = None,
    auto_migrate: bool | None = None,
    agent_inspection_read_authorizer: Callable[[Request, UUID], bool] | None = None,
) -> FastAPI:
    default_target_root = Path(__file__).resolve().parents[3] / "config" / "targets"
    targets = target_catalog or TargetCatalog(
        Path(os.getenv("HCUOPT_TARGET_ROOT", str(default_target_root)))
    )
    profiles = adapter_profiles or AdapterProfileCatalog()
    operator_catalog = operator_profiles or build_scripted_operator_profile_catalog()
    if operator_plan_compiler is not None:
        operator_identity = operator_service_identity or (
            operator_plan_compiler.service_identity
        )
        if operator_identity != operator_plan_compiler.service_identity:
            raise ValueError("Operator Plan Compiler service identity does not match the API")
        plan_compiler = operator_plan_compiler
    else:
        source_commit = _operator_source_commit()
        generation = os.getenv("HCUOPT_SERVER_INSTANCE_ID")
        server_instance_id = (
            UUID(generation)
            if generation
            else uuid5(
                NAMESPACE_URL,
                f"hcuopt:operator-service:{source_commit}:{operator_catalog.catalog_hash}",
            )
        )
        operator_identity = operator_service_identity or build_operator_service_identity(
            source_commit=source_commit,
            server_instance_id=server_instance_id,
            catalog=operator_catalog,
        )
        plan_compiler = OperatorPlanCompiler(
            operator_catalog,
            operator_identity,
            candidate_intake=_operator_scripted_candidate_intake(operator_catalog),
        )
    if operator_identity.profile_catalog_hash != operator_catalog.catalog_hash:
        raise ValueError("Operator service identity does not bind the active Profile Catalog")
    start_coordinator = operator_start_coordinator or OperatorStartCoordinator(
        plan_compiler,
        plan_authority=_operator_scripted_plan_authority(),
    )
    if start_coordinator.service_identity != operator_identity:
        raise ValueError("Operator Start Coordinator service identity does not match the API")
    read_models = operator_read_models or OperatorReadModelService()
    discovery = operator_discovery or OperatorDiscoveryService(
        operator_catalog,
        candidate_intake=plan_compiler.candidate_intake,
    )
    if discovery.profiles.catalog_hash != operator_catalog.catalog_hash:
        raise ValueError("Operator Discovery Profile Catalog does not match the API")
    if discovery.candidate_intake is not plan_compiler.candidate_intake:
        raise ValueError("Operator Discovery Candidate Intake does not match Plan Preview")

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
        should_migrate = (
            auto_migrate if auto_migrate is not None
            else os.getenv("HCUOPT_AUTO_MIGRATE", "true").lower() == "true"
        )
        if should_migrate:
            repo.migrate()
        yield

    application = FastAPI(
        title="HCU Auto Opt Control Plane",
        version="0.5.0-m1-control-plane",
        lifespan=lifespan,
    )

    @application.exception_handler(NotFound)
    async def not_found_handler(_request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "code": getattr(exc, "code", "not_found"),
                "message": str(exc),
                "retryable": getattr(exc, "retryable", False),
            },
        )

    @application.exception_handler(Conflict)
    async def conflict_handler(_request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "code": getattr(exc, "code", "conflict"),
                "message": str(exc),
                "retryable": getattr(exc, "retryable", False),
            },
        )

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

    @application.exception_handler(AgentGenerationReadModelError)
    async def agent_evidence_handler(
        _request: Request, exc: AgentGenerationReadModelError
    ) -> JSONResponse:
        unavailable = exc.code in {
            "agent_evidence_root_unavailable", "agent_inspection_unconfigured"
        }
        return JSONResponse(
            status_code=503 if unavailable else 422,
            headers={"Cache-Control": "no-store"},
            content={
                "code": exc.code,
                "message": str(exc),
                "retryable": unavailable,
            },
        )

    @application.exception_handler(FormalEvidenceAcceptanceReportError)
    async def formal_evidence_report_handler(
        _request: Request, exc: FormalEvidenceAcceptanceReportError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": exc.code,
                "message": str(exc),
                "retryable": False,
            },
        )

    def repo(request: Request) -> PostgresRepository:
        return request.app.state.repository

    def workflow(request: Request) -> WorkflowCoordinator:
        return workflow_factory(repo(request))

    def agent_evidence_service(request: Request) -> AgentGenerationEvidenceReadService:
        if agent_evidence_read_models is not None:
            return agent_evidence_read_models
        evidence_root = os.getenv("HCUOPT_AGENT_EVIDENCE_ROOT")
        if evidence_root is None:
            raise AgentGenerationReadModelError(
                "agent_evidence_root_unavailable",
                "HCUOPT_AGENT_EVIDENCE_ROOT is required for Agent evidence reads",
            )
        try:
            reader = HashedEvidenceReader(Path(evidence_root))
        except (OSError, ValueError) as exc:
            raise AgentGenerationReadModelError(
                "agent_evidence_root_unavailable",
                "configured Agent evidence root is unavailable",
            ) from exc
        return AgentGenerationEvidenceReadService(repo(request), reader)

    def formal_evidence_report_service() -> FormalEvidenceAcceptanceReportService:
        if formal_evidence_reports is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Formal Evidence Acceptance Report service is not configured",
            )
        return formal_evidence_reports

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

    @application.get(
        "/v1/operator/profiles",
        response_model=list[OperatorProfileDescriptor],
    )
    def list_operator_profiles(
        profile_kind: Literal["target", "workload", "measurement"] | None = None,
    ) -> list[OperatorProfileDescriptor]:
        return operator_catalog.list(profile_kind)

    @application.get(
        "/v1/operator/identity",
        response_model=OperatorServiceIdentity,
    )
    def get_operator_service_identity() -> OperatorServiceIdentity:
        return operator_identity

    @application.get(
        "/v1/operator/profiles/{profile_kind}/{profile_id}/versions/{profile_version}",
        response_model=OperatorProfileDescriptor,
    )
    def get_operator_profile(
        profile_kind: Literal["target", "workload", "measurement"],
        profile_id: str,
        profile_version: int,
    ) -> OperatorProfileDescriptor:
        return operator_catalog.get(profile_kind, profile_id, profile_version)

    @application.get(
        "/v1/operator/workloads",
        response_model=list[OperatorWorkloadView],
    )
    def list_operator_workloads() -> list[OperatorWorkloadView]:
        return discovery.workloads()

    @application.get(
        "/v1/operator/workloads/{profile_id}/versions/{profile_version}",
        response_model=OperatorWorkloadView,
    )
    def get_operator_workload(
        profile_id: str,
        profile_version: int,
    ) -> OperatorWorkloadView:
        return discovery.workload(profile_id, profile_version)

    @application.get(
        "/v1/operator/hotspots",
        response_model=list[OperatorHotspotView],
    )
    def list_operator_hotspots(
        request: Request,
        target_profile_id: str,
        target_profile_version: int,
        workload_profile_id: str,
        workload_profile_version: int,
    ) -> list[OperatorHotspotView]:
        return discovery.hotspots(
            target_profile_id=target_profile_id,
            target_profile_version=target_profile_version,
            workload_profile_id=workload_profile_id,
            workload_profile_version=workload_profile_version,
            repository=repo(request),
        )

    @application.get(
        "/v1/operator/hotspots/{hotspot_id}",
        response_model=OperatorHotspotView,
    )
    def get_operator_hotspot(
        hotspot_id: UUID,
        request: Request,
        target_profile_id: str,
        target_profile_version: int,
        workload_profile_id: str,
        workload_profile_version: int,
    ) -> OperatorHotspotView:
        return discovery.hotspot(
            hotspot_id,
            target_profile_id=target_profile_id,
            target_profile_version=target_profile_version,
            workload_profile_id=workload_profile_id,
            workload_profile_version=workload_profile_version,
            repository=repo(request),
        )

    @application.post(
        "/v1/operator/round-plans:preview",
        response_model=RoundPlanPreviewView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_operator_round_plan_preview(
        payload: RoundPlanPreviewRequest,
        request: Request,
        response: Response,
    ) -> RoundPlanPreviewView:
        repository = repo(request)
        request_digest = operator_preview_request_digest(payload)
        existing = repository.get_operator_plan_preview_by_idempotency(
            payload.idempotency_key
        )
        if existing is not None:
            if existing.preview_request_digest != request_digest:
                raise OperatorPlanHashMismatch(
                    "Operator Preview idempotency key was reused with different inputs"
                )
            response.status_code = status.HTTP_200_OK
            return existing
        preview = plan_compiler.compile(payload, repository)
        return repository.create_operator_plan_preview(payload.idempotency_key, preview)

    @application.post(
        "/v1/operator/round-plans/{preview_id}:start",
        response_model=OperatorStartView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_operator_round_plan(
        preview_id: UUID,
        payload: OperatorRoundStartRequest,
        request: Request,
        response: Response,
    ) -> OperatorStartView:
        if preview_id != payload.preview_id:
            raise OperatorPlanHashMismatch(
                "path preview_id does not match Operator Start request"
            )
        result = start_coordinator.start(payload, repo(request))
        response.headers["Location"] = (
            f"/v1/operator/start-intents/{result.intent_id}"
        )
        return result

    @application.get(
        "/v1/operator/start-intents/{intent_id}",
        response_model=OperatorStartIntentView,
    )
    def get_operator_start_intent(
        intent_id: UUID,
        request: Request,
    ) -> OperatorStartIntentView:
        return repo(request).get_operator_start_intent(intent_id)

    @application.post(
        "/v1/operator/start-intents/{intent_id}:reconcile",
        response_model=OperatorStartIntentView,
    )
    def reconcile_operator_start_intent(
        intent_id: UUID,
        request: Request,
    ) -> OperatorStartIntentView:
        return start_coordinator.reconcile(intent_id, repo(request))

    @application.get(
        "/v1/operator/formal-start-intents/{intent_id}",
        response_model=FormalStartIntentView,
    )
    def get_formal_start_intent(
        intent_id: UUID,
        request: Request,
    ) -> FormalStartIntentView:
        if formal_start_read_authorizer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Formal Start read authentication is not configured",
            )
        try:
            authorized = formal_start_read_authorizer(request, intent_id)
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Formal Start read authentication failed closed",
            ) from error
        if authorized is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Formal Start read access was rejected",
            )
        return repo(request).get_formal_start_intent(intent_id)

    @application.get(
        "/v1/operator/search-rounds",
        response_model=list[OperatorRoundSummary],
    )
    def list_operator_search_rounds(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[OperatorRoundSummary]:
        return read_models.summaries(repo(request), limit=limit)

    @application.get(
        "/v1/operator/search-rounds/{round_id}/summary",
        response_model=OperatorRoundSummary,
    )
    def get_operator_round_summary(
        round_id: UUID,
        request: Request,
    ) -> OperatorRoundSummary:
        return read_models.summary(round_id, repo(request))

    @application.get(
        "/v1/operator/search-rounds/{round_id}/candidate-evidence",
        response_model=OperatorCandidateEvidenceWorkspace,
    )
    def get_operator_candidate_evidence(
        round_id: UUID,
        request: Request,
    ) -> OperatorCandidateEvidenceWorkspace:
        return read_models.candidate_evidence(round_id, repo(request))

    @application.get(
        "/v1/operator/search-rounds/{round_id}/evaluation-evidence",
        response_model=OperatorEvaluationEvidenceWorkspace,
    )
    def get_operator_evaluation_evidence(
        round_id: UUID,
        request: Request,
    ) -> OperatorEvaluationEvidenceWorkspace:
        return read_models.evaluation_evidence(round_id, repo(request))

    @application.get(
        "/v1/operator/search-rounds/{round_id}/report",
        response_model=OperatorRoundReport,
    )
    def get_operator_round_report(
        round_id: UUID,
        request: Request,
    ) -> OperatorRoundReport:
        return read_models.report(round_id, repo(request))

    @application.get(
        "/v1/operator/agent-generations/{generation_run_id}/evidence",
        response_model=AgentGenerationReadModel,
    )
    def get_operator_agent_generation_evidence(
        generation_run_id: UUID,
        request: Request,
    ) -> AgentGenerationReadModel:
        return agent_evidence_service(request).get(generation_run_id)

    @application.get(
        "/v1/operator/agent-generations/{generation_run_id}/inspection",
        response_model=AgentGenerationInspection,
    )
    def get_operator_agent_generation_inspection(
        generation_run_id: UUID, request: Request, response: Response
    ) -> AgentGenerationInspection:
        # Authenticate each exact Run before looking at its Store or A state.
        # The model credential is never a browser/read credential.
        if agent_inspection_read_authorizer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent inspection read authentication is not configured",
                headers={"Cache-Control": "no-store"},
            )
        try:
            authorized = agent_inspection_read_authorizer(request, generation_run_id)
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent inspection read authentication failed closed",
                headers={"Cache-Control": "no-store"},
            ) from error
        if authorized is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent inspection read access was rejected",
                headers={"Cache-Control": "no-store"},
            )
        root = os.getenv("HCUOPT_AGENT_INSPECTION_ROOT")
        if not root:
            raise AgentGenerationReadModelError(
                "agent_inspection_unconfigured", "Agent inspection is not configured"
            )
        try:
            service = AgentGenerationInspectionService(repo(request), Path(root))
            response.headers["Cache-Control"] = "no-store"
            return service.get(generation_run_id)
        except (OSError, ValueError) as exc:
            raise AgentGenerationReadModelError(
                "agent_inspection_unavailable",
                "Agent inspection could not be independently verified",
            ) from exc

    @application.get(
        "/v1/operator/formal-rounds/{round_id}/evidence-acceptance",
        response_model=FormalEvidenceAcceptanceReport,
    )
    def get_formal_evidence_acceptance_report(
        round_id: UUID,
        request: Request,
        readiness_audit_id: str = Query(
            min_length=3,
            max_length=200,
            pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$",
        ),
    ) -> FormalEvidenceAcceptanceReport:
        if formal_evidence_read_authorizer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Formal Evidence Acceptance read authentication is not configured",
            )
        try:
            authorized = formal_evidence_read_authorizer(
                request, round_id, readiness_audit_id
            )
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Formal Evidence Acceptance read authentication failed closed",
            ) from error
        if authorized is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Formal Evidence Acceptance read access was rejected",
            )
        return formal_evidence_report_service().get(
            round_id=round_id,
            readiness_audit_id=readiness_audit_id,
        )

    @application.get("/v1/targets", response_model=list[TargetSpec])
    def list_targets() -> list[TargetSpec]:
        return targets.list()

    @application.get("/v1/targets/{target_id}", response_model=TargetSpec)
    def get_target(target_id: str) -> TargetSpec:
        return targets.load(target_id)

    @application.post(
        "/v1/search-rounds",
        response_model=SearchRoundView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_search_round(
        payload: SearchRound, request: Request
    ) -> dict[str, Any]:
        return repo(request).create_search_round(payload)

    @application.get(
        "/v1/search-rounds/{round_id}",
        response_model=SearchRoundView,
    )
    def get_search_round(round_id: UUID, request: Request) -> dict[str, Any]:
        return repo(request).get_search_round(round_id)

    @application.get(
        "/v1/search-rounds/{round_id}/summary",
        response_model=SearchRoundSummary,
    )
    def get_search_round_summary(
        round_id: UUID, request: Request
    ) -> dict[str, Any]:
        return repo(request).search_round_summary(round_id)

    @application.post(
        "/v1/search-rounds/{round_id}/candidates",
        response_model=RoundCandidateView,
        status_code=status.HTTP_201_CREATED,
    )
    def add_search_round_candidate(
        round_id: UUID, payload: RoundCandidate, request: Request
    ) -> dict[str, Any]:
        if round_id != payload.round_id:
            raise Conflict("path round_id does not match Candidate round_id")
        return repo(request).add_round_candidate(payload)

    @application.post(
        "/v1/search-rounds/{round_id}/intake-close",
        response_model=SearchRoundView,
    )
    def close_search_round_intake(
        round_id: UUID, request: Request
    ) -> dict[str, Any]:
        return repo(request).close_search_round_intake(round_id)

    @application.post(
        "/v1/search-rounds/{round_id}/candidates/{round_candidate_id}/build-terminal",
        response_model=RoundCandidateView,
    )
    def record_search_round_candidate_build(
        round_id: UUID,
        round_candidate_id: UUID,
        payload: RoundCandidateBuildTerminal,
        request: Request,
    ) -> dict[str, Any]:
        if round_id != payload.round_id:
            raise Conflict("path round_id does not match Build terminal round_id")
        if round_candidate_id != payload.round_candidate_id:
            raise Conflict(
                "path round_candidate_id does not match Build terminal member identity"
            )
        return repo(request).record_round_candidate_build(payload)

    @application.post(
        "/v1/search-rounds/{round_id}/artifact-family:freeze",
        response_model=SearchRoundView,
    )
    def freeze_search_round_artifact_family(
        round_id: UUID,
        payload: ArtifactFamilyFreezeRequest,
        request: Request,
    ) -> dict[str, Any]:
        if round_id != payload.round_id:
            raise Conflict("path round_id does not match Artifact Family round_id")
        return repo(request).freeze_search_round_artifact_family(payload)

    @application.post(
        "/v1/search-rounds/{round_id}/budget-reservations",
        response_model=RoundBudgetMutationResult,
        status_code=status.HTTP_201_CREATED,
    )
    def reserve_search_round_budget(
        round_id: UUID, payload: RoundBudgetReserveRequest, request: Request
    ) -> dict[str, Any]:
        if round_id != payload.reservation.round_id:
            raise Conflict("path round_id does not match Budget reservation round_id")
        return repo(request).reserve_round_budget(
            payload.reservation, payload.ledger_entry
        )

    @application.post(
        "/v1/search-rounds/{round_id}/budget-reservations/{reservation_id}/finalize",
        response_model=RoundBudgetMutationResult,
    )
    def finalize_search_round_budget(
        round_id: UUID,
        reservation_id: UUID,
        payload: RoundBudgetFinalizeRequest,
        request: Request,
    ) -> dict[str, Any]:
        entry = payload.ledger_entry
        if round_id != entry.round_id:
            raise Conflict("path round_id does not match Budget ledger round_id")
        if reservation_id != entry.reservation_id:
            raise Conflict("path reservation_id does not match Budget ledger reservation_id")
        return repo(request).finalize_round_budget(entry)

    @application.post(
        "/v1/search-rounds/{round_id}/barriers/search:close",
        response_model=SearchRoundView,
    )
    def close_scripted_search_barrier(
        round_id: UUID,
        payload: SearchBarrierDecision,
        request: Request,
    ) -> dict[str, Any]:
        if round_id != payload.barrier.round_id:
            raise Conflict("path round_id does not match Search Barrier round_id")
        return repo(request).close_scripted_search_barrier(payload)

    @application.post(
        "/v1/search-rounds/{round_id}/holdout-plan:reveal",
        response_model=SearchRoundView,
    )
    def record_scripted_holdout_reveal(
        round_id: UUID,
        payload: HoldoutRevealResult,
        request: Request,
    ) -> dict[str, Any]:
        if round_id != payload.round_id:
            raise Conflict("path round_id does not match Holdout Reveal round_id")
        return repo(request).record_scripted_holdout_reveal(payload)

    @application.post(
        "/v1/search-rounds/{round_id}/barriers/holdout:close",
        response_model=SearchRoundView,
    )
    def close_scripted_holdout_barrier(
        round_id: UUID,
        payload: RoundBarrierResult,
        request: Request,
    ) -> dict[str, Any]:
        if round_id != payload.round_id:
            raise Conflict("path round_id does not match Holdout Barrier round_id")
        return repo(request).close_scripted_holdout_barrier(payload)

    @application.post(
        "/v1/search-rounds/{round_id}/multiple-comparison",
        response_model=MultipleComparisonResult,
    )
    def record_scripted_multiple_comparison(
        round_id: UUID,
        payload: MultipleComparisonResult,
        request: Request,
    ) -> MultipleComparisonResult:
        if round_id != payload.round_id:
            raise Conflict("path round_id does not match Multiple Comparison round_id")
        return repo(request).record_scripted_multiple_comparison(payload)

    @application.post(
        "/v1/search-rounds/{round_id}/scripted:finalize",
        response_model=SearchRoundView,
    )
    def finalize_scripted_search_round(
        round_id: UUID,
        payload: RoundEvidenceBundle,
        request: Request,
    ) -> dict[str, Any]:
        if round_id != payload.round_id:
            raise Conflict("path round_id does not match Round Evidence round_id")
        return repo(request).finalize_scripted_search_round(payload)

    @application.post(
        "/v1/search-rounds/{round_id}/cancel",
        response_model=SearchRoundView,
    )
    def cancel_scripted_search_round(
        round_id: UUID,
        payload: FrameworkSmokeAction,
        request: Request,
    ) -> dict[str, Any]:
        return repo(request).cancel_scripted_search_round(round_id, payload.reason)

    @application.post(
        "/v1/search-rounds/{round_id}/reconcile",
        response_model=SearchRoundReconcileResult,
    )
    def reconcile_scripted_search_round(
        round_id: UUID,
        request: Request,
    ) -> dict[str, Any]:
        return repo(request).reconcile_scripted_search_round(round_id)

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
        from hcuopt.api.framework_signoff import submit_signoff

        return submit_signoff(framework_signoff_authorizer, repo(request).signoff_framework_task,
                              request, task_id, payload)

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

    @application.post(
        "/v1/endpoint-validation-runs",
        response_model=EndpointValidationRunView,
        status_code=201,
    )
    def create_endpoint_validation_run(
        payload: EndpointValidationRunCreate, request: Request
    ) -> dict[str, Any]:
        frozen = load_endpoint_workload_spec(
            Path(__file__).resolve().parents[3]
            / "config/workloads/bw20-sglang-endpoint-provisional-v1.yaml"
        )
        if payload.workload != frozen:
            raise Conflict("endpoint workload differs from the repository-frozen document")
        target = targets.load(payload.workload.target_id)
        profile = profiles.require(payload.adapter_profile)
        if profile.implementation_kind != "real":
            raise Conflict("Endpoint Validation requires a real Adapter Profile")
        profile.validate_target(target, scope="endpoint_validation")
        return repo(request).create_endpoint_validation_run(payload)

    @application.get(
        "/v1/endpoint-validation-runs/{endpoint_run_id}",
        response_model=EndpointValidationRunView,
    )
    def get_endpoint_validation_run(
        endpoint_run_id: UUID, request: Request
    ) -> dict[str, Any]:
        return repo(request).get_endpoint_validation_run(endpoint_run_id)

    @application.get("/v1/endpoint-validation-runs/{endpoint_run_id}/summary")
    def get_endpoint_validation_summary(
        endpoint_run_id: UUID, request: Request
    ) -> dict[str, Any]:
        return repo(request).endpoint_validation_summary(endpoint_run_id)

    @application.post(
        "/v1/endpoint-validation-campaigns",
        response_model=EndpointCampaignView,
        status_code=201,
    )
    def create_endpoint_validation_campaign(
        payload: EndpointCampaignCreate, request: Request
    ) -> dict[str, Any]:
        return repo(request).create_endpoint_validation_campaign(payload)

    @application.get(
        "/v1/endpoint-validation-campaigns/{campaign_id}",
        response_model=EndpointCampaignView,
    )
    def get_endpoint_validation_campaign(
        campaign_id: UUID, request: Request
    ) -> dict[str, Any]:
        return repo(request).get_endpoint_validation_campaign(campaign_id)

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
        run = stage0.get("run")
        task = stage0.get("task")
        stage0_resolves_measurement_blocker = (
            isinstance(run, dict)
            and isinstance(task, dict)
            and run.get("mode") == Stage0RunMode.FORMAL.value
            and run.get("state") == Stage0RunState.FINALIZED.value
            and task.get("stage0_authority") == "formal"
        )
        profile.validate_target(
            target,
            scope="optimization",
            evidence_resolved_blockers=(
                frozenset({"stage0_not_measured"})
                if stage0_resolves_measurement_blocker
                else frozenset()
            ),
        )
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

    @application.post("/v1/workers/{worker_id}/jobs/{job_id}/lease-check", status_code=204)
    def check_live_job_lease(
        worker_id: str, job_id: UUID, payload: JobHeartbeat, request: Request,
    ) -> Response:
        repo(request).assert_live_job_lease(
            worker_id, job_id, payload.claim_token, payload.fencing_token
        )
        return Response(status_code=204)

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
