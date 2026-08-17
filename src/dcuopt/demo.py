from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from dcuopt.domain.enums import WorkerType
from dcuopt.workers.sdk import Worker


def run_walking_demo(
    api_url: str,
    timeout_seconds: float = 60.0,
    embedded_workers: bool = True,
) -> dict[str, Any]:
    base_url = api_url.rstrip("/")
    client = httpx.Client(base_url=base_url, timeout=30)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if client.get("/healthz").is_success:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("control plane did not become healthy")
        time.sleep(0.5)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    task_response = client.post(
        "/v1/tasks",
        json={
            "name": f"walking-skeleton-{run_id}",
            "workload_id": "fixture-rmsnorm-control-flow",
            "idempotency_key": f"walking-skeleton:{run_id}",
            "budget": {"synthetic_jobs": 8},
            "automatic_release_allowed": False,
        },
    )
    task_response.raise_for_status()
    task_id = task_response.json()["task_id"]

    stage0 = client.post(
        f"/v1/tasks/{task_id}/stage0",
        json={
            "measurement": "pass",
            "profiler": "full",
            "hot_patch": "hot_patch",
            "hardware_fingerprint": "fake-dcu-fingerprint",
            "software_fingerprint": "fake-dtk-fingerprint",
            "timer_resolution_ns": 100.0,
            "noise_sigma_ns": 500.0,
            "noise_cv": 0.01,
            "mde_ratio": 0.03,
            "evidence_uri": "fake://stage0/control-flow-only",
        },
    )
    stage0.raise_for_status()
    baseline = client.post(
        f"/v1/tasks/{task_id}/baseline",
        json={
            "hardware_fingerprint": "fake-dcu-fingerprint",
            "software_fingerprint": "fake-dtk-fingerprint",
            "workload_id": "fixture-rmsnorm-control-flow",
            "configuration_hash": "fake-config-sha256",
        },
    )
    baseline.raise_for_status()

    workers: list[Worker] = []
    if embedded_workers:
        workers = [
            Worker(f"demo-agent-{run_id}", WorkerType.AGENT, base_url),
            Worker(f"demo-build-{run_id}", WorkerType.BUILD, base_url),
            Worker(
                f"demo-gpu-{run_id}",
                WorkerType.GPU,
                base_url,
                capabilities={"resource_id": f"fake-dcu-{run_id}"},
            ),
        ]

    while time.monotonic() < deadline:
        for worker in workers:
            worker.run_once()
        summary_response = client.get(f"/v1/tasks/{task_id}/summary")
        summary_response.raise_for_status()
        summary = summary_response.json()
        if summary["task"]["state"] in {"awaiting_signoff", "rejected"}:
            return summary
        time.sleep(0.1 if embedded_workers else 1.0)
    raise TimeoutError(f"walking skeleton did not finish; task_id={task_id}")
