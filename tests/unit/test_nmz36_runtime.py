from __future__ import annotations

from pathlib import Path

from hcuopt.measurement.nmz36_runtime import (
    HySmiTelemetryCollector,
    ManagedProcessRegistry,
    _proc_start_token,
)
from hcuopt.targets import load_target

ROOT = Path(__file__).parents[2]
TARGET = load_target(ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml")


def test_proc_start_token_handles_spaces_and_parentheses_in_comm() -> None:
    fields_3_through_21 = "S " + " ".join(str(value) for value in range(4, 22))
    line = f"321 (stage0 worker (hcu)) {fields_3_through_21} 778899 0 0"

    assert _proc_start_token(line, 321) == "linux-proc-startticks:778899"


def test_hysmi_process_parser_retains_unmanaged_hcu7_process(monkeypatch) -> None:
    registry = ManagedProcessRegistry()
    collector = HySmiTelemetryCollector(TARGET, registry)
    output = """
PIDs for KFD processes:

PID: 3865170
    HCU Index: ['0'] ['1'] ['2'] ['3'] ['4'] ['5'] ['6'] ['7']
    VRAM USED(MiB): 9330
"""
    monkeypatch.setattr("hcuopt.measurement.nmz36_runtime._read_link", lambda _path: "/bin/x")
    monkeypatch.setattr("hcuopt.measurement.nmz36_runtime._read_text", lambda _path: "worker")

    processes = collector._processes(output, 7)

    assert processes == [
        {
            "process_id": 3865170,
            "executable": "/bin/x",
            "command_line": "worker",
            "uses_accelerator": True,
            "device_memory_bytes": 9330 * 1024 * 1024,
            "managed_by_stage0": False,
        }
    ]


def test_hysmi_process_parser_marks_registered_worker_managed(monkeypatch) -> None:
    registry = ManagedProcessRegistry()
    registry.add(42)
    collector = HySmiTelemetryCollector(TARGET, registry)
    output = """
PID: 42
    HCU Index: ['7']
    VRAM USED(MiB): 1
"""
    monkeypatch.setattr("hcuopt.measurement.nmz36_runtime._read_link", lambda _path: "/bin/x")
    monkeypatch.setattr("hcuopt.measurement.nmz36_runtime._read_text", lambda _path: "worker")

    assert collector._processes(output, 7)[0]["managed_by_stage0"] is True
