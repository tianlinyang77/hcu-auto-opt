# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

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
    measurement = suite.measurement
    measurement_provenance = measurement.adapter_provenance
    result = build_m1_adjudication_result(
        M1AdjudicationContext(
            verification=suite.context,
            performance_verification=suite.performance_context,
            job_id=uuid4(),
            round_id=suite.performance_context.round_id,
            measurement=measurement,
            created_at=measurement.created_at,
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
