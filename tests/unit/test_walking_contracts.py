from uuid import uuid4

import pytest
from pydantic import ValidationError

from dcuopt.adapters.fake import (
    FakeCandidateGenerator,
    FakeEvaluator,
    FakeMeasurementHarness,
)
from dcuopt.api.app import create_app
from dcuopt.contracts.v1 import TaskCreate
from dcuopt.domain.enums import CandidateState
from dcuopt.domain.transitions import transition_candidate
from dcuopt.orchestrator.walking import WalkingSkeletonCoordinator
from dcuopt.storage.migrations import migration_sql
from dcuopt.workers.handlers import FakeJobHandlers


def test_mvp_contract_rejects_unknown_input_fields() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            name="fixture",
            workload_id="fixture",
            idempotency_key="fixture-key",
            unknown_field=True,
        )


def test_fake_pipeline_marks_every_measurement_as_synthetic() -> None:
    hotspot = {"symbol": "fixture_rmsnorm"}
    candidates = FakeCandidateGenerator().generate(hotspot)
    assert len(candidates) == 2
    assert not FakeEvaluator().correctness(candidates[0], output_dir=None)["passed"]
    result = FakeMeasurementHarness().run({"phase": "performance"}, output_dir=None)
    assert result["synthetic"] is True
    assert result["measurement_protocol"] == "fake-v1-control-flow-only"


def test_fake_job_handlers_generate_stable_candidate_ids() -> None:
    handler = FakeJobHandlers()
    payload = {"task_id": str(uuid4()), "hotspot": {"symbol": "fixture_rmsnorm"}}
    first = handler.handle_candidate_generate(payload)
    second = handler.handle_candidate_generate(payload)
    assert first == second


def test_migration_contains_hard_safety_invariants() -> None:
    sql = migration_sql()
    assert "mvp_never_auto_releases" in sql
    assert "baseline_epochs_immutable" in sql
    assert "fencing_token" in sql
    assert "workflow_advanced_at" in sql
    assert "FOR UPDATE" not in sql  # claims live in repository code, not the schema


def test_api_exposes_all_walking_skeleton_boundaries() -> None:
    paths = create_app().openapi()["paths"]
    expected = {
        "/v1/tasks",
        "/v1/tasks/{task_id}/stage0",
        "/v1/tasks/{task_id}/baseline",
        "/v1/tasks/{task_id}/summary",
        "/v1/tasks/{task_id}/artifacts",
        "/v1/tasks/{task_id}/evaluations",
        "/v1/workers",
        "/v1/workers/{worker_id}/claim",
        "/v1/jobs/{job_id}/complete",
        "/v1/jobs/{job_id}/fail",
        "/v1/jobs/{job_id}/cancel",
        "/v1/maintenance/reap",
        "/v1/resources",
        "/v1/leases",
    }
    assert expected <= paths.keys()


def test_replayed_candidate_advance_never_moves_state_backwards() -> None:
    candidate_id = uuid4()

    class StateRepository:
        state = CandidateState.BUILDING

        def get_candidate(self, _candidate_id):
            return {"state": self.state.value}

        def transition_candidate(self, _candidate_id, target):
            self.state = transition_candidate(self.state, target)

    repository = StateRepository()
    coordinator = WalkingSkeletonCoordinator(repository)  # type: ignore[arg-type]
    coordinator._ensure_candidate_state(candidate_id, CandidateState.CORRECTNESS_RUNNING)
    coordinator._ensure_candidate_state(candidate_id, CandidateState.CORRECTNESS_RUNNING)
    coordinator._ensure_candidate_state(candidate_id, CandidateState.E2E_RUNNING)
    assert repository.state is CandidateState.E2E_RUNNING
