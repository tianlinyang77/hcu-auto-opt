from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from hcuopt.contracts.platform_v1 import MeasurementSeries
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.evaluation.m1_reporting import (
    M1AdjudicationContext,
    build_m1_adjudication_result,
    write_m1_signoff_report,
)
from hcuopt.evaluation.m1_verifier import M1CorrectnessVerifier
from tests.unit.test_m1_d_verifier import _performance, _provenance, _Suite


@pytest.mark.skipif(os.name != "posix", reason="formal evidence reader requires POSIX openat")
def test_scripted_raw_evidence_to_signoff_bundle(tmp_path: Path) -> None:
    suite = _Suite(tmp_path / "raw")
    correctness = M1CorrectnessVerifier(
        suite.protocol,
        HashedEvidenceReader(suite.root),
    ).verify(suite.context, suite.hotspot, suite.reference)
    effects = [0.10, 0.11, 0.09, 0.10]
    performance_reference, performance = _performance(
        suite,
        correctness,
        effects,
        summary={"producer_verdict": "slower"},
    )
    measurement_provenance = _provenance("measurement_harness")
    measurement = MeasurementSeries(
        measurement_id=performance_reference.measurement_id,
        status="measured",
        metric_name="kernel_latency",
        unit="ns",
        protocol_version="m1-performance-v1",
        sample_count=8,
        warmup_count=2,
        process_restart_count=4,
        raw_samples_uri=performance_reference.uri,
        raw_samples_hash=performance_reference.sha256,
        environment_fingerprint=suite.context.target_fingerprint,
        summary={"producer_verdict": "slower"},
        adapter_provenance=measurement_provenance,
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    result = build_m1_adjudication_result(
        M1AdjudicationContext(
            verification=suite.context,
            job_id=uuid4(),
            round_id=uuid4(),
            measurement=measurement,
            created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            adapter_provenance=(
                measurement_provenance,
                _provenance("candidate_adjudicator"),
            ),
        ),
        correctness,
        performance,
    )
    report_root = tmp_path / "report"
    report_root.mkdir()
    artifacts = write_m1_signoff_report(report_root, result, correctness, performance)
    assert correctness.verdict == "correct"
    assert performance.verdict.value == "faster"
    assert result.evidence.summary["automatic_release_allowed"] is False
    assert performance_reference.uri in result.evidence.raw_uris
    assert Path(artifacts.manifest["uri"].removeprefix("file://")).is_file()
