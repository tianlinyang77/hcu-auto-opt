# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from hcuopt.deployment.bw20_m1_correctness_worker import (
    BW20M1CorrectnessJobHandler,
    BW20M1IdleGuard,
    BW20M1OutputAccess,
)
from hcuopt.domain.errors import ExecutionSafetyError


class _Registry:
    profile = "bw20-m1-manual-v1"

    @staticmethod
    def require(capability: str) -> object:
        assert capability == "kernel_correctness"
        return object()


def test_correctness_handler_rejects_every_other_job(tmp_path: Path) -> None:
    handler = BW20M1CorrectnessJobHandler(_Registry(), tmp_path)  # type: ignore[arg-type]

    for job_type in ("manual_build", "manual_performance", "stage0_probe", "shell"):
        with pytest.raises(ExecutionSafetyError, match="only manual_correctness"):
            handler.handle(job_type, {})


def test_correctness_cleanup_rejects_cross_job_scope(tmp_path: Path) -> None:
    handler = BW20M1CorrectnessJobHandler(_Registry(), tmp_path)  # type: ignore[arg-type]

    with pytest.raises(ExecutionSafetyError, match="scope"):
        handler.cleanup("manual_performance", {})


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], timeout: float):  # type: ignore[no-untyped-def]
        del timeout
        self.calls.append(argv)
        if argv[0] == "getfacl":
            return type("Result", (), {"returncode": 0, "stdout": b"user:65534:rwx\n"})()
        return type("Result", (), {"returncode": 0, "stdout": b""})()


def _stat(pid: int) -> str:
    return f"{pid} (worker) S " + " ".join(["1"] * 18) + " 123"


def _guard_observation(process_gpu_ids: dict[int, list[str]] | None = None) -> dict:
    process_gpu_ids = process_gpu_ids or {}
    pids = sorted(process_gpu_ids)
    return {
        "schema_version": "bw20-stage0-telemetry-v1",
        "host": "github-bw20",
        "pci": "0000:b1:00.0",
        "boot_id": str(uuid4()),
        "kfd_before": pids,
        "kfd_after": pids,
        "kfd_topology": [
            {"node": 8, "gpu_id": "11155", "drm_render_minor": "128"},
            {"node": 15, "gpu_id": "3004", "drm_render_minor": "135"},
        ],
        "files": {
            "numa_node": "4",
            "gpu_busy_percent": "0",
            "mem_info_vram_used": "2207744",
            "power_dpm_force_performance_level": "auto",
        },
        "smi_device": {
            "stderr": "",
            "stdout": "\n".join(
                "HCU[7]\t: " + line
                for line in (
                    "Temperature (Sensor edge) (C): 40.0",
                    "Temperature (Sensor junction) (C): 44.0",
                    "sclk clock level: 1 (600Mhz)",
                    "mclk clock level: 0 (1800Mhz)",
                    "Performance Level: auto",
                    "Average Graphics Package Power (W): 135.0",
                )
            ),
        },
        "smi_processes": {
            "stderr": "",
            "stdout": "\n".join(
                f"PID: {pid}\n\tHCU Index: \n\tVRAM USED(MiB): 1\n" for pid in pids
            ),
        },
        "processes": [
            {
                "pid": pid,
                "stat_before": _stat(pid),
                "stat_after": _stat(pid),
                "executable": "/usr/bin/python",
                "executable_status": "observed",
                "comm": "python\n",
                "queue_gpu_ids": process_gpu_ids[pid],
            }
            for pid in pids
        ],
    }


class _TelemetryRunner:
    def __init__(self, raw: dict) -> None:
        self.raw = raw

    def run(self, argv: tuple[str, ...], timeout: float):  # type: ignore[no-untyped-def]
        del argv, timeout
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.raw).encode(), stderr=b"")


def test_idle_guard_ignores_proven_processes_on_other_hcus() -> None:
    guard = BW20M1IdleGuard(_TelemetryRunner(_guard_observation({4321: ["11155"]})))

    guard("bw20-sglang-0.5.12:hcu:7")

    assert guard.observations[-1]["global_kfd_process_count"] == 1
    assert guard.observations[-1]["resource_background_processes"] == []
    assert guard.observations[-1]["target_kfd_gpu_id"] == 3004


def test_idle_guard_rejects_process_on_hcu7() -> None:
    guard = BW20M1IdleGuard(_TelemetryRunner(_guard_observation({4321: ["3004"]})))

    with pytest.raises(ExecutionSafetyError, match="not idle"):
        guard("bw20-sglang-0.5.12:hcu:7")

    assert guard.observations[-1]["resource_background_processes"][0]["process_id"] == 4321


@pytest.mark.parametrize("damage", ["mapping", "missing_queue", "unknown_gpu"])
def test_idle_guard_fails_closed_when_device_attribution_is_unproven(damage: str) -> None:
    raw = _guard_observation({4321: ["11155"]})
    if damage == "mapping":
        raw["kfd_topology"][1]["gpu_id"] = "9999"
    elif damage == "missing_queue":
        raw["processes"][0]["queue_gpu_ids"] = []
    else:
        raw["processes"][0]["queue_gpu_ids"] = ["9999"]
    guard = BW20M1IdleGuard(_TelemetryRunner(raw))

    with pytest.raises(ExecutionSafetyError):
        guard("bw20-sglang-0.5.12:hcu:7")


def test_output_access_grants_only_a_directory_below_job_root(tmp_path: Path) -> None:
    root = tmp_path / "job"
    output = root / "variant"
    outside = tmp_path / "outside"
    output.mkdir(parents=True)
    outside.mkdir()
    runner = _Runner()
    access = BW20M1OutputAccess(root, runner)  # type: ignore[arg-type]

    access(output)

    assert runner.calls[0][:4] == ("setfacl", "-m", "u:65534:rwx", "--")
    with pytest.raises(ExecutionSafetyError, match="escaped"):
        access(outside)
