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
    assert result.synthetic is True
    assert result.status == "not_measured"
    assert result.sample_count == 0
    assert result.protocol_version == "fake-v1-control-flow-only"
    assert "speedup_ratio" not in result.summary


def test_fake_job_handlers_generate_stable_candidate_ids() -> None:
    handler = FakeJobHandlers()
    payload = {"task_id": str(uuid4()), "hotspot": {"symbol": "fixture_rmsnorm"}}
    first = handler.handle_candidate_generate(payload)
    second = handler.handle_candidate_generate(payload)
    assert first == second


def test_fake_performance_job_has_no_performance_claim() -> None:
    result = FakeJobHandlers().handle_performance({"candidate_id": str(uuid4())})
    assert result["synthetic"] is True
    assert result["measurement"]["status"] == "not_measured"
    assert result["measurement"]["sample_count"] == 0
    assert "speedup_ratio" not in result
    assert "speedup_ratio" not in result["measurement"]["summary"]


def test_fake_performance_evaluation_is_not_a_pass_verdict() -> None:
    task_id = uuid4()
    candidate_id = uuid4()
    round_id = uuid4()
    baseline_epoch_id = uuid4()

    class EvaluationRepository:
        def get_candidate(self, _candidate_id):
            return {
                "candidate_id": candidate_id,
                "round_id": round_id,
                "baseline_epoch_id": baseline_epoch_id,
            }

        def get_baseline(self, _task_id):
            return {
                "hardware_fingerprint": "fixture-hardware",
                "software_fingerprint": "fixture-software",
                "workload_id": "fixture-workload",
                "configuration_hash": "fixture-config",
            }

    job = {
        "job_id": uuid4(),
        "task_id": task_id,
        "payload": {"candidate_id": str(candidate_id)},
    }
    result = FakeJobHandlers().handle_performance({"candidate_id": str(candidate_id)})
    run = WalkingSkeletonCoordinator(EvaluationRepository())._evaluation_run(  # type: ignore[arg-type]
        job, "performance", result
    )
    assert run.passed is None
    assert run.measurement is not None
    assert run.measurement.status == "not_measured"
    assert "passed" not in run.metrics


def test_migration_contains_hard_safety_invariants() -> None:
    sql = migration_sql(1)
    assert "mvp_never_auto_releases" in sql
    assert "baseline_epochs_immutable" in sql
    assert "fencing_token" in sql
    assert "workflow_advanced_at" in sql
    assert "FOR UPDATE" not in sql  # claims live in repository code, not the schema


def test_evaluation_migration_preserves_runs_and_attempts() -> None:
    sql = migration_sql(2)
    assert "evaluation_runs" in sql
    assert "execution_attempts" in sql
    assert "UNIQUE (evaluation_run_id, attempt_number)" in sql
    assert "UNIQUE (candidate_id, phase)" not in sql
    assert "legacy synthetic performance claims removed" in sql


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
