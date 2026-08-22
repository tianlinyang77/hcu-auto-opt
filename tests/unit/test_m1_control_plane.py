from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hcuopt.adapters.profiles import (
    MANUAL_CANDIDATE_CAPABILITIES,
    AdapterProfile,
    AdapterProfileCatalog,
)
from hcuopt.api.app import create_app
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    MeasurementSeries,
    SourceSnapshot,
)
from hcuopt.contracts.v1 import (
    ManualCandidateAdjudicationResult,
    ManualCandidateBuildResult,
    ManualCandidateCreate,
    ManualCorrectnessResult,
    ManualPerformanceEvidenceResult,
)
from hcuopt.domain.enums import (
    CandidateState,
    JobType,
    TaskState,
)
from hcuopt.domain.transitions import transition_candidate, transition_task
from hcuopt.orchestrator.router import WorkflowRouter
from hcuopt.targets import load_target

ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"
PROFILE = "m1-real-test"


def _provenance(kind: str = "real") -> AdapterProvenance:
    return AdapterProvenance(
        profile=PROFILE,
        capability="candidate_builder",
        adapter_name="FixtureBuilder",
        adapter_version="1",
        implementation_kind=kind,
    )


def test_m1_has_an_independent_single_candidate_state_chain() -> None:
    assert (
        transition_task(
            TaskState.BASELINE_PENDING,
            TaskState.MANUAL_CANDIDATE_PENDING,
        )
        is TaskState.MANUAL_CANDIDATE_PENDING
    )
    assert (
        transition_task(
            TaskState.MANUAL_PERFORMANCE,
            TaskState.MANUAL_ADJUDICATING,
        )
        is TaskState.MANUAL_ADJUDICATING
    )
    assert (
        transition_candidate(
            CandidateState.PERFORMANCE_RUNNING,
            CandidateState.ADJUDICATING,
        )
        is CandidateState.ADJUDICATING
    )
    assert (
        transition_candidate(
            CandidateState.AWAITING_SIGNOFF,
            CandidateState.ACCEPTED,
        )
        is CandidateState.ACCEPTED
    )


def test_manual_candidate_contract_rejects_non_overlay_tracks() -> None:
    base = {
        "baseline_epoch_id": str(uuid4()),
        "source_hash": "sha256:" + "a" * 64,
        "optimization_intent": "replace one hot LayerNorm implementation",
        "replacement_point": "sglang.srt.layers.layernorm",
        "candidate_kind": "business",
        "idempotency_key": "manual-candidate-fixture",
    }
    assert ManualCandidateCreate.model_validate(base).release_mode.value == "overlay"
    with pytest.raises(ValidationError, match="startup-overlay Triton track"):
        ManualCandidateCreate.model_validate({**base, "track": "hip"})
    with pytest.raises(ValidationError, match="startup Overlay"):
        ManualCandidateCreate.model_validate({**base, "release_mode": "manual_only"})


def test_manual_build_contract_rejects_fake_or_unbound_artifacts() -> None:
    candidate_id = uuid4()
    baseline_id = uuid4()
    source = SourceSnapshot(
        kind="candidate",
        repository="git@github.com:HYGON-AI/sglang-das.git",
        commit="a" * 40,
        tree_hash="b" * 40,
        source_hash="sha256:" + "c" * 64,
        worktree_uri="file:///candidate",
        clean=True,
        parent_snapshot_id=baseline_id,
    )
    artifact = ArtifactManifest(
        candidate_id=candidate_id,
        kind="python_overlay",
        uri="file:///artifact.py",
        content_hash="sha256:" + "d" * 64,
        source_snapshot_id=source.snapshot_id,
    )
    result = ManualCandidateBuildResult(
        candidate_id=candidate_id,
        source=source,
        artifact=artifact,
        adapter_provenance=[_provenance()],
    )
    assert result.synthetic is False
    with pytest.raises(ValidationError, match="fake Adapter provenance"):
        ManualCandidateBuildResult(
            candidate_id=candidate_id,
            source=source,
            artifact=artifact,
            adapter_provenance=[_provenance("fake")],
        )
    with pytest.raises(ValidationError, match="exact Candidate SourceSnapshot"):
        ManualCandidateBuildResult(
            candidate_id=candidate_id,
            source=source,
            artifact=artifact.model_copy(update={"source_snapshot_id": uuid4()}),
            adapter_provenance=[_provenance()],
        )


