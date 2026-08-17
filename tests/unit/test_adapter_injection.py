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
