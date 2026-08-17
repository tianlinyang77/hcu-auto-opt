from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from dcuopt.adapters.interfaces import JobHandler
from dcuopt.adapters.registry import AdapterRegistry
from dcuopt.contracts.v1 import CONTRACT_VERSION
from dcuopt.domain.enums import WorkerType
from dcuopt.workers.handlers import JobHandlers

LOGGER = logging.getLogger(__name__)


class ControlPlaneClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def register(
        self, worker_id: str, worker_type: WorkerType, capabilities: dict[str, Any]
    ) -> None:
        response = self.client.post(
            "/v1/workers",
            json={
                "worker_id": worker_id,
                "worker_type": worker_type.value,
                "contract_version": CONTRACT_VERSION,
                "capabilities": capabilities,
            },
        )
        response.raise_for_status()

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        response = self.client.post(f"/v1/workers/{worker_id}/claim")
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

    def heartbeat(self, worker_id: str, job: dict[str, Any]) -> None:
        response = self.client.post(
            f"/v1/workers/{worker_id}/jobs/{job['job_id']}/heartbeat",
            json={
                "claim_token": job["claim_token"],
                "fencing_token": job.get("fencing_token"),
            },
        )
        response.raise_for_status()

    def complete(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        response = self.client.post(
            f"/v1/jobs/{job['job_id']}/complete",
            json={
                "claim_token": job["claim_token"],
                "fencing_token": job.get("fencing_token"),
                "result": result,
            },
        )
        response.raise_for_status()

    def fail(self, job: dict[str, Any], exc: Exception) -> None:
        response = self.client.post(
            f"/v1/jobs/{job['job_id']}/fail",
            json={
                "claim_token": job["claim_token"],
                "fencing_token": job.get("fencing_token"),
                "error_code": exc.__class__.__name__,
                "message": str(exc),
                "retryable": True,
            },
        )
        response.raise_for_status()


class Worker:
    """Shared registration, claim, heartbeat, failure, and shutdown loop."""

    def __init__(
        self,
        worker_id: str,
        worker_type: WorkerType,
        api_url: str,
        capabilities: dict[str, Any] | None = None,
        heartbeat_seconds: float = 15.0,
        adapters: AdapterRegistry | None = None,
        handlers: JobHandler | None = None,
    ) -> None:
        if adapters is not None and handlers is not None:
            raise ValueError("pass adapters or handlers, not both")
        self.worker_id = worker_id
        self.worker_type = worker_type
        self.capabilities = dict(capabilities or {})
        self.heartbeat_seconds = heartbeat_seconds
        self.client = ControlPlaneClient(api_url)
        if handlers is not None:
            self.handlers = handlers
            self.capabilities.setdefault("adapter_profile", "custom-handler")
        else:
            registry = adapters or AdapterRegistry.fake()
            self.handlers = JobHandlers(registry)
            self.capabilities.setdefault("adapter_profile", registry.profile)
            self.capabilities.setdefault("adapters", list(registry.available()))
        self.stop_event = threading.Event()
        self.registered = False

    def register(self) -> None:
        self.client.register(self.worker_id, self.worker_type, self.capabilities)
        self.registered = True

    def run_once(self) -> bool:
        try:
            if not self.registered:
                self.register()
            job = self.client.claim(self.worker_id)
        except Exception:
            LOGGER.exception("worker %s could not contact the control plane", self.worker_id)
            return False
        if job is None:
            return False
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, heartbeat_stop),
            daemon=True,
        )
        try:
            self.client.heartbeat(self.worker_id, job)
            heartbeat.start()
            result = self.handlers.handle(job["job_type"], job["payload"])
            self.client.complete(job, result)
        except Exception as exc:
            LOGGER.exception("worker %s failed job %s", self.worker_id, job["job_id"])
            try:
                self.client.fail(job, exc)
            except Exception:
                LOGGER.exception("failed to report job failure")
            return False
        finally:
            heartbeat_stop.set()
            if heartbeat.is_alive():
                heartbeat.join(timeout=1)
        return True

    def _heartbeat_loop(self, job: dict[str, Any], stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                self.client.heartbeat(self.worker_id, job)
            except Exception:
                LOGGER.exception("heartbeat failed for job %s", job["job_id"])
                return

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        while not self.stop_event.is_set():
            worked = self.run_once()
            if not worked:
                self.stop_event.wait(poll_seconds)

    def stop(self) -> None:
        self.stop_event.set()