def test_correctness_contract_requires_real_provenance_and_healthy_cleanup() -> None:
    candidate_id = uuid4()
    payload = {
        "candidate_id": candidate_id,
        "verdict": "correct",
        "protocol_version": "m1-correctness-v1",
        "raw_evidence_uri": "file:///evidence/correctness.json",
        "raw_evidence_hash": "sha256:" + "e" * 64,
        "adapter_provenance": [_provenance().model_dump(mode="json")],
        "cleanup_evidence": {
            "fence": {"fenced": True},
            "health": {"healthy": True},
        },
    }
    result = ManualCorrectnessResult.model_validate(payload)
    assert result.verdict == "correct"
    with pytest.raises(ValidationError, match="fake Adapter provenance"):
        ManualCorrectnessResult.model_validate(
            {
                **payload,
                "adapter_provenance": [_provenance("fake").model_dump(mode="json")],
            }
        )
    with pytest.raises(ValidationError, match="healthy cleanup"):
        ManualCorrectnessResult.model_validate(
            {
                **payload,
                "cleanup_evidence": {
                    "fence": {"fenced": True},
                    "health": {"healthy": False},
                },
            }
        )


def test_performance_contract_is_raw_measurement_not_a_verdict() -> None:
    candidate_id = uuid4()
    measurement = MeasurementSeries(
        status="measured",
        metric_name="latency",
        unit="us",
        protocol_version="m1-performance-v1",
        sample_count=20,
        warmup_count=5,
        process_restart_count=4,
        raw_samples_uri="file:///evidence/samples.json",
        raw_samples_hash="sha256:" + "f" * 64,
        environment_fingerprint="sha256:" + "1" * 64,
        summary={"median_us": 100.0},
        adapter_provenance=_provenance(),
    )
    payload = {
        "candidate_id": candidate_id,
        "measurement": measurement.model_dump(mode="json"),
        "cleanup_evidence": {
            "fence": {"fenced": True},
            "health": {"healthy": True},
        },
    }
    result = ManualPerformanceEvidenceResult.model_validate(payload)
    assert result.measurement.status == "measured"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ManualPerformanceEvidenceResult.model_validate({**payload, "verdict": "faster"})


def test_adjudication_binds_task_candidate_baseline_and_real_evidence() -> None:
    task_id = uuid4()
    candidate_id = uuid4()
    round_id = uuid4()
    baseline_epoch_id = uuid4()
    measurement = MeasurementSeries(
        status="measured",
        metric_name="latency",
        unit="us",
        protocol_version="m1-adjudication-v1",
        sample_count=20,
        raw_samples_uri="file:///evidence/adjudication-samples.json",
        raw_samples_hash="sha256:" + "3" * 64,
        environment_fingerprint="sha256:" + "4" * 64,
        adapter_provenance=_provenance(),
    )
    evaluation = EvaluationRun(
        task_id=task_id,
        candidate_id=candidate_id,
        round_id=round_id,
        baseline_epoch_id=baseline_epoch_id,
        phase="performance",
        protocol_version="m1-adjudication-v1",
        target_fingerprint="sha256:" + "2" * 64,
        idempotency_key="m1-adjudication-evaluation",
        passed=True,
        metrics={"verdict": "faster"},
        measurement=measurement,
        adapter_provenance=[_provenance()],
    )
    evidence = EvidenceBundle(
        task_id=task_id,
        candidate_id=candidate_id,
        baseline_epoch_id=baseline_epoch_id,
        target_id="nmz36-sglang-0.5.12",
        evidence_type="m1_manual_candidate",
        protocol_version="m1-adjudication-v1",
        measurement_ids=[measurement.measurement_id],
        summary={"verdict": "faster", "automatic_release_allowed": False},
        adapter_provenance=[_provenance()],
    )
    result = ManualCandidateAdjudicationResult(
        candidate_id=candidate_id,
        verdict="faster",
        evaluation=evaluation,
        evidence=evidence,
    )
    assert result.verdict.value == "faster"
    with pytest.raises(ValidationError, match="baseline bindings differ"):
        ManualCandidateAdjudicationResult(
            candidate_id=candidate_id,
            verdict="faster",
            evaluation=evaluation,
            evidence=evidence.model_copy(update={"baseline_epoch_id": uuid4()}),
        )
    with pytest.raises(ValidationError, match="exact measured series"):
        ManualCandidateAdjudicationResult(
            candidate_id=candidate_id,
            verdict="faster",
            evaluation=evaluation.model_copy(update={"measurement": None}),
            evidence=evidence,
        )


