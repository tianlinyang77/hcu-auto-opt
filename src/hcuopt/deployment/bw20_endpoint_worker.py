# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Exclusive-Lease Worker for one BW20 provisional endpoint ABBA group."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.adapters.bw20_endpoint_execution import (
    PROFILE,
    BW20EndpointExecutionAdapter,
)
from hcuopt.adapters.execution import FencingGuard
from hcuopt.adapters.interfaces import ExecutionAdapter
from hcuopt.adapters.registry import AdapterRegistry
from hcuopt.adapters.resource_cleaner import ContainerResourceCleaner
from hcuopt.contracts.endpoint_control_v1 import (
    EndpointValidationRunCreate,
    endpoint_create_plan_hash,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance, ExecutionResult, TargetSpec
from hcuopt.deployment.bw20_endpoint_staging import (
    ACQUISITION_ORDER,
    PreparedEndpointRun,
    StagedEndpointRun,
    prepare_endpoint_run,
    stage_endpoint_run,
)
from hcuopt.deployment.bw20_local_runner import BW20LocalCommandRunner
from hcuopt.deployment.bw20_m1_correctness_worker import BW20M1IdleGuard
from hcuopt.domain.enums import LeaseScope, WorkerType
from hcuopt.domain.errors import ExecutionSafetyError
from hcuopt.targets import load_target
from hcuopt.workers.sdk import Worker

_IDLE_WINDOW_ERROR = "BW20 HCU 7 is not idle in the accepted auto window"


def _sha256(path: Path, *, limit: int = 2 * 1024**3) -> str:
    before = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= limit
    ):
        raise ExecutionSafetyError(f"unsafe endpoint evidence file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise ExecutionSafetyError(f"endpoint evidence changed while reading: {path.name}")
    return "sha256:" + digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    _sha256(path, limit=16 * 1024 * 1024)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ExecutionSafetyError(f"oversized endpoint JSON evidence: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionSafetyError(f"invalid endpoint JSON evidence: {path.name}") from exc
    if not isinstance(value, dict):
        raise ExecutionSafetyError(f"endpoint JSON evidence is not an object: {path.name}")
    return value


class BW20EndpointMeasurementRunner:
    def __init__(
        self,
        *,
        target: TargetSpec,
        source_root: Path,
        runner: BW20LocalCommandRunner,
        stage: Callable[..., StagedEndpointRun] = stage_endpoint_run,
        executor_factory: Callable[[str], ExecutionAdapter] | None = None,
    ) -> None:
        if target.target_id != "bw20-sglang-0.5.12":
            raise ValueError("endpoint Runner requires the BW20 Target Lock")
        self.target = target
        resolved_source = source_root.resolve(strict=True)
        if source_root.is_symlink() or source_root.absolute() != resolved_source:
            raise ValueError("endpoint Runner source root must not be redirected")
        self.source_root = resolved_source
        self.runner = runner
        self.stage = stage
        guard = FencingGuard()
        self.executor_factory = executor_factory or (
            lambda run_root: BW20EndpointExecutionAdapter(
                runner, run_root=run_root, fencing_guard=guard
            )
        )
        self.provenance = AdapterProvenance(
            profile=PROFILE,
            capability="endpoint_measurement_runner",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )

    def run_endpoint_validation(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> Mapping[str, Any]:
        context = payload.get("_job_context")
        if not isinstance(context, dict):
            raise ExecutionSafetyError("endpoint Runner requires trusted Job context")
        assert_live_lease = context.get("assert_live_lease")
        if (
            context.get("lease_scope") != LeaseScope.EXCLUSIVE.value
            or context.get("resource_id") != "bw20-sglang-0.5.12:hcu:7"
            or type(context.get("fencing_token")) is not int
            or not callable(assert_live_lease)
        ):
            raise ExecutionSafetyError("endpoint Runner requires the HCU 7 exclusive Lease")
        request = EndpointValidationRunCreate.model_validate(
            {
                "name": "worker-materialized-endpoint-run",
                "signed_m1": payload["signed_m1"],
                "workload": payload["workload"],
                "plan": payload["plan"],
                "environment_fingerprint": payload["environment_fingerprint"],
                "adapter_profile": payload["adapter_profile"],
                "idempotency_key": f"worker-{payload['endpoint_run_id']}",
            }
        )
        if (
            request.adapter_profile != PROFILE
            or payload.get("plan_hash") is None
            or endpoint_create_plan_hash(request) != payload.get("plan_hash")
            or request.workload.target_id != self.target.target_id
        ):
            raise ExecutionSafetyError("endpoint Job payload differs from the Worker profile")
        run_id = UUID(str(payload["endpoint_run_id"]))
        root = output_dir.resolve(strict=True)
        prepared_dir = root / f"endpoint-{run_id}"
        assert_live_lease()
        prepared = prepare_endpoint_run(
            repository=self.source_root,
            destination=prepared_dir,
            run_id=run_id,
            fencing_token=int(context["fencing_token"]),
            target=self.target,
            warmup_requests=request.plan.warmup_requests,
            measured_requests=request.plan.measured_requests_per_acquisition,
            ready_timeout_seconds=request.plan.ready_timeout_seconds,
            request_timeout_seconds=request.plan.request_timeout_seconds,
        )
        # The staging plan has its own Hash because it additionally binds mounts
        # and file identities. The API plan Hash remains the measurement identity.
        measurement_plan_hash = str(payload["plan_hash"])
        assert_live_lease()
        staged = self.stage(prepared=prepared, runner=self.runner)
        receipt_path = prepared.directory / "staging-receipt.json"
        with receipt_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(staged.receipt, stream, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
        receipt_path.chmod(0o444)
        executor = self.executor_factory(prepared.remote_run_root)
        acquisitions = []
        for ordinal, (arm, execution_request) in enumerate(
            zip(ACQUISITION_ORDER, prepared.requests, strict=True)
        ):
            assert_live_lease()
            result = executor.execute(execution_request, self.target, root)
            if result.status != "succeeded" or result.exit_code != 0:
                raise ExecutionSafetyError(
                    f"endpoint acquisition {ordinal} failed before evidence acceptance"
                )
            acquisitions.append(
                self._read_acquisition(prepared, result, ordinal=ordinal, arm=arm)
            )
            assert_live_lease()
        return {
            "schema_version": "bw20-endpoint-provisional-result-v1",
            "endpoint_run_id": str(run_id),
            "run_mode": "provisional",
            "status": "provisional_passed",
            "plan_hash": measurement_plan_hash,
            "staging_receipt_hash": _sha256(receipt_path),
            "acquisitions": acquisitions,
            "adapter_provenance": [self.provenance.model_dump(mode="json")],
            "producer_verdict": None,
            "automatic_release_allowed": False,
        }

    @staticmethod
    def _read_acquisition(
        prepared: PreparedEndpointRun,
        execution: ExecutionResult,
        *,
        ordinal: int,
        arm: str,
    ) -> dict[str, Any]:
        output = (
            Path(prepared.remote_run_root)
            / "acquisitions"
            / f"{ordinal:04d}-{arm}"
        )
        expected = {
            "result.json",
            "activation.json",
            "cache-namespace.json",
            "cache-cleanup.json",
            "stop.json",
        }
        if not output.is_dir() or any(not (output / name).is_file() for name in expected):
            raise ExecutionSafetyError("endpoint acquisition evidence is incomplete")
        result = _json(output / "result.json")
        stop = _json(output / "stop.json")
        cache = _json(output / "cache-cleanup.json")
        activation = _json(output / "activation.json")
        if (
            result.get("status") != "succeeded"
            or result.get("cleanup_succeeded") is not True
            or result.get("cache_cleanup_succeeded") is not True
            or result.get("producer_verdict") is not None
            or result.get("automatic_release_allowed") is not False
            or stop.get("cleanup_succeeded") is not True
            or cache.get("removed") is not True
            or activation.get("schema_version")
            != "sglang-endpoint-import-attestation-v1"
            or execution.metadata.get("arm") != arm
        ):
            raise ExecutionSafetyError("endpoint acquisition evidence failed closed")
        return {
            "acquisition_ordinal": ordinal,
            "arm": arm,
            "evidence_uri": output.as_uri(),
            "result_sha256": _sha256(output / "result.json"),
            "activation_sha256": _sha256(output / "activation.json"),
            "cache_namespace_sha256": _sha256(output / "cache-namespace.json"),
            "cleanup_succeeded": True,
        }


class BW20EndpointCleaner:
    def __init__(
        self, target: TargetSpec, guard: BW20EndpointIdleSettlementGuard, runner
    ) -> None:
        self.containers = ContainerResourceCleaner(target, runner, profile=PROFILE)
        self.guard = guard
        self.provenance = self.containers.provenance.model_copy(
            update={"adapter_name": type(self).__name__}
        )

    def fence(self, resource_id: str, fencing_token: int) -> Mapping[str, object]:
        return self.containers.fence(resource_id, fencing_token)

    def health_check(self, resource_id: str) -> Mapping[str, object]:
        try:
            self.guard(resource_id)
        except Exception as exc:
            return {
                "resource_id": resource_id,
                "healthy": False,
                "quarantined": True,
                "error_type": type(exc).__name__,
                "clock_mutation_performed": False,
            }
        return {
            "resource_id": resource_id,
            "healthy": True,
            "quarantined": False,
            "host_observation": self.guard.observations[-1],
            "idle_settlement": self.guard.last_settlement,
            "clock_mutation_performed": False,
        }


class BW20EndpointIdleSettlementGuard:
    """Require two stable idle readbacks without weakening the HCU guard.

    SGLang process teardown and device accounting are not atomic.  The generic
    Worker checks the device immediately after the Handler returns, so a single
    transient busy/VRAM readback must be allowed to settle.  Only the known
    idle-window rejection is retryable; telemetry, attribution and resource
    identity failures remain immediate fail-closed errors.
    """

    def __init__(
        self,
        guard: BW20M1IdleGuard,
        *,
        required_consecutive: int = 2,
        max_attempts: int = 30,
        interval_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if required_consecutive < 2 or max_attempts < required_consecutive:
            raise ValueError("endpoint idle settlement requires bounded stable samples")
        if interval_seconds < 0:
            raise ValueError("endpoint idle settlement interval must be non-negative")
        self.guard = guard
        self.required_consecutive = required_consecutive
        self.max_attempts = max_attempts
        self.interval_seconds = interval_seconds
        self.sleep = sleep
        self.last_settlement: dict[str, object] | None = None

    @property
    def observations(self) -> list[dict[str, Any]]:
        return self.guard.observations

    def __call__(self, resource_id: str | None) -> None:
        consecutive = 0
        transient_failures = 0
        for attempt in range(1, self.max_attempts + 1):
            try:
                self.guard(resource_id)
            except ExecutionSafetyError as exc:
                if str(exc) != _IDLE_WINDOW_ERROR:
                    raise
                consecutive = 0
                transient_failures += 1
            else:
                consecutive += 1
                if consecutive >= self.required_consecutive:
                    self.last_settlement = {
                        "required_consecutive": self.required_consecutive,
                        "attempts": attempt,
                        "transient_failures": transient_failures,
                        "interval_seconds": self.interval_seconds,
                        "settled": True,
                    }
                    return None
            if attempt < self.max_attempts:
                self.sleep(self.interval_seconds)
        self.last_settlement = {
            "required_consecutive": self.required_consecutive,
            "attempts": self.max_attempts,
            "transient_failures": transient_failures,
            "interval_seconds": self.interval_seconds,
            "settled": False,
        }
        raise ExecutionSafetyError(_IDLE_WINDOW_ERROR)


def build_worker(
    *, worker_id: str, api_url: str, target: TargetSpec, source_root: Path,
    output_dir: Path,
) -> Worker:
    runner = BW20LocalCommandRunner()
    raw_guard = BW20M1IdleGuard(runner)
    raw_guard("bw20-sglang-0.5.12:hcu:7")
    guard = BW20EndpointIdleSettlementGuard(raw_guard)
    output = output_dir.absolute()
    output.mkdir(parents=True, exist_ok=True)
    registry = AdapterRegistry(
        profile=PROFILE,
        endpoint_measurement_runner=BW20EndpointMeasurementRunner(
            target=target, source_root=source_root, runner=runner
        ),
        resource_cleaner=BW20EndpointCleaner(target, guard, runner),
    )
    return Worker(
        worker_id,
        WorkerType.GPU,
        api_url,
        capabilities={
            "adapter_profile": PROFILE,
            "adapters": ["endpoint_measurement_runner", "resource_cleaner"],
            "resource_id": "bw20-sglang-0.5.12:hcu:7",
            "run_mode": "provisional",
            "clock_mutation_performed": False,
            "automatic_release_allowed": False,
        },
        heartbeat_seconds=10,
        adapters=registry,
        output_dir=output,
        resource_guard=guard,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--target-lock", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    worker = build_worker(
        worker_id=args.worker_id,
        api_url=args.api_url,
        target=load_target(args.target_lock),
        source_root=args.source_root,
        output_dir=args.output_dir,
    )
    return 0 if worker.run_once() else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BW20EndpointCleaner",
    "BW20EndpointIdleSettlementGuard",
    "BW20EndpointMeasurementRunner",
    "build_worker",
    "main",
]
