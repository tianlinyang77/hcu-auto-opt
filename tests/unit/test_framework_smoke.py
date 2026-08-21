from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from hcuopt.adapters.profiles import (
    FRAMEWORK_SMOKE_CAPABILITIES,
    STAGE0_CAPABILITIES,
    AdapterProfile,
    AdapterProfileCatalog,
)
from hcuopt.api.app import create_app
from hcuopt.contracts.v1 import FrameworkSmokeResult
from hcuopt.domain.enums import CandidateState, JobType, TaskState
from hcuopt.domain.errors import AdapterUnavailable, TargetConfigError, TargetNotReady
from hcuopt.domain.transitions import transition_candidate, transition_task
from hcuopt.orchestrator.router import WorkflowRouter
from hcuopt.targets import TargetCatalog, load_target
from hcuopt.workers.handlers import FakeJobHandlers

ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"


def test_framework_smoke_has_an_independent_state_chain() -> None:
    assert (
        transition_task(TaskState.CREATED, TaskState.FRAMEWORK_SMOKE_PENDING)
        is TaskState.FRAMEWORK_SMOKE_PENDING
    )
    assert (
        transition_task(TaskState.FRAMEWORK_SMOKE_PENDING, TaskState.SOURCE_PREPARING)
        is TaskState.SOURCE_PREPARING
    )
    assert (
        transition_candidate(CandidateState.BUILT, CandidateState.FRAMEWORK_SMOKE_RUNNING)
        is CandidateState.FRAMEWORK_SMOKE_RUNNING
    )
    assert (
        transition_candidate(
            CandidateState.FRAMEWORK_SMOKE_PASSED,
            CandidateState.FRAMEWORK_SMOKE_RUNNING,
        )
        is CandidateState.FRAMEWORK_SMOKE_RUNNING
    )


def test_adapter_profile_fails_closed_for_unknown_or_missing_capability() -> None:
    catalog = AdapterProfileCatalog()
    with pytest.raises(AdapterUnavailable, match="not registered"):
        catalog.require("implicit-fake")

    incomplete = AdapterProfile(
        name="incomplete-real",
        implementation_kind="real",
        capabilities=FRAMEWORK_SMOKE_CAPABILITIES - {"resource_cleaner"},
    )
    with pytest.raises(AdapterUnavailable, match="resource_cleaner"):
        incomplete.require_framework_smoke()


def test_real_profile_applies_blockers_to_the_declared_gate() -> None:
    target = load_target(TARGET_PATH)
    framework_blocked = target.model_copy(
        update={
            "blockers": [
                blocker.model_copy(update={"status": "open"})
                if blocker.id == "locked_image_dependency_conflict"
                else blocker
                for blocker in target.blockers
            ]
        }
    )
    profile = AdapterProfile(
        name="real-test",
        implementation_kind="real",
        capabilities=FRAMEWORK_SMOKE_CAPABILITIES | STAGE0_CAPABILITIES,
    )
    with pytest.raises(
        TargetNotReady,
        match="open blockers for framework_smoke: locked_image_dependency_conflict",
    ):
        profile.validate_target(framework_blocked)
    # HCU 7 isolation is an explicitly accepted risk. Accepted does not mean
    # physically proven, but it no longer blocks Stage 0 intake.
    profile.validate_target(target, scope="stage0")


def test_target_catalog_lists_valid_targets_and_rejects_duplicate_ids(
    tmp_path: Path,
) -> None:
    raw = TARGET_PATH.read_text(encoding="utf-8")
    (tmp_path / "nmz36-sglang-0.5.12.yaml").write_text(raw, encoding="utf-8")
    catalog = TargetCatalog(tmp_path)
    assert [item.target_id for item in catalog.list()] == ["nmz36-sglang-0.5.12"]

    (tmp_path / "nmz36-sglang-0.5.12.yml").write_text(raw, encoding="utf-8")
    with pytest.raises(TargetConfigError, match="duplicate target id"):
        catalog.list()


