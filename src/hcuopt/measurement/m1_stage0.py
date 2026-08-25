# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_verifier import Stage0EvidenceError, Stage0EvidenceReader
from hcuopt.measurement.m1_models import (
    M1SampleBudget,
    M1Stage0Authority,
    M1Stage0ReportReference,
    m1_sample_budget_hash,
)


def load_m1_stage0_authority(
    reader: Stage0EvidenceReader,
    reference: M1Stage0ReportReference,
    *,
    task_payload: Mapping[str, Any],
) -> M1Stage0Authority:
    """Re-hash and bind the Formal report before using its run-scoped threshold."""

    report = reader.read(reference.uri, reference.sha256)
    verification = report.get("verification")
    if report.get("schema_version") != "stage0-formal-report-v1" or not isinstance(
        verification, dict
    ):
        raise Stage0EvidenceError("m1_stage0_report_invalid", "not a Formal Stage 0 report")
    expected = {
        "stage0_run_id": str(task_payload["stage0_run_id"]),
        "target_snapshot_id": str(task_payload["target_snapshot_id"]),
        "target_id": task_payload["target"]["target_id"],
        "workload_id": task_payload["workload_id"],
    }
    for field, value in expected.items():
        if report.get(field) != value:
            raise Stage0EvidenceError(
                "m1_stage0_binding_mismatch", f"Formal Stage 0 report {field} mismatch"
            )
    if (
        verification.get("measurement") != "pass"
        or verification.get("protocol_version") != reference.protocol_version
        or verification.get("protocol_hash") != reference.protocol_hash
        or verification.get("input_digest") != reference.input_digest
    ):
        raise Stage0EvidenceError(
            "m1_stage0_authority_invalid", "Formal Stage 0 authority is not usable by M1"
        )
    protocol = load_registered_stage0_protocol(reference.protocol_version)
    if protocol.protocol_hash != reference.protocol_hash:
        raise Stage0EvidenceError(
            "m1_stage0_protocol_mismatch", "registered Stage 0 protocol hash changed"
        )
    values = protocol.protocol
    sampling = values.sampling
    arm_by_segment = {
        "A1": "baseline",
        "A2": "baseline",
        "B1": "candidate",
        "B2": "candidate",
    }
    sample_budget = M1SampleBudget(
        acquisition_order=tuple(
            arm_by_segment[segment]
            for _ in range(sampling.restart_count)
            for segment in sampling.signal_segment_order
        ),
        warmup_count=sampling.warmup_count,
        samples_per_acquisition=sampling.signal_samples_per_segment,
        batch_iterations=sampling.batch_iterations,
    )
    try:
        return M1Stage0Authority(
            report=reference,
            metric_name=values.timing_metric_name,
            unit=values.timing_unit,
            timer_resolution_ns=verification["timer_resolution_ns"],
            noise_sigma_ns=verification["noise_sigma_ns"],
            noise_cv=verification["noise_cv"],
            mde_ratio=verification["mde_ratio"],
            alpha=values.statistics.alpha,
            power=values.statistics.power,
            bootstrap_resamples=values.statistics.bootstrap_resamples,
            bootstrap_method=values.statistics.bootstrap_method,
            bootstrap_seed_source=values.statistics.bootstrap_seed_source,
            sample_budget=sample_budget,
            sample_budget_hash=m1_sample_budget_hash(sample_budget),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Stage0EvidenceError(
            "m1_stage0_statistics_invalid", f"Formal Stage 0 statistics are invalid: {exc}"
        ) from exc
