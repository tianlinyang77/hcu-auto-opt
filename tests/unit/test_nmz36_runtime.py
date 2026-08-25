from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.measurement.nmz36_runtime import (
    HySmiTelemetryCollector,
    ManagedProcessRegistry,
    Nmz36Stage0MeasurementProbeAdapter,
    _proc_start_token,
    _run_text_with_retries,
)
from hcuopt.measurement.stage0 import Stage0ProbeOutput
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


def test_hysmi_command_retry_preserves_transient_failure(monkeypatch) -> None:
    results = iter(
        (
            subprocess.CompletedProcess(
                ("hy-smi", "--showpids"), -6, "", "free(): invalid pointer"
            ),
            subprocess.CompletedProcess(("hy-smi", "--showpids"), 0, "PIDs\n", ""),
        )
    )
    monkeypatch.setattr(
        "hcuopt.measurement.nmz36_runtime.subprocess.run",
        lambda *args, **kwargs: next(results),
    )

    output, warnings = _run_text_with_retries(("hy-smi", "--showpids"))

    assert output == "PIDs\n"
    assert len(warnings) == 1
    assert "free(): invalid pointer" in warnings[0]


def test_hysmi_command_retry_fails_closed_after_bound(monkeypatch) -> None:
    monkeypatch.setattr(
        "hcuopt.measurement.nmz36_runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ("hy-smi", "--showpids"), -6, "", "free(): invalid pointer"
        ),
    )

    with pytest.raises(Exception, match="failed after 3 attempts") as captured:
        _run_text_with_retries(("hy-smi", "--showpids"))

    assert "attempt 1/3" in str(captured.value)
    assert "attempt 2/3" in str(captured.value)
    assert "attempt 3/3" in str(captured.value)


def test_nmz36_deployment_adapter_uses_job_fence_and_preserves_raw_producer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    class Timer:
        def synchronize(self) -> None:
            return None

        def close(self) -> None:
            observed["timer_closed"] = True

    class Factory:
        def __init__(self, **kwargs) -> None:
            observed.update(kwargs)

        def timer(self) -> Timer:
            return Timer()

        def workload(self, probe_type, restart_ordinal):  # pragma: no cover
            raise AssertionError((probe_type, restart_ordinal))

    class Harness:
        def __init__(self, **kwargs) -> None:
            observed["harness"] = kwargs

    producer = AdapterProvenance(
        profile="nmz36-stage0-v2",
        capability="stage0_probe",
        adapter_name="RawEvidenceProducer",
        adapter_version="1",
        implementation_kind="real",
    )

    class Delegate:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def run_probe(self, payload, output_dir) -> Stage0ProbeOutput:
            return Stage0ProbeOutput(
                summary={"sample_count": 10},
                raw_evidence_uri=(output_dir / "raw.json").as_uri(),
                raw_evidence_hash="sha256:" + "1" * 64,
                cleanup_evidence={
                    "fence": {"fenced": True},
                    "health": {"healthy": True},
                },
                adapter_provenance=(producer,),
            )

    monkeypatch.setattr("hcuopt.measurement.nmz36_runtime.Nmz36WorkloadFactory", Factory)
    monkeypatch.setattr("hcuopt.measurement.nmz36_runtime.EvidenceMeasurementHarness", Harness)
    monkeypatch.setattr(
        "hcuopt.measurement.nmz36_runtime.Stage0MeasurementProbeAdapter",
        Delegate,
    )
    adapter = Nmz36Stage0MeasurementProbeAdapter(TARGET, tmp_path)

    output = adapter.run_probe(
        {
            "mode": "formal",
            "probe_type": "timer",
            "_job_context": {"resource_id": "hcu-7", "fencing_token": 23},
        },
        tmp_path,
    )

    assert observed["resource_id"] == "hcu-7"
    assert observed["fencing_token"] == 23
    assert observed["timer_closed"] is True
    assert output.adapter_provenance == (producer,)