def test_fake_framework_smoke_result_is_linked_and_has_no_performance_claim() -> None:
    target = load_target(TARGET_PATH)
    handlers = FakeJobHandlers()
    task_id = uuid4()
    candidate_id = uuid4()
    round_id = uuid4()
    baseline_epoch_id = uuid4()
    source = handlers.handle_source_prepare(
        {"target": target.model_dump(mode="json")}
    )
    build = handlers.handle_noop_build(
        {
            "baseline_source": source["source"],
            "candidate_id": str(candidate_id),
        }
    )
    raw_result = handlers.handle_framework_smoke(
        {
            "task_id": str(task_id),
            "candidate_id": str(candidate_id),
            "round_id": str(round_id),
            "baseline_epoch_id": str(baseline_epoch_id),
            "target": target.model_dump(mode="json"),
            "target_fingerprint": "sha256:" + "0" * 64,
            "artifact": build["artifact"],
            "evaluation_run_id": str(uuid4()),
            "execution_request_id": str(uuid4()),
            "evidence_id": str(uuid4()),
            "retest_ordinal": 0,
            "_job_context": {
                "attempt_number": 1,
                "resource_id": "fake-hcu-0",
                "fencing_token": 1,
            },
        }
    )
    result = FrameworkSmokeResult.model_validate(raw_result)
    serialized = yaml.safe_dump(result.model_dump(mode="json"))
    assert result.synthetic is True
    assert result.evaluation.passed is True
    assert result.evidence.summary["performance_conclusion"] == "not_measured"
    for forbidden in ("speedup_ratio", "latency", "throughput", "ci_low", "ci_high"):
        assert forbidden not in serialized


def test_openapi_exposes_framework_smoke_control_plane() -> None:
    class StubRepository:
        def migrate(self) -> None:
            return None

    app = create_app(repository=StubRepository())  # type: ignore[arg-type]
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/v1/targets" in paths
    assert "/v1/targets/{target_id}" in paths
    assert "/v1/framework-smoke/tasks" in paths
    assert "/v1/framework-smoke/tasks/{task_id}/summary" in paths
    assert "/v1/framework-smoke/tasks/{task_id}/cancel" in paths
    assert "/v1/framework-smoke/tasks/{task_id}/retest" in paths
    assert "/v1/framework-smoke/tasks/{task_id}/signoff" in paths
    assert "/v1/stage0-runs" in paths
    assert "/v1/stage0-runs/{stage0_run_id}" in paths
    assert "/v1/stage0-runs/{stage0_run_id}/finalize" in paths
    assert "/v1/resources/{resource_id}/cleanup" in paths


