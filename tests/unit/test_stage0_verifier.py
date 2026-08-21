from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    LeaseScope,
    ProfilerCapability,
    ProjectMode,
    Stage0ProbeType,
    Stage0RunMode,
)
from hcuopt.evaluation.stage0_finalizer import (
    FORMAL_STAGE0_SCOPE_WARNING,
    FileStage0Finalizer,
)
from hcuopt.evaluation.stage0_protocol import (
    LoadedStage0Protocol,
    load_registered_stage0_protocol,
)
from hcuopt.evaluation.stage0_verifier import (
    FingerprintEvidenceV2,
    HotpatchEvidenceV2,
    ProfilerEvidenceV2,
    Stage0EvidenceError,
    Stage0EvidenceReader,
    Stage0ProbeEvidenceReference,
    Stage0VerificationContext,
    Stage0Verifier,
    classify_hotpatch,
    classify_profiler,
    target_fingerprint,
    verification_input_digest,
)
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint
from hcuopt.runtime_probes.adapter import RuntimeProbeAdapter
from hcuopt.runtime_probes.evidence import DeploymentContentAddressedEvidencePublisher
from hcuopt.runtime_probes.overlay import OverlayCapabilityProbe
from hcuopt.runtime_probes.profiler import ProfilerCapabilityProbe
from hcuopt.stage0 import evaluate_stage0
from tests.unit.test_runtime_probe_formal_v2 import (
    _Cleaner as RuntimeCleaner,
)
from tests.unit.test_runtime_probe_formal_v2 import (
    _Clock as RuntimeClock,
)
from tests.unit.test_runtime_probe_formal_v2 import (
    _formal_hotpatch_target_and_profile,
    _FormalOverlayExecutor,
)
from tests.unit.test_runtime_probe_formal_v2 import (
    _Runner as RuntimeRunner,
)
from tests.unit.test_runtime_probe_formal_v2 import (
    _Telemetry as RuntimeTelemetry,
)

requires_posix_reader = pytest.mark.skipif(
    os.name != "posix",
    reason="Formal evidence verification requires POSIX openat/O_NOFOLLOW semantics",
)

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_ROOT = ROOT / "config" / "stage0"
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
TASK_ID = UUID(int=101)
RUN_ID = UUID(int=102)
TARGET_SNAPSHOT_ID = UUID(int=103)
LEASE_ID = UUID(int=104)
RESOURCE_ID = "hcu-7"
FENCING_TOKEN = 17
WORKLOAD_ID = "stage0-short-kernel-v1"
PROFILE = "nmz36-stage0-composite-v1"

PROVENANCE = AdapterProvenance(
    profile=PROFILE,
    capability="stage0_probe",
    adapter_name="CompositeStage0Adapter",
    adapter_version="2",
    implementation_kind="real",
    source_commit="1" * 40,
)
HEALTHY_CLEANUP = {
    "fence": {
        "fenced": True,
        "resource_id": RESOURCE_ID,
        "fencing_token": FENCING_TOKEN,
    },
    "health": {
        "healthy": True,
        "resource_id": RESOURCE_ID,
        "remaining_processes": [],
    },
}


class _PortableVerifierTestReader(Stage0EvidenceReader):
    """Exercise pure verifier logic on Windows without claiming Formal path safety."""

    def _secure_read(self, path: Path) -> bytes:
        encoded = path.read_bytes()
        if len(encoded) > self.max_bytes:
            raise Stage0EvidenceError("evidence_too_large", "test evidence exceeds size limit")
        return encoded


def _verifier_reader(root: Path) -> Stage0EvidenceReader:
    if os.name == "posix":
        return Stage0EvidenceReader(root)
    return _PortableVerifierTestReader(root)


def _load_target() -> TargetSpec:
    return TargetSpec.model_validate(yaml.safe_load(TARGET_PATH.read_text(encoding="utf-8")))


