# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Lease-guarded remote BW20 M1 process and local evidence projection."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from hcuopt.contracts.platform_v1 import ArtifactManifest, TargetSpec
from hcuopt.deployment.bw20_m1_evidence import BW20M1EvidenceMirror
from hcuopt.deployment.bw20_m1_runtime import (
    M1_WORKER_PROTOCOL,
    BW20M1ContainerPlan,
    BW20M1DockerTransport,
)
from hcuopt.measurement.m1_harness import M1PairedWorkload, _proc_start_token
from hcuopt.measurement.m1_models import M1ActivationEvidence
from hcuopt.measurement.models import ProcessIdentity, RawEvidenceFileV2


class BW20M1SessionError(RuntimeError):
    pass


class BW20M1ProcessSession:
    def __init__(
        self,
        *,
        plan: BW20M1ContainerPlan,
        target: TargetSpec,
        transport: BW20M1DockerTransport,
        assert_staging: Callable[[BW20M1ContainerPlan], None],
        assert_lease: Callable[[], None],
        bind_process: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
        cancelled: Callable[[], bool],
        expected_module_hash: str,
        budget_seconds: float = 480,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(budget_seconds) not in (int, float) or not 0 < budget_seconds <= 480:
            raise ValueError("BW20 M1 process budget must be within 480 seconds")
        self.plan = plan
        self.target = target
        self.transport = transport
        self.assert_staging = assert_staging
        self.assert_lease = assert_lease
        self.bind_process = bind_process
        self.cancelled = cancelled
        self.expected_module_hash = expected_module_hash
        self.monotonic = monotonic
        self.deadline = monotonic() + budget_seconds
        self.container_id: str | None = None
        self.channel = None
        self.start_observation: dict[str, Any] | None = None
        self.exit_observation: dict[str, Any] | None = None
        self.identity_observations: list[dict[str, Any]] = []
        self.closed = False
        self.cleanup_complete = False
        self.create_attempted = False

    def _guard(self) -> float:
        if self.closed or self.cancelled():
            raise BW20M1SessionError("BW20 M1 session is closed or cancelled")
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("BW20 M1 process budget exhausted")
        if self.assert_lease() is not None:
            raise BW20M1SessionError("live lease guard must raise on failure")
        remaining = self.deadline - self.monotonic()
        if remaining <= 0 or self.cancelled():
            raise BW20M1SessionError("lease expired or process cancelled")
        # Every Docker/stdio operation is routed through BW20's bounded
        # transport, whose per-call safety ceiling is 90 seconds.  The process
        # session may live for up to 480 seconds, but that total budget must not
        # be forwarded as one transport timeout.
        return min(90.0, remaining)

    def open(self) -> BW20M1ProcessSession:
        if self.create_attempted or self.closed:
            raise BW20M1SessionError("BW20 M1 session cannot be restarted")
        try:
            self._guard()
            if self.assert_staging(self.plan) is not None:
                raise BW20M1SessionError("staging guard must raise on failure")
            self.create_attempted = True
            self.container_id = self.transport.create(self.plan, self._guard())
            self.channel = self.transport.start(self.container_id, self._guard())
            ready = dict(self.channel.receive(self._guard()))
            self._validate_ready(ready)
            self.start_observation = ready
            self.process_id = int(ready["process_id"])
            self.process_start_token = _proc_start_token(
                str(ready["proc_stat_line"]), self.process_id
            )
            if ready.get("process_start_token") != self.process_start_token:
                raise BW20M1SessionError("M1 child start token attestation differs")
            self._binding()
            self._guard()
            return self
        except BaseException:
            self.force_close()
            raise

    def _validate_ready(self, ready: Mapping[str, Any]) -> None:
        process_id = ready.get("process_id")
        expected_device = {
            "pci": "0000:b1:00.0",
            "architecture": "gfx936",
            "logical_device_index": 0,
        }
        if (
            ready.get("protocol") != M1_WORKER_PROTOCOL
            or ready.get("event") != "ready"
            or ready.get("observer_process_id") != 1
            or type(process_id) is not int
            or process_id < 2
            or ready.get("module_hash") != self.expected_module_hash
            or ready.get("device_identity") != expected_device
        ):
            raise BW20M1SessionError("BW20 M1 worker activation attestation is invalid")

    def _binding(self) -> None:
        if self.container_id is None or self.start_observation is None:
            raise BW20M1SessionError("BW20 M1 process is not ready")
        value = dict(self.bind_process(self.container_id, self.start_observation))
        if (
            value.get("container_id") != self.container_id
            or value.get("resource_id") != self.plan.resource_id
            or value.get("fencing_token") != self.plan.fencing_token
            or value.get("worker_protocol") != M1_WORKER_PROTOCOL
            or value.get("container_ready") != self.start_observation
        ):
            raise BW20M1SessionError("BW20 M1 host process binding is invalid")
        self.identity_observations.append(value)

    def request(self, payload: Mapping[str, Any], timeout_seconds: float = 180) -> dict[str, Any]:
        if self.channel is None or self.start_observation is None:
            raise BW20M1SessionError("BW20 M1 process is not open")
        try:
            if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 180:
                raise ValueError("BW20 M1 request timeout must be within 180 seconds")
            self._binding()
            response = dict(
                self.channel.request(payload, min(float(timeout_seconds), self._guard()))
            )
            if response.get("protocol") != M1_WORKER_PROTOCOL or response.get("event") == "error":
                raise BW20M1SessionError("BW20 M1 worker returned an invalid response")
            self._guard()
            if payload.get("op") != "close":
                self._binding()
            return response
        except BaseException:
            self.force_close()
            raise

    def close(self) -> None:
        if self.closed:
            if not self.cleanup_complete:
                raise BW20M1SessionError("BW20 M1 cleanup remains unconfirmed")
            return
        try:
            response = self.request({"op": "close"}, 60)
            if (
                response.get("event") != "closing"
                or response.get("observer_process_id") != 1
                or response.get("process_id") != self.process_id
                or response.get("waitpid_result_pid") != self.process_id
                or response.get("wait_status") != 0
                or _proc_start_token(str(response.get("proc_stat_line")), self.process_id)
                != self.process_start_token
            ):
                raise BW20M1SessionError("BW20 M1 child reaping evidence is invalid")
            if self.channel.wait(self._guard()) != 0:
                raise BW20M1SessionError("BW20 M1 container exited unsuccessfully")
            self.exit_observation = response
        finally:
            self.force_close()
        if not self.cleanup_complete:
            raise BW20M1SessionError("BW20 M1 cleanup remains unconfirmed")

    def force_close(self) -> None:
        self.closed = True
        self.cleanup_complete = not self.create_attempted
        try:
            if self.container_id is not None:
                raw = self.transport.inspect(self.container_id, 15)
                if raw is not None:
                    self.transport.remove(self.container_id, 15)
                self.cleanup_complete = self.transport.inspect(self.container_id, 15) is None
        except Exception:
            self.cleanup_complete = False
        finally:
            if self.channel is not None:
                try:
                    self.channel.abort()
                except Exception:
                    self.cleanup_complete = False

    def is_alive(self) -> bool:
        return not self.closed and self.channel is not None and self.channel.poll() is None


class BW20M1PairedWorkload(M1PairedWorkload):
    def __init__(
        self,
        *,
        process: BW20M1ProcessSession,
        target: TargetSpec,
        artifact: ArtifactManifest,
        mirror: BW20M1EvidenceMirror,
    ) -> None:
        self.process = process
        self.target = target
        self.artifact = artifact
        self.mirror = mirror
        self.identity = ProcessIdentity(
            pid=process.process_id,
            start_token=process.process_start_token,
        )

    def process_identity(self) -> ProcessIdentity:
        return self.identity

    def activation_evidence(self) -> M1ActivationEvidence:
        ready = self.process.start_observation
        assert ready is not None
        cache = self.mirror.fetch("cache-namespace.json", str(ready["cache_sha256"]))
        imported = None
        if self.process.plan.arm == "candidate":
            imported = self.mirror.fetch(
                "import-attestation.json", str(ready["import_sha256"])
            )
        return M1ActivationEvidence(
            arm=self.process.plan.arm,
            activation_mode=(
                "startup_overlay" if self.process.plan.arm == "candidate" else "baseline"
            ),
            image_digest=self.target.inference_image.registry_digest,
            loaded_artifact_hash=(
                self.artifact.content_hash
                if self.process.plan.arm == "candidate"
                else None
            ),
            import_attestation=imported,
            cache_namespace_hash=str(ready["namespace_hash"]),
            cache_namespace_evidence=cache,
            cache_empty_before_execution=True,
        )

    def synchronize(self) -> None:
        response = self.process.request({"op": "synchronize"})
        if response.get("event") != "synchronized":
            raise BW20M1SessionError("BW20 M1 synchronize acknowledgement is invalid")

    def warmup(self) -> None:
        response = self.process.request({"op": "warmup", "iterations": 1})
        if response.get("event") != "warmed":
            raise BW20M1SessionError("BW20 M1 warmup acknowledgement is invalid")

    def measure_batch(self, iterations: int) -> RawEvidenceFileV2:
        response = self.process.request({"op": "measure", "iterations": iterations})
        if response.get("event") != "measured" or type(response.get("sample_ordinal")) is not int:
            raise BW20M1SessionError("BW20 M1 measurement acknowledgement is invalid")
        ordinal = response["sample_ordinal"]
        return self.mirror.fetch(
            f"device-event-{ordinal:04d}.json", str(response["event_sha256"])
        )

    def close(self) -> None:
        self.process.close()

    def is_alive(self) -> bool:
        return self.process.is_alive()


__all__ = [
    "BW20M1PairedWorkload",
    "BW20M1ProcessSession",
    "BW20M1SessionError",
]
