from __future__ import annotations

from pathlib import Path
from typing import Any

from hcuopt.deployment.nmz36_m1_allocator import build_m1_allocator_hotspot_spec
from hcuopt.deployment.nmz36_m1_formal import (
    CANDIDATE_SOURCE_HASH,
    FORMAL_PERFORMANCE_JOB_ID,
    PROFILE,
    WORKLOAD_ID,
    configuration_document,
    formal_ids,
    performance_job_id,
    recover_terminal_telemetry_failure,
    workload_document,
)
from hcuopt.domain.enums import CandidateState, JobState, JobType, TaskState
from hcuopt.evaluation.m1_protocol import m1_hotspot_spec_sha256
from hcuopt.targets import load_target

TARGET_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "targets"
    / "nmz36-sglang-0.5.12.yaml"
)


def test_nmz36_formal_ids_and_documents_are_deterministic() -> None:
    ids = formal_ids()
    assert ids.task_id == formal_ids().task_id
    assert ids.hotspot_id == formal_ids().hotspot_id
    assert ids.candidate_id == formal_ids().candidate_id
    assert ids.baseline_epoch_id == formal_ids().baseline_epoch_id
    assert performance_job_id() == FORMAL_PERFORMANCE_JOB_ID

    workload = workload_document()
    assert workload["workload_id"] == WORKLOAD_ID
    assert workload["selected_kernel_case"] == "target-4090-direct-nosort"

    configuration = configuration_document(load_target(TARGET_PATH))
    assert configuration["adapter_profile"] == PROFILE
    assert len(configuration["acquisition_order"]) == 40
    assert configuration["batch_iterations"] == 5000
    assert CANDIDATE_SOURCE_HASH != configuration["image_digest"]


def test_nmz36_formal_correctness_spec_binds_hotspot_id() -> None:
    ids = formal_ids()
    spec = build_m1_allocator_hotspot_spec(str(ids.hotspot_id))
    assert spec.hotspot_id == str(ids.hotspot_id)
    assert len(spec.cases) == 6
    assert m1_hotspot_spec_sha256(spec).startswith("sha256:")


class _QueryResult:
    def __init__(self, *, one: Any = None, all_rows: list[dict[str, Any]] | None = None):
        self._one = one
        self._all = all_rows or []

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[dict[str, Any]]:
        return self._all


class _RecoveryConnection:
    def __init__(self) -> None:
        ids = formal_ids()
        self.task = {
            "task_id": ids.task_id,
            "workflow_type": "manual_candidate",
            "state": TaskState.REJECTED.value,
            "automatic_release_allowed": False,
        }
        self.candidate = {
            "candidate_id": ids.candidate_id,
            "task_id": ids.task_id,
            "state": CandidateState.REJECTED.value,
            "verdict": None,
            "evidence_bundle_id": None,
        }
        self.job = {
            "job_id": performance_job_id(),
            "job_type": JobType.MANUAL_PERFORMANCE.value,
            "state": JobState.FAILED.value,
            "attempts": 3,
            "max_attempts": 3,
            "result": None,
            "last_error": {
                "code": "Nmz36RuntimeError",
                "message": (
                    "telemetry command failed: hy-smi --showpids; "
                    "stderr=free(): invalid pointer"
                ),
            },
        }
        self.jobs = [
            {
                "job_id": "build",
                "job_type": JobType.MANUAL_BUILD.value,
                "state": JobState.SUCCEEDED.value,
            },
            {
                "job_id": "correctness",
                "job_type": JobType.MANUAL_CORRECTNESS.value,
                "state": JobState.SUCCEEDED.value,
            },
            {
                "job_id": self.job["job_id"],
                "job_type": JobType.MANUAL_PERFORMANCE.value,
                "state": JobState.FAILED.value,
            },
        ]
        self.writes: list[tuple[str, Any]] = []

    def __enter__(self) -> _RecoveryConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> _QueryResult:
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT * FROM tasks"):
            return _QueryResult(one=self.task)
        if normalized.startswith("SELECT * FROM candidates"):
            return _QueryResult(one=self.candidate)
        if normalized.startswith("SELECT * FROM jobs"):
            return _QueryResult(one=self.job)
        if normalized.startswith("SELECT job_id, job_type, state FROM jobs"):
            return _QueryResult(all_rows=self.jobs)
        if normalized.startswith("SELECT signoff_id"):
            return _QueryResult(one=None)

        self.writes.append((normalized, params))
        if normalized.startswith("UPDATE jobs"):
            self.job.update(
                {
                    "state": JobState.QUEUED.value,
                    "max_attempts": 4,
                }
            )
            self.jobs[-1]["state"] = JobState.QUEUED.value
        elif normalized.startswith("UPDATE tasks"):
            self.task["state"] = TaskState.MANUAL_PERFORMANCE.value
        elif normalized.startswith("UPDATE candidates"):
            self.candidate["state"] = CandidateState.PERFORMANCE_RUNNING.value
        return _QueryResult()


class _RecoveryRepository:
    def __init__(self) -> None:
        self.connection_state = _RecoveryConnection()

    def connection(self) -> _RecoveryConnection:
        return self.connection_state


def test_terminal_telemetry_recovery_is_bounded_audited_and_idempotent() -> None:
    repository = _RecoveryRepository()

    result = recover_terminal_telemetry_failure(repository)  # type: ignore[arg-type]

    assert result == {
        "status": "authorized",
        "job_id": str(performance_job_id()),
        "attempts": 3,
        "max_attempts": 4,
        "automatic_release_allowed": False,
    }
    assert repository.connection_state.job["attempts"] == 3
    assert repository.connection_state.job["last_error"]["code"] == "Nmz36RuntimeError"
    event_writes = [
        params
        for query, params in repository.connection_state.writes
        if query.startswith("INSERT INTO job_events")
        or query.startswith("INSERT INTO task_events")
    ]
    assert len(event_writes) == 2
    assert all(params[1] == "manual_infrastructure_retry_authorized" for params in event_writes)

    write_count = len(repository.connection_state.writes)
    replay = recover_terminal_telemetry_failure(repository)  # type: ignore[arg-type]

    assert replay["status"] == "already_authorized"
    assert len(repository.connection_state.writes) == write_count
