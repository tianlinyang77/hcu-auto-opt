from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from hcuopt.adapters.interfaces import JobHandler
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.contracts.v1 import CONTRACT_VERSION
from hcuopt.domain.enums import WorkerType
from hcuopt.workers.handlers import JobHandlers

LOGGER = logging.getLogger(__name__)


class ControlPlaneClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def register(
        self,
        worker_id: str,
        worker_type: WorkerType,
        capabilities: dict[str, Any],
        adapter_profile: str | None = None,
    ) -> None:
        response = self.client.post(
            "/v1/workers",
            json={
                "worker_id": worker_id,
                "worker_type": worker_type.value,
                "contract_version": CONTRACT_VERSION,
                "adapter_profile": adapter_profile,
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

    def check_live_lease(self, worker_id: str, job: dict[str, Any]) -> None:
        response = self.client.post(
            f"/v1/workers/{worker_id}/jobs/{job['job_id']}/lease-check",
            json={"claim_token": job["claim_token"], "fencing_token": job.get("fencing_token")},
        )
        response.raise_for_status()
        if response.status_code != 204:
            raise RuntimeError("unexpected live lease check response")

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

    def fail(
        self,
        job: dict[str, Any],
        exc: Exception,
        cleanup_evidence: dict[str, Any] | None = None,
    ) -> None:
        response = self.client.post(
            f"/v1/jobs/{job['job_id']}/fail",
            json={
                "claim_token": job["claim_token"],
                "fencing_token": job.get("fencing_token"),
                "error_code": exc.__class__.__name__,
                "message": str(exc),
                "retryable": True,
                "cleanup_evidence": cleanup_evidence,
            },
        )
        response.raise_for_status()

    def report_cleanup(
        self,
        resource_id: str,
        fencing_token: int,
        cleanup_evidence: dict[str, Any],
    ) -> None:
        response = self.client.post(
            f"/v1/resources/{resource_id}/cleanup",
            json={
                "fencing_token": fencing_token,
                "cleanup_evidence": cleanup_evidence,
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
        output_dir: Path | None = None,
        resource_guard: Callable[[str | None], None] | None = None,
    ) -> None:
        if adapters is not None and handlers is not None:
            raise ValueError("pass adapters or handlers, not both")
        if resource_guard is not None and not callable(resource_guard):
            raise ValueError("resource guard must be a deployment-owned callable")
        self.resource_guard = resource_guard
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
            self.handlers = JobHandlers(registry, output_dir)
            self.capabilities.setdefault("adapter_profile", registry.profile)
            self.capabilities.setdefault("adapters", list(registry.available()))
        self.stop_event = threading.Event()
        self.registered = False

    def register(self) -> None:
        self.client.register(
            self.worker_id,
            self.worker_type,
            self.capabilities,
            self.capabilities.get("adapter_profile"),
        )
        self.registered = True

    def run_once(self) -> bool:
        try:
            self._assert_resource_safe(self.capabilities.get("resource_id"))
            if not self.registered:
                self.register()
            job = self.client.claim(self.worker_id)
        except Exception:
            LOGGER.exception(
                "worker %s not ready or could not contact control plane", self.worker_id
            )
            return False
        if job is None:
            return False
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        payload = dict(job["payload"])
        # Process-local capability, never taken from the incoming JSON payload.
        # The server checks active state/expiry; this does not renew or release.
        lease_job = {key: job.get(key) for key in ("job_id", "claim_token", "fencing_token")}

        def assert_live_lease():
            if lease_lost.is_set() or heartbeat_stop.is_set() or self.stop_event.is_set():
                raise RuntimeError("job stopped or lease already lost")
            try:
                self.client.check_live_lease(self.worker_id, lease_job)
            except Exception:
                lease_lost.set()
                raise
            if lease_lost.is_set() or heartbeat_stop.is_set() or self.stop_event.is_set():
                raise RuntimeError("job stopped or lease lost during authority check")

        payload["_job_context"] = {
            "job_id": job["job_id"],
            "attempt_number": job["attempts"],
            "lease_id": job.get("lease_id"),
            "resource_id": job.get("resource_id"),
            "fencing_token": job.get("fencing_token"),
            "lease_scope": job.get("lease_scope"),
            "lease_lost_event": lease_lost,
            "assert_live_lease": assert_live_lease,
        }
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, heartbeat_stop, lease_lost, payload),
            daemon=True,
        )
        try:
            self.client.heartbeat(self.worker_id, job)
            heartbeat.start()
            # Check again after claim; the durable state may have changed while
            # contacting the control plane. Never accept a payload-supplied guard.
            self._assert_resource_safe(job.get("resource_id"))
            result = self.handlers.handle(job["job_type"], payload)
            if lease_lost.is_set():
                raise RuntimeError("job lease was lost while the handler was running")
            self.client.heartbeat(self.worker_id, job)
            cleanup_evidence = result.get("cleanup_evidence")
            if (
                job.get("resource_id") is not None
                and job.get("fencing_token") is not None
                and not (
                    isinstance(cleanup_evidence, dict)
                    and isinstance(cleanup_evidence.get("fence"), dict)
                    and isinstance(cleanup_evidence.get("health"), dict)
                )
            ):
                result = {
                    **result,
                    "cleanup_evidence": self._cleanup_job(job, payload),
                }
            # Completion commits the job before the coordinator/HTTP response
            # finishes. A concurrent heartbeat then sees a terminal job (409)
            # and can incorrectly start lost-lease cleanup. Quiesce background
            # heartbeats first, then renew/check ownership once synchronously.
            heartbeat_stop.set()
            heartbeat.join(timeout=35)
            if heartbeat.is_alive():
                raise RuntimeError("heartbeat did not stop before job completion")
            if lease_lost.is_set():
                raise RuntimeError("job lease was lost before completion")
            self.client.heartbeat(self.worker_id, job)
            self._assert_resource_safe(job.get("resource_id"))
            self.client.complete(job, result)
        except Exception as exc:
            LOGGER.exception("worker %s failed job %s", self.worker_id, job["job_id"])
            cleanup_evidence = self._cleanup_job(job, payload)
            try:
                self.client.fail(job, exc, cleanup_evidence or None)
            except Exception:
                LOGGER.exception("failed to report job failure")
                self._report_cleanup_after_handler_exit(job, cleanup_evidence)
            return False
        finally:
            heartbeat_stop.set()
            if heartbeat.is_alive():
                heartbeat.join(timeout=1)
        return True

    def _heartbeat_loop(
        self,
        job: dict[str, Any],
        stop: threading.Event,
        lease_lost: threading.Event,
        payload: dict[str, Any],
    ) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                self.client.heartbeat(self.worker_id, job)
            except Exception:
                LOGGER.exception("heartbeat failed for job %s", job["job_id"])
                lease_lost.set()
                self._cleanup_job(job, payload)
                return

    def _cleanup_job(
        self, job: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        cleanup = getattr(self.handlers, "cleanup", None)
        if not callable(cleanup):
            return self._guard_cleanup(job, {})
        try:
            result = dict(cleanup(job["job_type"], payload))
        except Exception:
            LOGGER.exception("cleanup failed for job %s", job["job_id"])
            result = {
                "fence": {"fenced": False, "error": "handler cleanup raised"},
                "health": {"healthy": False, "quarantined": True},
            }
        return self._guard_cleanup(job, result)

    def _assert_resource_safe(self, resource_id: str | None) -> None:
        if self.resource_guard is not None and self.resource_guard(resource_id) is not None:
            raise RuntimeError("resource safety guard did not confirm readiness")

    def _guard_cleanup(self, job, evidence):
        if self.resource_guard is None:
            return evidence
        try:
            self._assert_resource_safe(job.get("resource_id"))
        except Exception as exc:
            # Preserve process-fencing evidence but never release a resource
            # with unresolved device state, even if handler cleanup says healthy.
            health = evidence.get("health")
            return {
                **evidence,
                "health": {**(health if isinstance(health, dict) else {}),
                           "healthy": False, "quarantined": True},
                "resource_guard": {"clear": False, "error_type": type(exc).__name__},
            }
        return {**evidence, "resource_guard": {"clear": True}}

    def _report_cleanup_after_handler_exit(
        self,
        job: dict[str, Any],
        cleanup_evidence: dict[str, Any],
    ) -> None:
        resource_id = job.get("resource_id")
        fencing_token = job.get("fencing_token")
        if not cleanup_evidence or resource_id is None or fencing_token is None:
            return
        try:
            self.client.report_cleanup(
                resource_id,
                int(fencing_token),
                self._guard_cleanup(job, cleanup_evidence),
            )
        except Exception:
            LOGGER.exception(
                "failed to report cleanup for quarantined resource %s",
                resource_id,
            )

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        while not self.stop_event.is_set():
            worked = self.run_once()
            if not worked:
                self.stop_event.wait(poll_seconds)

    def stop(self) -> None:
        self.stop_event.set()