def test_m1_profile_is_explicit_and_fails_closed() -> None:
    incomplete = AdapterProfile(
        name="m1-incomplete",
        implementation_kind="real",
        capabilities=MANUAL_CANDIDATE_CAPABILITIES - {"candidate_adjudicator"},
    )
    with pytest.raises(Exception, match="candidate_adjudicator"):
        incomplete.require_manual_candidate()


def test_openapi_exposes_m1_without_allowing_executable_public_budget() -> None:
    target = load_target(TARGET_PATH)
    stage0_run_id = uuid4()
    target_snapshot_id = uuid4()
    task_id = uuid4()

    class StubRepository:
        def migrate(self) -> None:
            return None

        def stage0_run_summary(self, _stage0_run_id):
            return {"target": target.model_dump(mode="json")}

        def create_manual_candidate_task(self, payload):
            now = datetime.now(timezone.utc)
            return {
                "task_id": task_id,
                "name": payload.name,
                "workload_id": "nmz36-sglang-smoke-v1",
                "state": "manual_candidate_pending",
                "project_mode": "degraded_manual_intake",
                "budget": payload.budget.model_dump(mode="json", exclude_none=True),
                "automatic_release_allowed": False,
                "stage0_authority": "formal",
                "version": 1,
                "created_at": now,
                "updated_at": now,
                "workflow_type": "manual_candidate",
                "target_id": target.target_id,
                "target_snapshot_id": target_snapshot_id,
                "adapter_profile": payload.adapter_profile,
                "stage0_run_id": stage0_run_id,
            }

    profile = AdapterProfile(
        name=PROFILE,
        implementation_kind="real",
        capabilities=MANUAL_CANDIDATE_CAPABILITIES,
    )
    app = create_app(
        repository=StubRepository(),  # type: ignore[arg-type]
        adapter_profiles=AdapterProfileCatalog((profile,)),
    )
    payload = {
        "name": "M1 fixture",
        "stage0_run_id": str(stage0_run_id),
        "adapter_profile": PROFILE,
        "baseline_source_snapshot_id": str(uuid4()),
        "workload_hash": "sha256:" + "1" * 64,
        "configuration_hash": "sha256:" + "2" * 64,
        "idempotency_key": "m1-openapi-fixture",
    }
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        response = client.post("/v1/manual-candidate/tasks", json=payload)
        rejected = client.post(
            "/v1/manual-candidate/tasks",
            json={**payload, "budget": {"argv": ["sh", "-c", "untrusted"]}},
        )
    assert response.status_code == 201
    assert response.json()["workflow_type"] == "manual_candidate"
    assert response.json()["automatic_release_allowed"] is False
    assert rejected.status_code == 422
    assert "/v1/manual-candidate/tasks" in schema["paths"]
    assert "/v1/manual-candidate/tasks/{task_id}/candidates" in schema["paths"]
    assert "/v1/manual-candidate/tasks/{task_id}/summary" in schema["paths"]
    assert "/v1/manual-candidate/tasks/{task_id}/signoff" in schema["paths"]


def test_router_dispatches_m1_jobs_without_falling_into_fake_walking_flow() -> None:
    job_id = uuid4()

    class StubRepository:
        pass

    class RecordingCoordinator:
        def __init__(self) -> None:
            self.jobs: list[dict] = []

        def advance(self, job: dict) -> None:
            self.jobs.append(job)

    router = WorkflowRouter(StubRepository())  # type: ignore[arg-type]
    manual = RecordingCoordinator()
    walking = RecordingCoordinator()
    router.manual_candidate = manual  # type: ignore[assignment]
    router.walking = walking  # type: ignore[assignment]
    router.advance({"job_id": job_id, "job_type": JobType.MANUAL_BUILD.value})
    assert [job["job_id"] for job in manual.jobs] == [job_id]
    assert walking.jobs == []
