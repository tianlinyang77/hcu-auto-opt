from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import LeaseScope, Stage0ProbeType, Stage0RunMode
from hcuopt.evaluation.stage0_protocol import LoadedStage0Protocol
from hcuopt.evaluation.stage0_verifier import target_fingerprint
from hcuopt.measurement.evidence import EvidenceArtifact, write_evidence

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64


def _telemetry() -> dict[str, Any]:
    return {
        "device": {
            "device_index": 7,
            "temperature_c": 55.0,
            "hotspot_temperature_c": 55.0,
            "sclk_mhz": 1350.0,
            "mclk_mhz": 875.0,
            "performance_level": "manual",
            "power_w": 210.0,
        },
        "cache": {
            "state": "flushed",
            "cleared_before_sample": True,
            "identity_hash": None,
        },
        "background_processes": [],
    }


def _observations() -> list[dict[str, Any]]:
    return [
        {
            "phase": "before_run",
            "captured_monotonic_ns": 1_000_000,
            "restart_ordinal": None,
            "acquisition_ordinal": None,
            "telemetry": _telemetry(),
        },
        {
            "phase": "after_run",
            "captured_monotonic_ns": 1_000_000_000,
            "restart_ordinal": None,
            "acquisition_ordinal": None,
            "telemetry": _telemetry(),
        },
    ]


def _binding(
    probe_type: Stage0ProbeType,
    *,
    task_id: UUID,
    stage0_run_id: UUID,
    target_snapshot_id: UUID,
    target: TargetSpec,
    workload_id: str,
    protocol: LoadedStage0Protocol,
    lease_id: UUID,
    resource_id: str,
    fencing_token: int,
) -> dict[str, Any]:
    return {
        "task_id": str(task_id),
        "stage0_run_id": str(stage0_run_id),
        "target_snapshot_id": str(target_snapshot_id),
        "target_id": target.target_id,
        "target_fingerprint": target_fingerprint(target),
        "environment_fingerprint": SHA_B,
        "workload_id": workload_id,
        "probe_type": probe_type.value,
        "run_mode": Stage0RunMode.FORMAL.value,
        "protocol_version": protocol.protocol.protocol_version,
        "protocol_hash": protocol.protocol_hash,
        "metric_name": "kernel_elapsed",
        "unit": "ns",
        "measurement_id": str(UUID(int=400 + list(Stage0ProbeType).index(probe_type))),
        "lease": {
            "lease_id": str(lease_id),
            "lease_scope": LeaseScope.EXCLUSIVE.value,
            "resource_id": resource_id,
            "fencing_token": fencing_token,
        },
    }


def _calibration() -> dict[str, Any]:
    return {
        "device_name": "hcu-event",
        "device_index": 7,
        "timer_resolution_ns": 1.0,
        "ns_per_tick": 1.0,
        "max_residual_ns": 1.0,
        "points": [
            {
                "point_ordinal": ordinal,
                "device_ticks": device_ticks,
                "host_started_monotonic_ns": midpoint - 1,
                "host_finished_monotonic_ns": midpoint + 1,
            }
            for ordinal, (device_ticks, midpoint) in enumerate(
                ((1_000, 10_001), (2_000, 11_001), (3_000, 12_001))
            )
        ],
        "resolution_tick_deltas": [1, 1, 1],
    }


