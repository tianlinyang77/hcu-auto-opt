import threading
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.api.app import create_app
from hcuopt.domain.enums import WorkerType
from hcuopt.domain.errors import AdapterUnavailable
from hcuopt.workers.handlers import JobHandlers
from hcuopt.workers.sdk import Worker


class RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((job_type, payload))
        return {"handled": True}


class CleanupRecordingHandler:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.cleanup_calls = 0

    def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.result

    def cleanup(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.cleanup_calls += 1
        return {
            "fence": {"fenced": True, "synthetic": True},
            "health": {"healthy": True, "synthetic": True},
        }


class SuccessfulJobClient:
    def __init__(
        self,
        *,
        resource_id: str | None,
        fencing_token: int | None,
        job_type: str = "profile",
    ) -> None:
        self.resource_id = resource_id
        self.fencing_token = fencing_token
        self.job_type = job_type
        self.completed: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def claim(self, worker_id: str) -> dict[str, Any]:
        return {
            "job_id": str(uuid4()),
            "task_id": str(uuid4()),
            "job_type": self.job_type,
            "payload": {},
            "claim_token": str(uuid4()),
            "fencing_token": self.fencing_token,
            "resource_id": self.resource_id,
            "attempts": 1,
        }

    def heartbeat(self, worker_id: str, job: dict[str, Any]) -> None:
        return None

    def complete(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        self.completed.append((job, result))


def test_fake_registry_declares_all_public_adapter_boundaries() -> None:
    registry = AdapterRegistry.fake()
    assert registry.profile == "fake-v1-control-flow-only"
    assert set(registry.available()) == {
        "profiler",
        "candidate_generator",
        "builder",
        "measurement_harness",
        "evaluator",
        "resource_cleaner",
        "executor",
        "source_manager",
        "artifact_store",
    }
    for capability in registry.available():
        assert registry.require(capability).provenance.profile == registry.profile


def test_missing_adapter_fails_closed() -> None:
    handlers = JobHandlers(AdapterRegistry(profile="empty-test-profile"))
    with pytest.raises(AdapterUnavailable, match="profiler"):
        handlers.handle_profile({"workload_id": "fixture"})


def test_adapter_without_provenance_fails_closed() -> None:
    registry = AdapterRegistry(profile="fixture", profiler=object())  # type: ignore[arg-type]
    with pytest.raises(AdapterUnavailable, match="valid provenance"):
        registry.require("profiler")


def test_worker_accepts_an_injected_handler_without_using_fake_defaults() -> None:
    handler = RecordingHandler()
    worker = Worker(
        "fixture-worker",
        WorkerType.AGENT,
        "http://127.0.0.1:9",
        handlers=handler,
    )
    assert worker.handlers is handler
    assert worker.capabilities["adapter_profile"] == "custom-handler"


def test_worker_rejects_ambiguous_injection() -> None:
    with pytest.raises(ValueError, match="adapters or handlers"):
        Worker(
            "fixture-worker",
            WorkerType.AGENT,
            "http://127.0.0.1:9",
            adapters=AdapterRegistry.fake(),
            handlers=RecordingHandler(),
        )


def test_worker_cancels_and_reports_cleanup_when_heartbeat_loses_lease() -> None:
    cleanup_called = threading.Event()

    class BlockingHandler:
        def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert cleanup_called.wait(timeout=2)
            return {"handled": True}

        def cleanup(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
            cleanup_called.set()
            return {
                "fence": {"fenced": True},
                "health": {"healthy": True},
            }

    class LeaseLosingClient:
        def __init__(self) -> None:
            self.heartbeats = 0
            self.cleanup_reports = []
            self.failures = []
            self.completed = []

        def claim(self, worker_id: str) -> dict[str, Any]:
            return {
                "job_id": str(uuid4()),
                "task_id": str(uuid4()),
                "job_type": "framework_smoke",
                "payload": {"execution_request_id": str(uuid4())},
                "claim_token": str(uuid4()),
                "fencing_token": 7,
                "resource_id": "hcu-7",
                "attempts": 1,
            }

        def heartbeat(self, worker_id: str, job: dict[str, Any]) -> None:
            self.heartbeats += 1
            if self.heartbeats > 1:
                raise RuntimeError("lease lost")

        def report_cleanup(self, resource_id, fencing_token, cleanup_evidence) -> None:
            self.cleanup_reports.append((resource_id, fencing_token, cleanup_evidence))

        def complete(self, job, result) -> None:
            self.completed.append((job, result))

        def fail(self, job, exc, cleanup_evidence=None) -> None:
            self.failures.append((job, exc, cleanup_evidence))

    handler = BlockingHandler()
    client = LeaseLosingClient()
    worker = Worker(
        "fixture-worker",
        WorkerType.GPU,
        "http://127.0.0.1:9",
        heartbeat_seconds=0.01,
        handlers=handler,
    )
    worker.client = client  # type: ignore[assignment]
    worker.registered = True

    assert worker.run_once() is False
    assert cleanup_called.is_set()
    assert client.completed == []
    assert client.cleanup_reports == []
    assert client.failures
    assert client.failures[0][2]["fence"]["fenced"] is True


def test_worker_reports_cleanup_after_stale_failure_rejection() -> None:
    class Handler:
        def handle(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("fixture execution failure")

        def cleanup(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "fence": {"fenced": True},
                "health": {"healthy": True},
            }

    class StaleFailureClient:
        def __init__(self) -> None:
            self.cleanup_reports = []

        def claim(self, worker_id: str) -> dict[str, Any]:
            return {
                "job_id": str(uuid4()),
                "task_id": str(uuid4()),
                "job_type": "framework_smoke",
                "payload": {"execution_request_id": str(uuid4())},
                "claim_token": str(uuid4()),
                "fencing_token": 9,
                "resource_id": "hcu-7",
                "attempts": 1,
            }

        def heartbeat(self, worker_id: str, job: dict[str, Any]) -> None:
            return None

        def fail(self, job, exc, cleanup_evidence=None) -> None:
            raise RuntimeError("claim is already stale")

        def report_cleanup(self, resource_id, fencing_token, cleanup_evidence) -> None:
            self.cleanup_reports.append((resource_id, fencing_token, cleanup_evidence))

    client = StaleFailureClient()
    worker = Worker(
        "fixture-worker",
        WorkerType.GPU,
        "http://127.0.0.1:9",
        handlers=Handler(),
    )
    worker.client = client  # type: ignore[assignment]
    worker.registered = True

    assert worker.run_once() is False
    assert client.cleanup_reports[0][:2] == ("hcu-7", 9)
    assert client.cleanup_reports[0][2]["health"]["healthy"] is True


def test_worker_finalizes_successful_resource_job_before_completion() -> None:
    handler = CleanupRecordingHandler({"handled": True})
    client = SuccessfulJobClient(resource_id="hcu-7", fencing_token=11)
    worker = Worker(
        "fixture-worker",
        WorkerType.GPU,
        "http://127.0.0.1:9",
        handlers=handler,
    )
    worker.client = client  # type: ignore[assignment]
    worker.registered = True

    assert worker.run_once() is True
    assert handler.cleanup_calls == 1
    result = client.completed[0][1]
    assert result["cleanup_evidence"]["fence"]["fenced"] is True
    assert result["cleanup_evidence"]["health"]["healthy"] is True


def test_worker_does_not_finalize_successful_job_without_resource() -> None:
    handler = CleanupRecordingHandler({"handled": True})
    client = SuccessfulJobClient(resource_id=None, fencing_token=None)
    worker = Worker(
        "fixture-worker",
        WorkerType.AGENT,
        "http://127.0.0.1:9",
        handlers=handler,
    )
    worker.client = client  # type: ignore[assignment]
    worker.registered = True

    assert worker.run_once() is True
    assert handler.cleanup_calls == 0
    assert client.completed[0][1] == {"handled": True}


def test_worker_preserves_handler_cleanup_evidence_without_repeating_cleanup() -> None:
    evidence = {
        "fence": {"fenced": True},
        "health": {"healthy": True},
    }

    handler = CleanupRecordingHandler(
        {"handled": True, "cleanup_evidence": evidence}
    )
    client = SuccessfulJobClient(
        resource_id="hcu-7",
        fencing_token=12,
        job_type="framework_smoke",
    )
    worker = Worker(
        "fixture-worker",
        WorkerType.GPU,
        "http://127.0.0.1:9",
        handlers=handler,
    )
    worker.client = client  # type: ignore[assignment]
    worker.registered = True

    assert worker.run_once() is True
    assert handler.cleanup_calls == 0
    assert client.completed[0][1]["cleanup_evidence"] is evidence


def test_job_handlers_clean_up_any_resource_bound_job() -> None:
    handlers = JobHandlers(AdapterRegistry.fake())

    cleanup = handlers.cleanup(
        "profile",
        {
            "_job_context": {
                "resource_id": "hcu-7",
                "fencing_token": 13,
            }
        },
    )

    assert cleanup["fence"]["fenced"] is True
    assert cleanup["fence"]["synthetic"] is True
    assert cleanup["health"]["healthy"] is True
    assert cleanup["health"]["synthetic"] is True


def test_control_plane_uses_an_injected_workflow() -> None:
    class StubRepository:
        def migrate(self) -> None:
            return None

        def freeze_baseline(self, task_id, payload):
            return {
                "baseline_epoch_id": uuid4(),
                "task_id": task_id,
                "hardware_fingerprint": payload.hardware_fingerprint,
                "software_fingerprint": payload.software_fingerprint,
                "workload_id": payload.workload_id,
                "configuration_hash": payload.configuration_hash,
                "frozen": True,
                "created_at": datetime.now(timezone.utc),
            }

    class RecordingWorkflow:
        name = "recording-workflow"

        def __init__(self) -> None:
            self.started: list[UUID] = []

        def start_after_baseline(self, task_id, baseline):
            self.started.append(task_id)
            return {"started": True}

        def advance(self, job) -> None:
            return None

        def reconcile(self) -> list[UUID]:
            return []

    repository = StubRepository()
    workflow = RecordingWorkflow()
    app = create_app(repository=repository, workflow_factory=lambda _repository: workflow)
    task_id = uuid4()
    with TestClient(app) as client:
        response = client.post(
            f"/v1/tasks/{task_id}/baseline",
            json={
                "hardware_fingerprint": "fixture-hardware",
                "software_fingerprint": "fixture-software",
                "workload_id": "fixture-workload",
                "configuration_hash": "fixture-configuration",
            },
        )
    assert response.status_code == 200
    assert workflow.started == [task_id]
