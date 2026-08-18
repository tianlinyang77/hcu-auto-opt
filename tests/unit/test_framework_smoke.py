from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from hcuopt.adapters.profiles import (
    FRAMEWORK_SMOKE_CAPABILITIES,
    AdapterProfile,
    AdapterProfileCatalog,
)
from hcuopt.api.app import create_app
from hcuopt.contracts.v1 import FrameworkSmokeResult
from hcuopt.domain.enums import CandidateState, TaskState
from hcuopt.domain.errors import AdapterUnavailable, TargetConfigError, TargetNotReady
from hcuopt.domain.transitions import transition_candidate, transition_task
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
    profile = AdapterProfile(
        name="real-test",
        implementation_kind="real",
        capabilities=FRAMEWORK_SMOKE_CAPABILITIES,
    )
    profile.validate_target(target)
    with pytest.raises(TargetNotReady, match="open blockers for stage0"):
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
    assert "/v1/resources/{resource_id}/cleanup" in paths


def test_framework_create_returns_stable_profile_and_target_errors() -> None:
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

    target_catalog = TargetCatalog(TARGET_PATH.parent)
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
    assert response.status_code == 201