def _samples(
    segment_order: tuple[str, ...],
    samples_per_segment: int,
    *,
    comparison_effect: float = 0.0,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    acquisition = 0
    for restart in range(10):
        for segment in segment_order:
            arm = "single"
            sample_ns = 1_000.0
            if segment in {"A1", "A2"}:
                arm = "baseline"
            elif segment in {"B1", "B2"}:
                arm = "comparison"
                sample_ns *= 1.0 + comparison_effect
            for segment_ordinal in range(samples_per_segment):
                started_device_ticks = 1_000_000 + acquisition * 200_000
                elapsed_ticks = int(round(sample_ns * 100))
                started_host_ns = 2_000_000 + acquisition * 200_000
                samples.append(
                    {
                        "process_id": 10_000 + restart,
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
    return samples


def _measurement_evidence(
    probe_type: Stage0ProbeType,
    *,
    binding: dict[str, Any],
    provenance: AdapterProvenance,
) -> dict[str, Any]:
    if probe_type is Stage0ProbeType.TIMER:
        segment_order = ("timer",)
        samples_per_segment = 1
        comparison_effect = 0.0
    elif probe_type is Stage0ProbeType.NOISE:
        segment_order = ("noise",)
        samples_per_segment = 30
        comparison_effect = 0.0
    else:
        segment_order = ("A1", "B1", "B2", "A2")
        samples_per_segment = 10
        comparison_effect = 0.12 if probe_type is Stage0ProbeType.KNOWN_SIGNAL else 0.0
    return {
        "schema_version": "measurement-evidence-v2",
        "binding": binding,
        "plan": {
            "restart_count": 10,
            "warmup_count": 10,
            "batch_iterations": 100,
            "segment_order": list(segment_order),
            "samples_per_segment": samples_per_segment,
        },
        "calibration": _calibration(),
        "samples": _samples(
            segment_order,
            samples_per_segment,
            comparison_effect=comparison_effect,
        ),
        "observations": _observations(),
        "adapter_provenance": [provenance.model_dump(mode="json")],
        "synthetic": False,
    }


def write_formal_stage0_raw_suite(
    run_root: Path,
    *,
    task_id: UUID,
    stage0_run_id: UUID,
    target_snapshot_id: UUID,
    target: TargetSpec,
    workload_id: str,
    protocol: LoadedStage0Protocol,
    lease_id: UUID,
    resource_id: str,
    fencing_token: int,
    provenance: AdapterProvenance,
) -> dict[Stage0ProbeType, EvidenceArtifact]:
    """Write one deterministic, passing raw-v2 artifact for every Stage 0 probe."""

    common = {
        "schema_version": "measurement-evidence-v2",
        "observations": _observations(),
        "adapter_provenance": [provenance.model_dump(mode="json")],
        "synthetic": False,
    }
    raw: dict[Stage0ProbeType, dict[str, Any]] = {}
    for probe_type in Stage0ProbeType:
        binding = _binding(
            probe_type,
            task_id=task_id,
            stage0_run_id=stage0_run_id,
            target_snapshot_id=target_snapshot_id,
            target=target,
            workload_id=workload_id,
            protocol=protocol,
            lease_id=lease_id,
            resource_id=resource_id,
            fencing_token=fencing_token,
        )
        if probe_type in {
            Stage0ProbeType.TIMER,
            Stage0ProbeType.NOISE,
            Stage0ProbeType.KNOWN_SIGNAL,
            Stage0ProbeType.NULL_SIGNAL,
        }:
            raw[probe_type] = _measurement_evidence(
                probe_type,
                binding=binding,
                provenance=provenance,
            )
        elif probe_type is Stage0ProbeType.FINGERPRINT:
            raw[probe_type] = {
                **common,
                "binding": binding,
                "hardware_fingerprint": SHA_C,
                "software_fingerprint": SHA_D,
            }
        elif probe_type is Stage0ProbeType.PROFILER:
            raw[probe_type] = {
                **common,
                "binding": binding,
                "parser_succeeded": True,
                "kernels": [
                    {
                        "kernel_name": "hgemm_128x128",
                        "duration_ns": 900.0,
                        "call_count": 10,
                        "shapes": ["1x128x128"],
                        "dtypes": ["float16"],
                        "metadata": {"grid": "128x1x1"},
                        "python_source": "model.layers.0.mlp",
                        "hip_symbol": "_Z17hgemm_128x128v",
                    }
                ],
            }
        else:
            raw[probe_type] = {
                **common,
                "binding": binding,
                "activation_mode": "runtime_hot_patch",
                "original_state_hash": SHA_A,
                "activated_state_hash": SHA_B,
                "recovered_state_hash": SHA_A,
                "baseline_output_hash": SHA_C,
                "activated_output_hash": SHA_C,
                "baseline_cache_namespace_hash": SHA_C,
                "activated_cache_namespace_hash": SHA_D,
            }

    return {
        probe_type: write_evidence(run_root / probe_type.value / "raw.json", raw_value)
        for probe_type, raw_value in raw.items()
    }


def write_formal_stage0_raw_probe(
    path: Path,
    probe_type: Stage0ProbeType,
    *,
    task_id: UUID,
    stage0_run_id: UUID,
    target_snapshot_id: UUID,
    target: TargetSpec,
    workload_id: str,
    protocol: LoadedStage0Protocol,
    lease_id: UUID,
    resource_id: str,
    fencing_token: int,
    provenance: AdapterProvenance,
) -> EvidenceArtifact:
    """Write one passing raw-v2 probe using its actual physical lease identity."""

    binding = _binding(
        probe_type,
        task_id=task_id,
        stage0_run_id=stage0_run_id,
        target_snapshot_id=target_snapshot_id,
        target=target,
        workload_id=workload_id,
        protocol=protocol,
        lease_id=lease_id,
        resource_id=resource_id,
        fencing_token=fencing_token,
    )
    common = {
        "schema_version": "measurement-evidence-v2",
        "binding": binding,
        "observations": _observations(),
        "adapter_provenance": [provenance.model_dump(mode="json")],
        "synthetic": False,
    }
    if probe_type in {
        Stage0ProbeType.TIMER,
        Stage0ProbeType.NOISE,
        Stage0ProbeType.KNOWN_SIGNAL,
        Stage0ProbeType.NULL_SIGNAL,
    }:
        raw = _measurement_evidence(
            probe_type,
            binding=binding,
            provenance=provenance,
        )
    elif probe_type is Stage0ProbeType.FINGERPRINT:
        raw = {
            **common,
            "hardware_fingerprint": SHA_C,
            "software_fingerprint": SHA_D,
        }
    elif probe_type is Stage0ProbeType.PROFILER:
        raw = {
            **common,
            "parser_succeeded": True,
            "kernels": [
                {
                    "kernel_name": "hgemm_128x128",
                    "duration_ns": 900.0,
                    "call_count": 10,
                    "shapes": ["1x128x128"],
                    "dtypes": ["float16"],
                    "metadata": {"grid": "128x1x1"},
                    "python_source": "model.layers.0.mlp",
                    "hip_symbol": "_Z17hgemm_128x128v",
                }
            ],
        }
    else:
        raw = {
            **common,
            "activation_mode": "runtime_hot_patch",
            "original_state_hash": SHA_A,
            "activated_state_hash": SHA_B,
            "recovered_state_hash": SHA_A,
            "baseline_output_hash": SHA_C,
            "activated_output_hash": SHA_C,
            "baseline_cache_namespace_hash": SHA_C,
            "activated_cache_namespace_hash": SHA_D,
        }
    return write_evidence(path, raw)