def test_framework_create_returns_stable_profile_and_target_errors(tmp_path: Path) -> None:
    class StubRepository:
        def migrate(self) -> None:
            return None

        def create_framework_smoke_task(self, payload, target, source_path):
            now = datetime.now(timezone.utc)
            return {
                "task_id": uuid4(),
                "name": payload.name,
                "workload_id": f"framework-smoke:{target.target_id}",
                "state": "source_preparing",
                "project_mode": None,
                "budget": {},
                "automatic_release_allowed": False,
                "version": 0,
                "created_at": now,
                "updated_at": now,
                "workflow_type": "framework_smoke",
                "target_id": target.target_id,
                "target_snapshot_id": uuid4(),
                "adapter_profile": payload.adapter_profile,
                "retest_count": 0,
            }

    raw_target = yaml.safe_load(TARGET_PATH.read_text(encoding="utf-8"))
    for blocker in raw_target["blockers"]:
        if blocker["id"] == "locked_image_dependency_conflict":
            blocker["status"] = "open"
    (tmp_path / TARGET_PATH.name).write_text(
        yaml.safe_dump(raw_target, sort_keys=False), encoding="utf-8"
    )
    target_catalog = TargetCatalog(tmp_path)
    missing_profile_app = create_app(
        repository=StubRepository(),  # type: ignore[arg-type]
        target_catalog=target_catalog,
        adapter_profiles=AdapterProfileCatalog(()),
    )
    payload = {
        "name": "fixture",
        "target_id": "nmz36-sglang-0.5.12",
        "adapter_profile": "missing",
        "idempotency_key": "fixture-framework-task",
    }
    with TestClient(missing_profile_app) as client:
        response = client.post("/v1/framework-smoke/tasks", json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "adapter_unavailable"

    real = AdapterProfile(
        name="real-test",
        implementation_kind="real",
        capabilities=FRAMEWORK_SMOKE_CAPABILITIES,
    )
    real_profile_app = create_app(
        repository=StubRepository(),  # type: ignore[arg-type]
        target_catalog=target_catalog,
        adapter_profiles=AdapterProfileCatalog((real,)),
    )
    payload["adapter_profile"] = "real-test"
    with TestClient(real_profile_app) as client:
        response = client.post("/v1/framework-smoke/tasks", json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "target_not_ready"
    assert "locked_image_dependency_conflict" in response.json()["message"]


def test_formal_stage0_rejects_fake_profile_before_creating_a_run() -> None:
    class StubRepository:
        def migrate(self) -> None:
            return None

    app = create_app(repository=StubRepository())  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post(
            "/v1/stage0-runs",
            json={
                "name": "formal fixture",
                "workload_id": "fixture",
                "target_id": "nmz36-sglang-0.5.12",
                "adapter_profile": "fake-v1-control-flow-only",
                "mode": "formal",
                "protocol_version": "fixture-v1",
                "idempotency_key": "formal-stage0-fake-profile",
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert "real Adapter Profile" in response.json()["message"]


def test_stage0_public_budget_rejects_executable_probe_configuration() -> None:
    class StubRepository:
        def migrate(self) -> None:
            return None

    app = create_app(repository=StubRepository())  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post(
            "/v1/stage0-runs",
            json={
                "name": "untrusted runtime configuration",
                "workload_id": "fixture",
                "target_id": "nmz36-sglang-0.5.12",
                "adapter_profile": "nmz36-stage0-v2",
                "mode": "dry_run",
                "protocol_version": "fixture-v1",
                "idempotency_key": "reject-runtime-probe-injection",
                "budget": {
                    "runtime_probe": {
                        "profiler": {"profile_argv": ["sh", "-c", "untrusted"]}
                    }
                },
            },
        )

    assert response.status_code == 422
    assert "runtime_probe" in response.text


def test_stage0_run_rejects_profile_without_stage0_capability() -> None:
    class StubRepository:
        def migrate(self) -> None:
            return None

    framework_only = AdapterProfile(
        name="framework-only",
        implementation_kind="real",
        capabilities=FRAMEWORK_SMOKE_CAPABILITIES,
    )
    app = create_app(
        repository=StubRepository(),  # type: ignore[arg-type]
        adapter_profiles=AdapterProfileCatalog((framework_only,)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/stage0-runs",
            json={
                "name": "capability boundary fixture",
                "workload_id": "fixture",
                "target_id": "nmz36-sglang-0.5.12",
                "adapter_profile": "framework-only",
                "mode": "dry_run",
                "protocol_version": "fixture-v1",
                "idempotency_key": "stage0-missing-capability",
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "adapter_unavailable"
    assert "stage0_probe" in response.json()["message"]


def test_router_reconcile_dispatches_recovery_by_job_type() -> None:
    stage0_job_id = uuid4()
    optimization_job_id = uuid4()

    class StubRepository:
        def unadvanced_succeeded_jobs(self):
            return [
                {"job_id": stage0_job_id, "job_type": JobType.STAGE0_PROBE.value},
                {"job_id": optimization_job_id, "job_type": JobType.PROFILE.value},
            ]

    class RecordingCoordinator:
        def __init__(self) -> None:
            self.jobs: list[dict] = []

        def advance(self, job: dict) -> None:
            self.jobs.append(job)

    router = WorkflowRouter(StubRepository())  # type: ignore[arg-type]
    stage0 = RecordingCoordinator()
    walking = RecordingCoordinator()
    router.stage0 = stage0  # type: ignore[assignment]
    router.walking = walking  # type: ignore[assignment]
    router.framework_smoke = RecordingCoordinator()  # type: ignore[assignment]

    assert router.reconcile() == [stage0_job_id, optimization_job_id]
    assert [job["job_id"] for job in stage0.jobs] == [stage0_job_id]
    assert [job["job_id"] for job in walking.jobs] == [optimization_job_id]