def _hash_bytes(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _telemetry(
    *,
    temperature_c: float = 55.0,
    sclk_mhz: float = 1350.0,
    mclk_mhz: float = 875.0,
    performance_level: str = "manual",
    cache_state: str = "flushed",
    unmanaged_accelerator_process: bool = False,
) -> dict[str, Any]:
    background_processes: list[dict[str, Any]] = []
    if unmanaged_accelerator_process:
        background_processes.append(
            {
                "process_id": 9999,
                "executable": "/usr/bin/noisy-neighbor",
                "command_line": "noisy-neighbor --device 7",
                "uses_accelerator": True,
                "device_memory_bytes": 4096,
                "managed_by_stage0": False,
            }
        )
    return {
        "device": {
            "device_index": 7,
            "temperature_c": temperature_c,
            "hotspot_temperature_c": temperature_c,
            "sclk_mhz": sclk_mhz,
            "mclk_mhz": mclk_mhz,
            "performance_level": performance_level,
            "power_w": 210.0,
        },
        "cache": {
            "state": cache_state,
            "cleared_before_sample": True,
            "identity_hash": None,
        },
        "background_processes": background_processes,
    }


def _binding(
    probe_type: Stage0ProbeType,
    protocol: LoadedStage0Protocol,
    target: TargetSpec,
) -> dict[str, Any]:
    return {
        "task_id": str(TASK_ID),
        "stage0_run_id": str(RUN_ID),
        "target_snapshot_id": str(TARGET_SNAPSHOT_ID),
        "target_id": target.target_id,
        "target_fingerprint": target_fingerprint(target),
        "environment_fingerprint": stable_fingerprint(target.model_dump(mode="json")),
        "workload_id": WORKLOAD_ID,
        "probe_type": probe_type.value,
        "run_mode": Stage0RunMode.FORMAL.value,
        "protocol_version": protocol.protocol.protocol_version,
        "protocol_hash": protocol.protocol_hash,
        "metric_name": "kernel_elapsed",
        "unit": "ns",
        "measurement_id": str(UUID(int=200 + list(Stage0ProbeType).index(probe_type))),
        "lease": {
            "lease_id": str(LEASE_ID),
            "lease_scope": LeaseScope.EXCLUSIVE.value,
            "resource_id": RESOURCE_ID,
            "fencing_token": FENCING_TOKEN,
        },
    }


def _calibration(*, timer_resolution_ns: float, max_residual_ns: float) -> dict[str, Any]:
    interval_ns = 2
    resolution_tick_delta = max(1, int(round(timer_resolution_ns)))
    middle_offset_ns = int(round(max_residual_ns * 1.5))
    midpoints = (10_001, 11_001 + middle_offset_ns, 12_001)
    points = []
    for ordinal, (device_ticks, midpoint) in enumerate(
        zip((1_000, 2_000, 3_000), midpoints, strict=True)
    ):
        started = midpoint - interval_ns // 2
        finished = started + interval_ns
        points.append(
            {
                "point_ordinal": ordinal,
                "device_ticks": device_ticks,
                "host_started_monotonic_ns": started,
                "host_finished_monotonic_ns": finished,
            }
        )
    actual_resolution = float(resolution_tick_delta)
    actual_residual = middle_offset_ns * (2.0 / 3.0) + interval_ns / 2.0
    return {
        "device_name": "hcu-event",
        "device_index": 7,
        "timer_resolution_ns": actual_resolution,
        "ns_per_tick": 1.0,
        "max_residual_ns": actual_residual,
        "points": points,
        "resolution_tick_deltas": [resolution_tick_delta] * 3,
    }


def _samples(
    *,
    segment_order: tuple[str, ...],
    samples_per_segment: int,
    restart_values: tuple[float, ...] | None = None,
    comparison_effect: float = 0.0,
    outlier_count: int = 0,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    acquisition = 0
    for restart in range(10):
        baseline_ns = 1_000.0 if restart_values is None else restart_values[restart]
        for segment in segment_order:
            arm = "single"
            nominal_sample_ns = baseline_ns
            if segment in {"A1", "A2"}:
                arm = "baseline"
            elif segment in {"B1", "B2"}:
                arm = "comparison"
                nominal_sample_ns = baseline_ns * (1.0 + comparison_effect)
            for segment_ordinal in range(samples_per_segment):
                sample_ns = baseline_ns * 2.0 if acquisition < outlier_count else nominal_sample_ns
                started_device_ticks = 1_000_000 + acquisition * 200_000
                elapsed_ticks = int(round(sample_ns * 100))
                started_host_ns = 2_000_000 + acquisition * 200_000
                result.append(
                    {
                        "process_id": 10_000 + restart,
                        "process_start_token": f"linux-proc-startticks:{50_000 + restart}",
                        "restart_ordinal": restart,
                        "arm": arm,
                        "segment": segment,
                        "acquisition_ordinal": acquisition,
                        "segment_sample_ordinal": segment_ordinal,
                        "started_monotonic_ns": started_host_ns,
                        "finished_monotonic_ns": started_host_ns + elapsed_ticks,
                        "started_device_ticks": started_device_ticks,
                        "finished_device_ticks": started_device_ticks + elapsed_ticks,
                        "batch_iterations": 100,
                    }
                )
                acquisition += 1
    return result


def _measurement_evidence(
    probe_type: Stage0ProbeType,
    protocol: LoadedStage0Protocol,
    target: TargetSpec,
    *,
    restart_values: tuple[float, ...] | None = None,
    comparison_effect: float = 0.0,
    timer_resolution_ns: float = 1.0,
    max_residual_ns: float = 0.5,
    before_telemetry: dict[str, Any] | None = None,
    after_telemetry: dict[str, Any] | None = None,
    outlier_count: int = 0,
) -> dict[str, Any]:
    if probe_type is Stage0ProbeType.TIMER:
        segment_order = ("timer",)
        samples_per_segment = 1
    elif probe_type is Stage0ProbeType.NOISE:
        segment_order = ("noise",)
        samples_per_segment = 30
    else:
        segment_order = ("A1", "B1", "B2", "A2")
        samples_per_segment = 10
    samples = _samples(
        segment_order=segment_order,
        samples_per_segment=samples_per_segment,
        restart_values=restart_values,
        comparison_effect=comparison_effect,
        outlier_count=outlier_count,
    )
    observations: list[dict[str, Any]] = [
        {
            "phase": "before_run",
            "captured_monotonic_ns": 1_000_000,
            "restart_ordinal": None,
            "acquisition_ordinal": None,
            "process_id": None,
            "process_start_token": None,
            "process_lifecycle_record": None,
            "telemetry": before_telemetry or _telemetry(),
        }
    ]
    for restart in range(10):
        restart_samples = [item for item in samples if item["restart_ordinal"] == restart]
        process_id = 10_000 + restart
        process_start_token = f"linux-proc-startticks:{50_000 + restart}"
        observations.extend(
            (
                {
                    "phase": "before_restart",
                    "captured_monotonic_ns": restart_samples[0]["started_monotonic_ns"] - 10,
                    "restart_ordinal": restart,
                    "acquisition_ordinal": None,
                    "process_id": process_id,
                    "process_start_token": process_start_token,
                    "process_lifecycle_record": None,
                    "telemetry": before_telemetry or _telemetry(),
                },
                {
                    "phase": "after_restart",
                    "captured_monotonic_ns": restart_samples[-1]["finished_monotonic_ns"] + 10,
                    "restart_ordinal": restart,
                    "acquisition_ordinal": None,
                    "process_id": process_id,
                    "process_start_token": process_start_token,
                    "process_lifecycle_record": None,
                    "telemetry": after_telemetry or _telemetry(),
                },
            )
        )
    observations.append(
        {
            "phase": "after_run",
            "captured_monotonic_ns": 1_000_000_000,
            "restart_ordinal": None,
            "acquisition_ordinal": None,
            "process_id": None,
            "process_start_token": None,
            "process_lifecycle_record": None,
            "telemetry": after_telemetry or _telemetry(),
        }
    )
    observations.sort(key=lambda item: item["captured_monotonic_ns"])
    return {
        "schema_version": "measurement-evidence-v2",
        "binding": _binding(probe_type, protocol, target),
        "plan": {
            "restart_count": 10,
            "warmup_count": 10,
            "batch_iterations": 100,
            "segment_order": list(segment_order),
            "samples_per_segment": samples_per_segment,
        },
        "calibration": _calibration(
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
        ),
        "samples": samples,
        "observations": observations,
        "adapter_provenance": [PROVENANCE.model_dump(mode="json")],
        "synthetic": False,
    }


def _raw_suite(
    protocol: LoadedStage0Protocol,
    target: TargetSpec,
    *,
    noise_restart_values: tuple[float, ...] | None = None,
    known_effect: float = 0.12,
    null_effect: float = 0.0,
    timer_resolution_ns: float = 1.0,
    max_residual_ns: float = 0.5,
    before_telemetry: dict[str, Any] | None = None,
    after_telemetry: dict[str, Any] | None = None,
    noise_outlier_count: int = 0,
) -> dict[Stage0ProbeType, dict[str, Any]]:
    common = {
        "schema_version": "measurement-evidence-v2",
        "observations": [
            {
                "phase": "before_run",
                "captured_monotonic_ns": 1_000_000,
                "restart_ordinal": None,
                "acquisition_ordinal": None,
                "telemetry": before_telemetry or _telemetry(),
            },
            {
                "phase": "after_run",
                "captured_monotonic_ns": 1_000_000_000,
                "restart_ordinal": None,
                "acquisition_ordinal": None,
                "telemetry": after_telemetry or _telemetry(),
            },
        ],
        "adapter_provenance": [PROVENANCE.model_dump(mode="json")],
        "synthetic": False,
    }
    return {
        Stage0ProbeType.FINGERPRINT: {
            **common,
            "binding": _binding(Stage0ProbeType.FINGERPRINT, protocol, target),
            "hardware_fingerprint": SHA_C,
            "software_fingerprint": SHA_D,
        },
        Stage0ProbeType.TIMER: _measurement_evidence(
            Stage0ProbeType.TIMER,
            protocol,
            target,
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
            before_telemetry=before_telemetry,
            after_telemetry=after_telemetry,
        ),
        Stage0ProbeType.NOISE: _measurement_evidence(
            Stage0ProbeType.NOISE,
            protocol,
            target,
            restart_values=noise_restart_values,
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
            before_telemetry=before_telemetry,
            after_telemetry=after_telemetry,
            outlier_count=noise_outlier_count,
        ),
        Stage0ProbeType.KNOWN_SIGNAL: _measurement_evidence(
            Stage0ProbeType.KNOWN_SIGNAL,
            protocol,
            target,
            comparison_effect=known_effect,
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
            before_telemetry=before_telemetry,
            after_telemetry=after_telemetry,
        ),
        Stage0ProbeType.NULL_SIGNAL: _measurement_evidence(
            Stage0ProbeType.NULL_SIGNAL,
            protocol,
            target,
            comparison_effect=null_effect,
            timer_resolution_ns=timer_resolution_ns,
            max_residual_ns=max_residual_ns,
            before_telemetry=before_telemetry,
            after_telemetry=after_telemetry,
        ),
        Stage0ProbeType.PROFILER: {
            **common,
            "binding": _binding(Stage0ProbeType.PROFILER, protocol, target),
            "tool_name": "profile-llm-torch",
            "parser_version": "torch-trace-v1",
        },
        Stage0ProbeType.HOTPATCH: {
            **common,
            "binding": _binding(Stage0ProbeType.HOTPATCH, protocol, target),
            "activation_mode": "runtime_hot_patch",
        },
    }


def _raw_file_reference(path: Path, encoded: bytes) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return {"uri": path.as_uri(), "sha256": _hash_bytes(encoded)}


def _profiler_csv_bytes(
    *,
    include_kernel: bool = True,
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        (
            "Kernel_Name",
            "Start_Timestamp",
            "End_Timestamp",
            "GPU_ID",
            "Queue_ID",
        )
    )
    if include_kernel:
        writer.writerow(("hgemm_128x128", "100", "1000", "7", "1"))
    return buffer.getvalue().encode("utf-8")


def _profiler_trace_bytes(*, include_kernel: bool = True, enriched: bool = True) -> bytes:
    events: list[dict[str, Any]] = []
    if include_kernel:
        args: dict[str, Any] = {}
        if enriched:
            args = {
                "Input Dims": ["1x128x128"],
                "Input type": ["float16"],
                "Concrete Inputs": {"grid": "128x1x1"},
                "Call stack": "model.layers.0.mlp",
                "hip_symbol": "_Z17hgemm_128x128v",
            }
        events.append(
            {
                "name": "hgemm_128x128",
                "cat": "kernel",
                "ph": "X",
                "dur": 0.9,
                "args": args,
            }
        )
    return json.dumps({"traceEvents": events}, separators=(",", ":")).encode()


def _proc_stat_line(process_id: int, start_ticks: int) -> str:
    fields_4_through_21 = [str(value) for value in range(4, 22)]
    return f"{process_id} (stage0-fixture) S {' '.join(fields_4_through_21)} {start_ticks}"


def _lifecycle_record(
    root: Path,
    *,
    name: str,
    event: str,
    ordinal: int,
    process_id: int,
    start_ticks: int,
    captured_monotonic_ns: int,
) -> dict[str, str]:
    payload = {
        "schema_version": "process-lifecycle-v1",
        "event": event,
        "restart_ordinal": ordinal,
        "observer_process_id": 4242,
        "process_id": process_id,
        "proc_stat_line": _proc_stat_line(process_id, start_ticks),
        "captured_monotonic_ns": captured_monotonic_ns,
        "waitpid_result_pid": process_id if event == "reaped" else None,
        "wait_status": 0 if event == "reaped" else None,
    }
    return _raw_file_reference(root / name, canonical_json_bytes(payload))


def _phase_execution(
    *,
    target: TargetSpec,
    request_id: UUID,
    container_id: str,
    mounts: list[dict[str, Any]],
    stdout_uri: str,
    environment: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = {
        "request_id": str(request_id),
        "target_id": target.target_id,
        "argv": ["python", "stage0-hotpatch.py"],
        "working_directory": "/workspace",
        "environment": environment,
        "timeout_seconds": 600,
        "lease_scope": "exclusive",
        "resource_id": RESOURCE_ID,
        "fencing_token": FENCING_TOKEN,
        "container_image": target.inference_image.immutable_reference,
        "mounts": mounts,
    }
    result = {
        "request_id": str(request_id),
        "status": "succeeded",
        "exit_code": 0,
        "started_at": "2026-08-20T00:00:00Z",
        "finished_at": "2026-08-20T00:01:00Z",
        "stdout_uri": stdout_uri,
        "stderr_uri": "file:///evidence/stderr.log",
        "metadata": {
            "target_id": target.target_id,
            "resource_id": RESOURCE_ID,
            "fencing_token": FENCING_TOKEN,
            "container_image": target.inference_image.immutable_reference,
            "container_name": container_id,
        },
        "adapter_provenance": PROVENANCE.model_dump(mode="json"),
        "synthetic": False,
    }
    return request, result


def _write_capability_auxiliary_evidence(
    evidence_root: Path,
    raw: dict[Stage0ProbeType, dict[str, Any]],
) -> None:
    for probe_type in (
        Stage0ProbeType.TIMER,
        Stage0ProbeType.NOISE,
        Stage0ProbeType.KNOWN_SIGNAL,
        Stage0ProbeType.NULL_SIGNAL,
    ):
        probe_root = evidence_root / probe_type.value
        for observation in raw[probe_type]["observations"]:
            if observation["phase"] not in {"before_restart", "after_restart"}:
                continue
            restart = observation["restart_ordinal"]
            event = "started" if observation["phase"] == "before_restart" else "reaped"
            observation["process_lifecycle_record"] = _lifecycle_record(
                probe_root,
                name=f"lifecycle-{restart}-{event}.json",
                event=event,
                ordinal=restart,
                process_id=observation["process_id"],
                start_ticks=50_000 + restart,
                captured_monotonic_ns=observation["captured_monotonic_ns"],
            )

    profiler_root = evidence_root / Stage0ProbeType.PROFILER.value
    raw[Stage0ProbeType.PROFILER]["tool_version_output"] = _raw_file_reference(
        profiler_root / "profile-llm-version.txt", b"profile-llm-torch 1.0\n"
    )
    raw[Stage0ProbeType.PROFILER]["raw_output"] = _raw_file_reference(
        profiler_root / "torch-trace.json",
        _profiler_trace_bytes(),
    )

    hotpatch_root = evidence_root / Stage0ProbeType.HOTPATCH.value
    target = _load_target()
    baseline_snapshot_id = UUID(int=501)
    candidate_snapshot_id = UUID(int=502)
    baseline_source = {
        "snapshot_id": str(baseline_snapshot_id),
        "kind": "baseline",
        "repository": target.source_baseline.repository,
        "commit": target.source_baseline.commit,
        "tree_hash": "a" * 40,
        "source_hash": SHA_A,
        "worktree_uri": f"file://{target.source_baseline.clean_checkout}",
        "clean": True,
        "parent_snapshot_id": None,
        "created_at": "2026-08-20T00:00:00Z",
    }
    candidate_source = {
        **baseline_source,
        "snapshot_id": str(candidate_snapshot_id),
        "kind": "candidate",
        "tree_hash": "b" * 40,
        "source_hash": SHA_B,
        "worktree_uri": "file:///home/github/sglang-candidate",
        "parent_snapshot_id": str(baseline_snapshot_id),
    }
    artifact = _raw_file_reference(hotpatch_root / "candidate.py", b"VALUE = 'candidate'\n")
    artifact_manifest = {
        "artifact_id": str(UUID(int=503)),
        "candidate_id": str(UUID(int=504)),
        "kind": "python_overlay",
        "uri": artifact["uri"],
        "content_hash": artifact["sha256"],
        "source_snapshot_id": str(candidate_snapshot_id),
        "build_recipe": {"builder": "stage0-fixture"},
        "metadata": {},
        "sbom_uri": None,
        "signature_uri": None,
        "synthetic": False,
        "created_at": "2026-08-20T00:00:00Z",
    }
    baseline_entry = _raw_file_reference(
        hotpatch_root / "state" / "baseline-kernel.py", b"VALUE = 'baseline'\n"
    )
    candidate_entry = _raw_file_reference(
        hotpatch_root / "state" / "candidate-kernel.py", b"VALUE = 'candidate'\n"
    )
    normalized_output = _raw_file_reference(
        hotpatch_root / "normalized-output.json",
        canonical_json_bytes({"tokens": [1, 2, 3], "text": "fixture"}),
    )
    baseline_cache = _raw_file_reference(
        hotpatch_root / "cache-baseline.json",
        canonical_json_bytes({"namespace": "baseline"}),
    )
    candidate_cache = _raw_file_reference(
        hotpatch_root / "cache-candidate.json",
        canonical_json_bytes({"namespace": "candidate"}),
    )
    request_id = UUID(int=505)
    container_id = "stage0-runtime-hotpatch"
    process_id = 20_001
    process_ticks = 70_000
    runtime_start_record = _lifecycle_record(
        hotpatch_root,
        name="runtime-process-start.json",
        event="started",
        ordinal=0,
        process_id=process_id,
        start_ticks=process_ticks,
        captured_monotonic_ns=1_000,
    )
    runtime_exit_record = _lifecycle_record(
        hotpatch_root,
        name="runtime-process-exit.json",
        event="reaped",
        ordinal=0,
        process_id=process_id,
        start_ticks=process_ticks,
        captured_monotonic_ns=1_350,
    )
    phase_references: dict[str, dict[str, str]] = {}
    for phase in ("baseline", "candidate", "recovery"):
        is_candidate = phase == "candidate"
        implementation_hash = artifact["sha256"] if is_candidate else baseline_entry["sha256"]
        phase_output = _raw_file_reference(
            hotpatch_root / f"execution-output-{phase}.json",
            canonical_json_bytes(
                {
                    "protocol_version": "hcuopt-overlay-result-v2",
                    "activation_marker": "candidate-v1" if is_candidate else "baseline",
                    "output_hash": normalized_output["sha256"],
                    "workload_kind": "sglang_python_triton",
                    "replacement_point": "/opt/hcuopt/kernel.py",
                    "implementation_hash": implementation_hash,
                    "process_id": process_id,
                    "loaded_artifact_hash": artifact["sha256"] if is_candidate else None,
                }
            ),
        )
        request, result = _phase_execution(
            target=target,
            request_id=request_id,
            container_id=container_id,
            mounts=[],
            stdout_uri=phase_output["uri"],
            environment={
                "HCUOPT_CANDIDATE_CACHE_DIR": (
                    "/tmp/hcuopt-cache-candidate" if is_candidate else "/tmp/hcuopt-cache-baseline"
                )
            },
        )
        manifest = {
            "schema_version": "hotpatch-phase-v2",
            "phase": phase,
            "source_snapshot_id": str(
                candidate_snapshot_id if is_candidate else baseline_snapshot_id
            ),
            "execution_request": request,
            "execution_result": result,
            "process_id": process_id,
            "process_start_token": f"linux-proc-startticks:{process_ticks}",
            "process_start_record": runtime_start_record,
            "process_exit_record": runtime_exit_record,
            "container_id": container_id,
            "entries": [
                {
                    "path": "python/kernel.py",
                    "content": candidate_entry if is_candidate else baseline_entry,
                }
            ],
            "output": phase_output,
            "normalized_output": normalized_output,
            "cache_namespace": candidate_cache if is_candidate else baseline_cache,
        }
        phase_references[phase] = _raw_file_reference(
            hotpatch_root / f"state-{phase}.json", canonical_json_bytes(manifest)
        )
    raw[Stage0ProbeType.HOTPATCH].update(
        {
            "baseline_source": _raw_file_reference(
                hotpatch_root / "source-baseline.json", canonical_json_bytes(baseline_source)
            ),
            "candidate_source": _raw_file_reference(
                hotpatch_root / "source-candidate.json", canonical_json_bytes(candidate_source)
            ),
            "artifact_manifest": _raw_file_reference(
                hotpatch_root / "artifact-manifest.json",
                canonical_json_bytes(artifact_manifest),
            ),
            "artifact": artifact,
            "baseline_state": phase_references["baseline"],
            "candidate_state": phase_references["candidate"],
            "recovery_state": phase_references["recovery"],
        }
    )


def _reference_path(reference: dict[str, str]) -> Path:
    raw_path = unquote(urlparse(reference["uri"]).path)
    if os.name == "nt" and len(raw_path) > 2 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return Path(raw_path)


def _startup_overlay_raw(suite: _Suite) -> dict[str, Any]:
    raw = deepcopy(suite.raw[Stage0ProbeType.HOTPATCH])
    artifact_source = unquote(urlparse(raw["artifact"]["uri"]).path)
    candidate_container = "stage0-overlay-candidate"
    for ordinal, phase in enumerate(("baseline", "candidate", "recovery")):
        field_name = f"{phase}_state"
        state = json.loads(_reference_path(raw[field_name]).read_text(encoding="utf-8"))
        process_id = 30_001 + ordinal
        start_ticks = 80_001 + ordinal
        container_id = candidate_container if phase == "candidate" else f"stage0-overlay-{phase}"
        request_id = UUID(int=601 + ordinal)
        state["process_id"] = process_id
        state["process_start_token"] = f"linux-proc-startticks:{start_ticks}"
        state["process_start_record"] = _lifecycle_record(
            suite.root / Stage0ProbeType.HOTPATCH.value,
            name=f"overlay-{phase}-process-start.json",
            event="started",
            ordinal=ordinal,
            process_id=process_id,
            start_ticks=start_ticks,
            captured_monotonic_ns=2_000 + ordinal * 100,
        )
        state["process_exit_record"] = _lifecycle_record(
            suite.root / Stage0ProbeType.HOTPATCH.value,
            name=f"overlay-{phase}-process-exit.json",
            event="reaped",
            ordinal=ordinal,
            process_id=process_id,
            start_ticks=start_ticks,
            captured_monotonic_ns=2_050 + ordinal * 100,
        )
        state["container_id"] = container_id
        output = json.loads(_reference_path(state["output"]).read_text(encoding="utf-8"))
        output["process_id"] = process_id
        state["output"] = _raw_file_reference(
            suite.root / Stage0ProbeType.HOTPATCH.value / f"overlay-execution-output-{phase}.json",
            canonical_json_bytes(output),
        )
        state["execution_request"]["request_id"] = str(request_id)
        state["execution_request"]["mounts"] = (
            [
                {
                    "source": artifact_source,
                    "target": "/opt/hcuopt/kernel.py",
                    "read_only": True,
                }
            ]
            if phase == "candidate"
            else []
        )
        state["execution_result"]["request_id"] = str(request_id)
        state["execution_result"]["stdout_uri"] = state["output"]["uri"]
        state["execution_result"]["metadata"]["container_name"] = container_id
        raw[field_name] = _raw_file_reference(
            suite.root / Stage0ProbeType.HOTPATCH.value / f"overlay-state-{phase}.json",
            canonical_json_bytes(state),
        )
    raw["activation_mode"] = "startup_overlay"
    raw["overlay_mount"] = {
        "source_uri": raw["artifact"]["uri"],
        "source_hash": raw["artifact"]["sha256"],
        "target_path": "/opt/hcuopt/kernel.py",
        "read_only": True,
        "container_id": candidate_container,
    }
    return raw


@dataclass
class _Suite:
    root: Path
    protocol: LoadedStage0Protocol
    context: Stage0VerificationContext
    raw: dict[Stage0ProbeType, dict[str, Any]]
    paths: dict[Stage0ProbeType, Path]
    references: dict[Stage0ProbeType, Stage0ProbeEvidenceReference]

    def rewrite(self, probe_type: Stage0ProbeType) -> None:
        # Production evidence is write-once. These negative tests intentionally
        # replace a temporary fixture before verification to model a producer
        # that published malformed or incorrectly bound raw evidence.
        self.paths[probe_type].chmod(stat.S_IREAD | stat.S_IWRITE)
        self.paths[probe_type].unlink()
        artifact = write_evidence(self.paths[probe_type], self.raw[probe_type])
        self.references[probe_type] = self.references[probe_type].model_copy(
            update={"raw_evidence_hash": artifact.sha256}
        )

    def verify(self):
        return Stage0Verifier(self.protocol, _verifier_reader(self.root)).verify(
            self.context,
            tuple(self.references.values()),
        )


def _build_suite(
    tmp_path: Path,
    *,
    target: TargetSpec | None = None,
    **raw_options: Any,
) -> _Suite:
    protocol = load_registered_stage0_protocol("s0-g0-v1", config_root=PROTOCOL_ROOT)
    target = target or _load_target()
    context = Stage0VerificationContext(
        task_id=TASK_ID,
        stage0_run_id=RUN_ID,
        target_snapshot_id=TARGET_SNAPSHOT_ID,
        target=target,
        target_fingerprint=target_fingerprint(target),
        workload_id=WORKLOAD_ID,
        adapter_profile=PROFILE,
        expected_resource_id=RESOURCE_ID,
    )
    evidence_root = tmp_path / "results" / "stage0" / str(RUN_ID)
    evidence_root.mkdir(parents=True)
    raw = _raw_suite(protocol, target, **raw_options)
    _write_capability_auxiliary_evidence(evidence_root, raw)
    paths: dict[Stage0ProbeType, Path] = {}
    references: dict[Stage0ProbeType, Stage0ProbeEvidenceReference] = {}
    for probe_type in Stage0ProbeType:
        path = evidence_root / probe_type.value / "raw.json"
        artifact = write_evidence(path, raw[probe_type])
        paths[probe_type] = path
        references[probe_type] = Stage0ProbeEvidenceReference(
            probe_record_id=UUID(int=300 + list(Stage0ProbeType).index(probe_type)),
            probe_type=probe_type,
            raw_evidence_uri=artifact.uri,
            raw_evidence_hash=artifact.sha256,
            adapter_provenance=(PROVENANCE,),
            lease_id=LEASE_ID,
            resource_id=RESOURCE_ID,
            fencing_token=FENCING_TOKEN,
            cleanup_evidence=deepcopy(HEALTHY_CLEANUP),
        )
    return _Suite(
        root=evidence_root,
        protocol=protocol,
        context=context,
        raw=raw,
        paths=paths,
        references=references,
    )


def _parse_profiler(raw: dict[str, Any]) -> ProfilerEvidenceV2:
    return ProfilerEvidenceV2.model_validate_json(canonical_json_bytes(raw))


def _parse_hotpatch(raw: dict[str, Any]) -> HotpatchEvidenceV2:
    return HotpatchEvidenceV2.model_validate_json(canonical_json_bytes(raw))


@requires_posix_reader
def test_safe_reader_accepts_only_canonical_content_with_matching_hash(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    path = allowed / "raw.json"
    encoded = canonical_json_bytes({"probe": "noise", "value": 1})
    path.write_bytes(encoded)
    reader = Stage0EvidenceReader(allowed)

    assert reader.read(path.as_uri(), _hash_bytes(encoded)) == {"probe": "noise", "value": 1}

    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(path.as_uri(), SHA_A)
    assert caught.value.code == "evidence_hash_mismatch"


@requires_posix_reader
def test_safe_reader_rejects_path_escape_noncanonical_and_oversized_files(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.json"
    outside_bytes = canonical_json_bytes({"outside": True})
    outside.write_bytes(outside_bytes)
    reader = Stage0EvidenceReader(allowed)

    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(outside.as_uri(), _hash_bytes(outside_bytes))
    assert caught.value.code == "evidence_path_escape"

    noncanonical = allowed / "pretty.json"
    pretty_bytes = b'{\n  "value": 1\n}\n'
    noncanonical.write_bytes(pretty_bytes)
    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(noncanonical.as_uri(), _hash_bytes(pretty_bytes))
    assert caught.value.code == "evidence_noncanonical"

    oversized = allowed / "oversized.json"
    oversized_bytes = canonical_json_bytes({"value": "too-large"})
    oversized.write_bytes(oversized_bytes)
    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0EvidenceReader(allowed, max_bytes=8).read(
            oversized.as_uri(), _hash_bytes(oversized_bytes)
        )
    assert caught.value.code == "evidence_too_large"


@requires_posix_reader
def test_safe_reader_rejects_symbolic_links_even_when_target_stays_inside_root(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = allowed / "target.json"
    encoded = canonical_json_bytes({"value": 1})
    target.write_bytes(encoded)
    link = allowed / "linked.json"
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - depends on Windows developer-mode policy.
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0EvidenceReader(allowed).read(link.as_uri(), _hash_bytes(encoded))
    assert caught.value.code == "evidence_symlink"


@requires_posix_reader
def test_safe_reader_rejects_non_regular_files_and_non_finite_json(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    directory = allowed / "directory"
    directory.mkdir()
    reader = Stage0EvidenceReader(allowed)

    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(directory.as_uri(), SHA_A)
    assert caught.value.code == "evidence_not_regular"

    invalid = allowed / "non-finite.json"
    encoded = b'{"value":NaN}\n'
    invalid.write_bytes(encoded)
    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(invalid.as_uri(), _hash_bytes(encoded))
    assert caught.value.code == "evidence_invalid_json"

    nested: dict[str, Any] = {"leaf": True}
    for _ in range(70):
        nested = {"nested": nested}
    deeply_nested = allowed / "deep.json"
    deep_bytes = canonical_json_bytes(nested)
    deeply_nested.write_bytes(deep_bytes)
    with pytest.raises(Stage0EvidenceError) as caught:
        reader.read(deeply_nested.as_uri(), _hash_bytes(deep_bytes))
    assert caught.value.code == "evidence_invalid_json"


@requires_posix_reader
def test_safe_reader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    fifo = allowed / "raw.fifo"
    os.mkfifo(fifo)

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0EvidenceReader(allowed).read(fifo.as_uri(), SHA_A)

    assert caught.value.code == "evidence_not_regular"


@pytest.mark.skipif(os.name == "posix", reason="non-POSIX fail-closed behavior")
def test_formal_reader_fails_closed_without_posix_nofollow(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    path = allowed / "raw.json"
    encoded = canonical_json_bytes({"value": 1})
    path.write_bytes(encoded)

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0EvidenceReader(allowed).read(path.as_uri(), _hash_bytes(encoded))

    assert caught.value.code == "evidence_platform_unsupported"


def test_seven_probe_canonical_evidence_is_recomputed_to_full_pass(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)

    result = suite.verify()

    assert result.measurement is GateResult.PASS
    assert result.profiler is ProfilerCapability.FULL
    assert result.hot_patch is HotPatchCapability.HOT_PATCH
    assert result.failure_codes == ()
    assert result.noise_cv == 0.0
    assert result.mde_ratio == 0.0
    assert result.statistics["known_signal"]["effect_ratio"] == pytest.approx(0.12)
    assert result.statistics["null_signal"]["effect_ratio"] == 0.0
    assert sum(item["kind"] == "probe_envelope" for item in result.input_evidence) == 7
    assert any(item["kind"] == "profiler_raw_output" for item in result.input_evidence)
    assert any(item["kind"] == "process_lifecycle_record" for item in result.input_evidence)
    assert any(item["kind"] == "artifact" for item in result.input_evidence)
    assert result.verifier_provenance.implementation_kind == "real"


def test_formal_verifier_rejects_modified_thresholds_with_a_registered_name(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path / "suite")
    custom_root = tmp_path / "custom-protocol"
    custom_root.mkdir()
    raw = yaml.safe_load((PROTOCOL_ROOT / "s0-g0-v1.yaml").read_text(encoding="utf-8"))
    raw["measurement_gates"]["max_cv_ratio"] = 0.50
    (custom_root / "s0-g0-v1.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    modified = load_registered_stage0_protocol("s0-g0-v1", config_root=custom_root)

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0Verifier(modified, Stage0EvidenceReader(suite.root))

    assert caught.value.code == "protocol_not_registered"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("timer_resolution_ns", 999.0),
        ("ns_per_tick", 2.0),
        ("max_residual_ns", 999.0),
    ],
)
def test_claimed_calibration_summary_cannot_override_raw_points(
    tmp_path: Path,
    field: str,
    replacement: float,
) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NOISE]["calibration"][field] = replacement
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "calibration_summary_mismatch"


def test_input_digest_reference_surface_excludes_untrusted_database_summary(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    original_records = [
        {
            **reference.model_dump(mode="python"),
            "summary": {"cv": 0.001, "known_signal_detected": True},
        }
        for reference in suite.references.values()
    ]
    tampered_records = deepcopy(original_records)
    for record in tampered_records:
        record["summary"] = {
            "cv": 999.0,
            "mde": 999.0,
            "known_signal_detected": False,
            "capability": "none",
        }

    assert "summary" not in Stage0ProbeEvidenceReference.model_fields
    with pytest.raises(ValidationError, match="summary"):
        Stage0ProbeEvidenceReference.model_validate(original_records[0])

    def project(records: list[dict[str, Any]]):
        fields = Stage0ProbeEvidenceReference.model_fields
        return {
            record["probe_type"]: Stage0ProbeEvidenceReference.model_validate(
                {name: value for name, value in record.items() if name in fields}
            )
            for record in records
        }

    before = verification_input_digest(suite.context, project(original_records), suite.protocol)
    after = verification_input_digest(suite.context, project(tampered_records), suite.protocol)
    assert before == after


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("task_id", str(UUID(int=999))),
        ("target_fingerprint", SHA_A),
        ("workload_id", "different-workload"),
        ("protocol_hash", SHA_A),
    ],
)
def test_raw_identity_binding_mismatch_is_rejected(
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NOISE]["binding"][field] = replacement
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "evidence_binding_mismatch"


def test_shared_environment_hash_must_still_match_the_target_snapshot(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    for probe_type in Stage0ProbeType:
        suite.raw[probe_type]["binding"]["environment_fingerprint"] = SHA_A
        suite.rewrite(probe_type)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "environment_fingerprint_mismatch"


def test_restart_process_token_must_match_lifecycle_evidence(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    noise = suite.raw[Stage0ProbeType.NOISE]
    for sample in noise["samples"]:
        if sample["restart_ordinal"] == 0:
            sample["process_start_token"] = "linux-proc-startticks:99999"
    for observation in noise["observations"]:
        if observation["restart_ordinal"] == 0:
            observation["process_start_token"] = "linux-proc-startticks:99999"
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "process_lifecycle_invalid"


def test_each_restart_requires_confirmed_process_exit(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    noise = suite.raw[Stage0ProbeType.NOISE]
    after_restart = next(
        observation
        for observation in noise["observations"]
        if observation["phase"] == "after_restart" and observation["restart_ordinal"] == 0
    )
    record_reference = after_restart["process_lifecycle_record"]
    record = json.loads(_reference_path(record_reference).read_text(encoding="utf-8"))
    record["wait_status"] = 0x7F
    after_restart["process_lifecycle_record"] = _raw_file_reference(
        suite.root / Stage0ProbeType.NOISE.value / "lifecycle-stopped.json",
        canonical_json_bytes(record),
    )
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "process_lifecycle_invalid"


def test_context_target_mutation_is_detected_before_verification(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.context.target.execution_host.accelerator.expected_sclk_mhz = 999

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "target_snapshot_mutated"

    with pytest.raises(Stage0EvidenceError) as digest_error:
        verification_input_digest(suite.context, suite.references, suite.protocol)
    assert digest_error.value.code == "target_snapshot_mutated"


def test_mutated_cleanup_reference_is_revalidated_before_use(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    reference = suite.references[Stage0ProbeType.NOISE]
    reference.cleanup_evidence["health"]["healthy"] = False

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "reference_schema_invalid"


def test_huge_raw_integer_is_classified_as_invalid_evidence(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NOISE]["calibration"]["points"][2]["device_ticks"] = 10**1000
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "evidence_schema_invalid"


def test_all_timing_probes_cannot_relabel_the_registered_metric(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    for probe_type in (
        Stage0ProbeType.TIMER,
        Stage0ProbeType.NOISE,
        Stage0ProbeType.KNOWN_SIGNAL,
        Stage0ProbeType.NULL_SIGNAL,
    ):
        suite.raw[probe_type]["binding"]["metric_name"] = "throughput"
        suite.raw[probe_type]["binding"]["unit"] = "us"
        suite.rewrite(probe_type)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "metric_binding_mismatch"


@pytest.mark.parametrize(
    ("lease_field", "replacement"),
    [
        ("lease_id", str(UUID(int=999))),
        ("resource_id", "hcu-6"),
        ("fencing_token", 18),
    ],
)
def test_raw_lease_and_fencing_binding_mismatch_is_rejected(
    tmp_path: Path,
    lease_field: str,
    replacement: Any,
) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.TIMER]["binding"]["lease"][lease_field] = replacement
    suite.rewrite(Stage0ProbeType.TIMER)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "evidence_binding_mismatch"


def test_telemetry_and_clock_calibration_must_match_target_device(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    noise = suite.raw[Stage0ProbeType.NOISE]
    noise["calibration"]["device_index"] = 0
    for observation in noise["observations"]:
        observation["telemetry"]["device"]["device_index"] = 0
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "evidence_device_mismatch"


def test_seven_probe_barrier_cannot_mix_accelerator_resources(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    reference = suite.references[Stage0ProbeType.NOISE]
    payload = reference.model_dump(mode="python")
    payload["resource_id"] = "hcu-6"
    payload["cleanup_evidence"] = {
        "fence": {"fenced": True, "resource_id": "hcu-6", "fencing_token": FENCING_TOKEN},
        "health": {"healthy": True, "resource_id": "hcu-6", "remaining_processes": []},
    }
    suite.references[Stage0ProbeType.NOISE] = Stage0ProbeEvidenceReference.model_validate(payload)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "resource_binding_mismatch"


def test_shared_formal_lease_is_schema_invalid(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NOISE]["binding"]["lease"]["lease_scope"] = "shared"
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "evidence_schema_invalid"


def test_each_probe_requires_a_distinct_measurement_id(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    reused = suite.raw[Stage0ProbeType.TIMER]["binding"]["measurement_id"]
    suite.raw[Stage0ProbeType.NOISE]["binding"]["measurement_id"] = reused
    suite.rewrite(Stage0ProbeType.NOISE)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "measurement_id_reused"


def test_all_timing_probes_require_one_metric_and_unit_binding(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.NULL_SIGNAL]["binding"]["unit"] = "us"
    suite.rewrite(Stage0ProbeType.NULL_SIGNAL)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "metric_binding_mismatch"


def test_raw_and_recorded_provenance_must_match(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    suite.raw[Stage0ProbeType.PROFILER]["adapter_provenance"][0]["adapter_name"] = (
        "DifferentRealAdapter"
    )
    suite.rewrite(Stage0ProbeType.PROFILER)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()
    assert caught.value.code == "provenance_mismatch"


@pytest.mark.parametrize(
    "invalid_values",
    [
        {"adapter_provenance": (PROVENANCE.model_copy(update={"implementation_kind": "fake"}),)},
        {
            "cleanup_evidence": {
                "fence": {"fenced": True},
                "health": {"healthy": False},
            }
        },
    ],
)
def test_reference_rejects_fake_provenance_or_unhealthy_cleanup(
    tmp_path: Path,
    invalid_values: dict[str, Any],
) -> None:
    suite = _build_suite(tmp_path)
    payload = suite.references[Stage0ProbeType.NOISE].model_dump(mode="python")
    payload.update(invalid_values)

    with pytest.raises(ValidationError):
        Stage0ProbeEvidenceReference.model_validate(payload)


def test_cleanup_evidence_is_bound_to_reference_resource_and_fencing_token(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    payload = suite.references[Stage0ProbeType.NOISE].model_dump(mode="python")
    payload["cleanup_evidence"] = {
        "fence": {
            "fenced": True,
            "resource_id": "hcu-6",
            "fencing_token": FENCING_TOKEN + 1,
        },
        "health": {"healthy": True, "resource_id": "hcu-6"},
    }

    with pytest.raises(ValidationError, match="cleanup"):
        Stage0ProbeEvidenceReference.model_validate(payload)


@pytest.mark.parametrize(
    ("case", "options", "expected_codes"),
    [
        (
            "noise",
            {"noise_restart_values": (900.0, 1100.0) * 5},
            {"noise_cv_exceeded", "noise_mde_exceeded"},
        ),
        (
            "known",
            {"known_effect": 0.02},
            {"known_signal_not_detected"},
        ),
        (
            "null",
            {"null_effect": 0.04},
            {"null_signal_not_equivalent"},
        ),
        (
            "timer_environment",
            {
                "timer_resolution_ns": 20.0,
                "max_residual_ns": 20.0,
                "before_telemetry": _telemetry(
                    temperature_c=70.0,
                    sclk_mhz=1200.0,
                    mclk_mhz=800.0,
                    performance_level="auto",
                    cache_state="unknown",
                    unmanaged_accelerator_process=True,
                ),
                "after_telemetry": _telemetry(
                    temperature_c=80.0,
                    sclk_mhz=1200.0,
                    mclk_mhz=800.0,
                    performance_level="auto",
                    cache_state="unknown",
                    unmanaged_accelerator_process=True,
                ),
            },
            {
                "timer_timer_resolution_exceeded",
                "timer_calibration_residual_exceeded",
                "temperature_drift_exceeded",
                "sclk_drift_exceeded",
                "mclk_drift_exceeded",
                "performance_level_mismatch",
                "cache_state_unknown",
                "background_accelerator_process",
            },
        ),
    ],
)
def test_four_measurement_gate_classes_fail_from_recomputed_raw_values(
    tmp_path: Path,
    case: str,
    options: dict[str, Any],
    expected_codes: set[str],
) -> None:
    suite = _build_suite(tmp_path / case, **options)

    result = suite.verify()

    assert result.measurement is GateResult.FAIL
    assert expected_codes.issubset(result.failure_codes)


def test_hampel_outliers_are_retained_but_excess_ratio_fails_gate(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path, noise_outlier_count=20)

    result = suite.verify()

    assert result.measurement is GateResult.FAIL
    assert "noise_outlier_ratio_exceeded" in result.failure_codes
    assert result.statistics["noise"]["sample_count"] == 300
    assert result.statistics["noise"]["outlier_count"] == 20


def test_cache_must_be_cleared_and_consistent_across_probes(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    noise = suite.raw[Stage0ProbeType.NOISE]
    noise["observations"][0]["telemetry"]["cache"]["cleared_before_sample"] = False
    noise["observations"][0]["telemetry"]["cache"]["identity_hash"] = SHA_A
    suite.rewrite(Stage0ProbeType.NOISE)

    result = suite.verify()

    assert result.measurement is GateResult.FAIL
    assert "cache_not_cleared" in result.failure_codes
    assert "cache_policy_mismatch" in result.failure_codes


def test_profiler_classification_covers_full_degraded_and_none(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = suite.raw[Stage0ProbeType.PROFILER]
    reader = _verifier_reader(suite.root)

    full = _parse_profiler(raw)
    degraded_raw = deepcopy(raw)
    degraded_raw["raw_output"] = _raw_file_reference(
        suite.root / Stage0ProbeType.PROFILER.value / "torch-degraded.json",
        _profiler_trace_bytes(enriched=False),
    )
    degraded = _parse_profiler(degraded_raw)
    none_raw = deepcopy(raw)
    none_raw["raw_output"] = _raw_file_reference(
        suite.root / Stage0ProbeType.PROFILER.value / "torch-empty.json",
        _profiler_trace_bytes(include_kernel=False),
    )
    none = _parse_profiler(none_raw)

    assert classify_profiler(full, reader) is ProfilerCapability.FULL
    assert classify_profiler(degraded, reader) is ProfilerCapability.DEGRADED
    assert classify_profiler(none, reader) is ProfilerCapability.NONE


def test_profiler_capability_rejects_unparseable_raw_output(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    profiler = suite.raw[Stage0ProbeType.PROFILER]
    profiler["raw_output"] = _raw_file_reference(
        suite.root / Stage0ProbeType.PROFILER.value / "rocprof-invalid.csv",
        b"producer-says-parser-succeeded\n",
    )
    suite.rewrite(Stage0ProbeType.PROFILER)

    with pytest.raises(Stage0EvidenceError) as caught:
        suite.verify()

    assert caught.value.code == "profiler_raw_invalid"


def test_profiler_rejects_producer_normalized_csv_as_raw_rocprof(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.PROFILER])
    raw["tool_name"] = "rocprof"
    raw["parser_version"] = "rocprof-csv-v1"
    raw["raw_output"] = _raw_file_reference(
        suite.root / Stage0ProbeType.PROFILER.value / "producer-normalized.csv",
        (
            b"kernel_name,duration_ns,call_count,shapes,dtypes,metadata_json,"
            b"python_source,hip_symbol\n"
            b"forged,1,1,1x1,float16,{},fake.py,fake_symbol\n"
        ),
    )

    with pytest.raises(Stage0EvidenceError) as caught:
        classify_profiler(_parse_profiler(raw), _verifier_reader(suite.root))

    assert caught.value.code == "profiler_raw_invalid"


def test_hotpatch_classification_covers_runtime_overlay_and_unsafe_none(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    raw = suite.raw[Stage0ProbeType.HOTPATCH]
    reader = _verifier_reader(suite.root)

    runtime = _parse_hotpatch(raw)
    overlay_raw = _startup_overlay_raw(suite)
    overlay = _parse_hotpatch(overlay_raw)
    unsafe_raw = deepcopy(raw)
    candidate_state = json.loads(
        _reference_path(unsafe_raw["candidate_state"]).read_text(encoding="utf-8")
    )
    candidate_state["normalized_output"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "normalized-output-unsafe.json",
        canonical_json_bytes({"tokens": [9], "text": "regression"}),
    )
    output = json.loads(_reference_path(candidate_state["output"]).read_text(encoding="utf-8"))
    output["output_hash"] = candidate_state["normalized_output"]["sha256"]
    candidate_state["output"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "output-unsafe.json",
        canonical_json_bytes(output),
    )
    candidate_state["execution_result"]["stdout_uri"] = candidate_state["output"]["uri"]
    unsafe_raw["candidate_state"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "state-unsafe-output.json",
        canonical_json_bytes(candidate_state),
    )
    unsafe = _parse_hotpatch(unsafe_raw)
    none_raw = deepcopy(raw)
    none_raw["activation_mode"] = "none"
    for field_name in (
        "baseline_source",
        "candidate_source",
        "artifact_manifest",
        "artifact",
        "baseline_state",
        "candidate_state",
        "recovery_state",
    ):
        none_raw.pop(field_name)
    none = _parse_hotpatch(none_raw)

    target = _load_target()
    assert classify_hotpatch(runtime, reader, target) is HotPatchCapability.HOT_PATCH
    assert classify_hotpatch(overlay, reader, target) is HotPatchCapability.OVERLAY_ONLY
    assert classify_hotpatch(unsafe, reader, target) is HotPatchCapability.NONE
    assert classify_hotpatch(none, reader, target) is HotPatchCapability.NONE


def test_hotpatch_process_identity_is_recomputed_from_state_manifest(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    hotpatch = suite.raw[Stage0ProbeType.HOTPATCH]
    activated = json.loads(_reference_path(hotpatch["candidate_state"]).read_text(encoding="utf-8"))
    activated["process_start_token"] = "different-process"
    hotpatch["candidate_state"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "state-wrong-process.json",
        canonical_json_bytes(activated),
    )
    suite.rewrite(Stage0ProbeType.HOTPATCH)

    assert suite.verify().hot_patch is HotPatchCapability.NONE


def test_hotpatch_rejects_arbitrary_bytes_as_candidate_snapshot(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.HOTPATCH])
    raw["candidate_source"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "not-a-source-snapshot.bin",
        b"producer says this is a candidate snapshot\n",
    )

    with pytest.raises(Stage0EvidenceError) as caught:
        classify_hotpatch(_parse_hotpatch(raw), _verifier_reader(suite.root), _load_target())

    assert caught.value.code == "evidence_invalid_json"


def test_hotpatch_source_chain_must_bind_the_target_lock(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.HOTPATCH])
    baseline = json.loads(_reference_path(raw["baseline_source"]).read_text(encoding="utf-8"))
    baseline["repository"] = "git@github.com:attacker/unrelated.git"
    raw["baseline_source"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "source-unrelated.json",
        canonical_json_bytes(baseline),
    )

    assert (
        classify_hotpatch(_parse_hotpatch(raw), _verifier_reader(suite.root), _load_target())
        is HotPatchCapability.NONE
    )


def test_hotpatch_rejects_generic_or_unbound_execution_output(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.HOTPATCH])
    candidate = json.loads(_reference_path(raw["candidate_state"]).read_text(encoding="utf-8"))
    output = json.loads(_reference_path(candidate["output"]).read_text(encoding="utf-8"))
    output["workload_kind"] = "generic_artifact_mount"
    candidate["output"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "output-generic.json",
        canonical_json_bytes(output),
    )
    candidate["execution_result"]["stdout_uri"] = candidate["output"]["uri"]
    raw["candidate_state"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "state-generic-output.json",
        canonical_json_bytes(candidate),
    )

    with pytest.raises(Stage0EvidenceError) as caught:
        classify_hotpatch(_parse_hotpatch(raw), _verifier_reader(suite.root), _load_target())

    assert caught.value.code == "hotpatch_output_invalid"

    candidate["execution_result"]["stdout_uri"] = "file:///different/stdout.json"
    raw["candidate_state"] = _raw_file_reference(
        suite.root / Stage0ProbeType.HOTPATCH.value / "state-unbound-output.json",
        canonical_json_bytes(candidate),
    )
    with pytest.raises(Stage0EvidenceError) as caught:
        classify_hotpatch(_parse_hotpatch(raw), _verifier_reader(suite.root), _load_target())

    assert caught.value.code == "hotpatch_state_invalid"


def test_startup_overlay_rejects_reused_process_identity(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = _startup_overlay_raw(suite)
    root = suite.root / Stage0ProbeType.HOTPATCH.value
    for ordinal, phase in enumerate(("baseline", "candidate", "recovery")):
        state = json.loads(_reference_path(raw[f"{phase}_state"]).read_text(encoding="utf-8"))
        state["process_id"] = 31_000
        state["process_start_token"] = "linux-proc-startticks:90000"
        state["process_start_record"] = _lifecycle_record(
            root,
            name=f"reused-{phase}-process-start.json",
            event="started",
            ordinal=ordinal,
            process_id=31_000,
            start_ticks=90_000,
            captured_monotonic_ns=3_000 + ordinal * 100,
        )
        state["process_exit_record"] = _lifecycle_record(
            root,
            name=f"reused-{phase}-process-exit.json",
            event="reaped",
            ordinal=ordinal,
            process_id=31_000,
            start_ticks=90_000,
            captured_monotonic_ns=3_050 + ordinal * 100,
        )
        raw[f"{phase}_state"] = _raw_file_reference(
            root / f"reused-state-{phase}.json", canonical_json_bytes(state)
        )

    assert (
        classify_hotpatch(_parse_hotpatch(raw), _verifier_reader(suite.root), _load_target())
        is HotPatchCapability.NONE
    )


def test_overlay_capability_requires_a_read_only_mount(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.HOTPATCH])
    raw["activation_mode"] = "startup_overlay"

    with pytest.raises(ValidationError, match="read-only mount"):
        _parse_hotpatch(raw)

    for unsafe_target in ("/", "/../../etc", "/opt//overlay"):
        mounted = deepcopy(raw)
        mounted["overlay_mount"] = {
            "source_uri": raw["artifact"]["uri"],
            "source_hash": raw["artifact"]["sha256"],
            "target_path": unsafe_target,
            "read_only": True,
            "container_id": "stage0-overlay-1",
        }
        with pytest.raises(ValidationError, match="normalized non-root"):
            _parse_hotpatch(mounted)


def test_hotpatch_capability_is_derived_from_hash_relations_not_booleans(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.HOTPATCH])
    raw["activation_verified"] = True

    with pytest.raises(ValidationError, match="activation_verified"):
        _parse_hotpatch(raw)


def test_probe_barrier_requires_exactly_one_of_each_probe(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    references = list(suite.references.values())

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0Verifier(suite.protocol, _verifier_reader(suite.root)).verify(
            suite.context,
            references[:-1],
        )
    assert caught.value.code == "probe_barrier_incomplete"

    with pytest.raises(Stage0EvidenceError) as caught:
        Stage0Verifier(suite.protocol, _verifier_reader(suite.root)).verify(
            suite.context,
            [*references, references[0]],
        )
    assert caught.value.code == "duplicate_probe"


def test_fingerprint_schema_is_strict_and_bound(tmp_path: Path) -> None:
    suite = _build_suite(tmp_path)
    raw = deepcopy(suite.raw[Stage0ProbeType.FINGERPRINT])
    raw["producer_summary"] = {"hardware_ok": True}

    with pytest.raises(ValidationError, match="producer_summary"):
        FingerprintEvidenceV2.model_validate_json(canonical_json_bytes(raw))


@requires_posix_reader
def test_file_finalizer_verifies_and_publishes_both_report_formats(
    tmp_path: Path,
) -> None:
    suite = _build_suite(tmp_path)
    finalizer = FileStage0Finalizer(suite.root)

    verification = finalizer.verify(
        suite.context,
        tuple(suite.references.values()),
        protocol_version="s0-g0-v1",
    )
    decision = evaluate_stage0(
        verification.to_stage0_evidence(evidence_uri="stage0://verified-fixture")
    )
    artifacts = finalizer.publish_report(
        suite.context,
        verification,
        mode=decision.mode,
        reasons=tuple(decision.reasons),
        accepted_target_risks=("device_isolation_not_reserved",),
    )

    assert decision.mode is ProjectMode.FULL_MVP
    machine_path = Path(unquote(urlparse(artifacts.machine_report_uri).path))
    markdown_path = Path(unquote(urlparse(artifacts.markdown_report_uri).path))
    machine = json.loads(machine_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert machine["verification"]["input_digest"] == verification.input_digest
    assert machine["automatic_release_allowed"] is False
    assert machine["accepted_target_risks"] == ["device_isolation_not_reserved"]
    assert FORMAL_STAGE0_SCOPE_WARNING in markdown
    assert "device_isolation_not_reserved" in markdown


@requires_posix_reader
def test_real_b_and_c_envelopes_cross_the_file_finalizer_barrier(
    tmp_path: Path,
) -> None:
    target, runtime_profile, phase_dirs, baseline_implementation, artifact_hash = (
        _formal_hotpatch_target_and_profile(tmp_path / "producer")
    )
    runtime_profile = runtime_profile.model_copy(update={"profile": PROFILE})
    suite = _build_suite(tmp_path / "suite", target=target)
    cleaner = RuntimeCleaner()
    executor = _FormalOverlayExecutor(
        phase_dirs,
        baseline_implementation,
        artifact_hash,
    )
    adapter = RuntimeProbeAdapter(
        ProfilerCapabilityProbe(
            RuntimeRunner(
                [
                    b"rocprofiler-sdk 0.6\n",
                    (
                        b"Kernel_Name,Start_Timestamp,End_Timestamp,GPU_ID,Queue_ID\n"
                        b"hgemm_128x128,100,1000,7,1\n"
                    ),
                ]
            )
        ),
        OverlayCapabilityProbe(executor, cleaner),
        cleaner,
        target,
        runtime_profile,
        DeploymentContentAddressedEvidencePublisher(suite.root),
        RuntimeTelemetry(),
        RuntimeClock(),
    )

    def payload(probe_type: Stage0ProbeType) -> dict[str, Any]:
        reference = suite.references[probe_type]
        return {
            "task_id": str(suite.context.task_id),
            "stage0_run_id": str(suite.context.stage0_run_id),
            "target_snapshot_id": str(suite.context.target_snapshot_id),
            "target_fingerprint": suite.context.target_fingerprint,
            "target": target.model_dump(mode="json"),
            "workload_id": suite.context.workload_id,
            "adapter_profile": PROFILE,
            "probe_type": probe_type.value,
            "protocol_version": suite.protocol.protocol.protocol_version,
            "mode": "formal",
            "_job_context": {
                "lease_id": str(reference.lease_id),
                "lease_scope": "exclusive",
                "resource_id": reference.resource_id,
                "fencing_token": reference.fencing_token,
            },
        }

    profiler_output = adapter.run_probe(
        payload(Stage0ProbeType.PROFILER),
        tmp_path / "worker-output",
    )
    hotpatch_output = adapter.run_probe(
        payload(Stage0ProbeType.HOTPATCH),
        tmp_path / "worker-output",
    )
    for probe_type, output in (
        (Stage0ProbeType.PROFILER, profiler_output),
        (Stage0ProbeType.HOTPATCH, hotpatch_output),
    ):
        suite.references[probe_type] = suite.references[probe_type].model_copy(
            update={
                "raw_evidence_uri": output.raw_evidence_uri,
                "raw_evidence_hash": output.raw_evidence_hash,
                "adapter_provenance": output.adapter_provenance,
                "cleanup_evidence": output.cleanup_evidence,
            }
        )

    # Producer-owned summaries are deliberately outside the verifier input.
    profiler_output.summary["capability"] = "full"
    finalizer = FileStage0Finalizer(suite.root)
    verification = finalizer.verify(
        suite.context,
        tuple(suite.references.values()),
        protocol_version="s0-g0-v1",
    )
    decision = evaluate_stage0(
        verification.to_stage0_evidence(evidence_uri="stage0://real-producer-test")
    )
    artifacts = finalizer.publish_report(
        suite.context,
        verification,
        mode=decision.mode,
        reasons=tuple(decision.reasons),
        accepted_target_risks=("device_isolation_not_reserved",),
    )

    assert verification.profiler is ProfilerCapability.DEGRADED
    assert verification.hot_patch is HotPatchCapability.OVERLAY_ONLY
    assert decision.mode is ProjectMode.FULL_MVP
    machine = json.loads(
        Path(unquote(urlparse(artifacts.machine_report_uri).path)).read_text(encoding="utf-8")
    )
    assert machine["automatic_release_allowed"] is False
    assert machine["accepted_target_risks"] == ["device_isolation_not_reserved"]
