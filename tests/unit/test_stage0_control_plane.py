from uuid import uuid4

import pytest
from pydantic import ValidationError

from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.contracts.v1 import Stage0EvidenceRequest, Stage0ProbeResult
from hcuopt.domain.enums import (
    GateResult,
    ProjectMode,
    Stage0ProbeType,
)
from hcuopt.domain.errors import ContractError
from hcuopt.stage0 import evaluate_stage0, evidence_from_probe_summaries


def _probe_summaries() -> dict[Stage0ProbeType, dict[str, object]]:
    return {
        Stage0ProbeType.FINGERPRINT: {
            "hardware_fingerprint": "sha256:hardware",
            "software_fingerprint": "sha256:software",
        },
        Stage0ProbeType.TIMER: {"timer_resolution_ns": 100.0},
        Stage0ProbeType.NOISE: {
            "gate_result": "pass",
            "noise_sigma_ns": 200.0,
            "noise_cv": 0.01,
            "mde_ratio": 0.03,
        },
        Stage0ProbeType.KNOWN_SIGNAL: {"detected": True},
        Stage0ProbeType.NULL_SIGNAL: {"false_positive": False},
        Stage0ProbeType.PROFILER: {"capability": "full"},
        Stage0ProbeType.HOTPATCH: {"capability": "hot_patch"},
    }


def test_probe_barrier_builds_the_existing_four_mode_input() -> None:
    evidence = evidence_from_probe_summaries(
        _probe_summaries(), evidence_uri="stage0://runs/fixture"
    )
    report = evaluate_stage0(evidence)

    assert evidence.measurement is GateResult.PASS
    assert report.mode is ProjectMode.FULL_MVP
    assert report.automatic_release_allowed is False


@pytest.mark.parametrize(
    ("probe_type", "replacement"),
    [
        (Stage0ProbeType.KNOWN_SIGNAL, {"detected": False}),
        (Stage0ProbeType.NULL_SIGNAL, {"false_positive": True}),
        (
            Stage0ProbeType.NOISE,
            {
                "gate_result": "fail",
                "noise_sigma_ns": 200.0,
                "noise_cv": 0.01,
                "mde_ratio": 0.03,
            },
        ),
    ],
)
def test_measurement_controls_fail_closed(
    probe_type: Stage0ProbeType, replacement: dict[str, object]
) -> None:
    probes = _probe_summaries()
    probes[probe_type] = replacement

    evidence = evidence_from_probe_summaries(
        probes, evidence_uri="stage0://runs/fixture"
    )

    assert evidence.measurement is GateResult.FAIL
    assert evaluate_stage0(evidence).mode is ProjectMode.STOPPED_MEASUREMENT


def test_real_probe_requires_raw_hashed_evidence() -> None:
    provenance = AdapterProvenance(
        profile="real-stage0",
        capability="timer",
        adapter_name="FixtureTimer",
        adapter_version="1",
        implementation_kind="real",
    )
    with pytest.raises(ValidationError, match="raw evidence URI and SHA256"):
        Stage0ProbeResult(
            stage0_run_id=uuid4(),
            target_snapshot_id=uuid4(),
            probe_type=Stage0ProbeType.TIMER,
            protocol_version="fixture-v1",
            adapter_provenance=[provenance],
        )


def test_fake_probe_cannot_claim_real_origin() -> None:
    provenance = AdapterProvenance(
        profile="fake-stage0",
        capability="timer",
        adapter_name="FakeTimer",
        adapter_version="1",
        implementation_kind="fake",
    )
    with pytest.raises(ValidationError, match="fake Stage 0 probes must be synthetic"):
        Stage0ProbeResult(
            stage0_run_id=uuid4(),
            target_snapshot_id=uuid4(),
            probe_type=Stage0ProbeType.TIMER,
            protocol_version="fixture-v1",
            raw_evidence_uri="file:///fixture.json",
            raw_evidence_hash="sha256:" + "0" * 64,
            adapter_provenance=[provenance],
            synthetic=False,
        )


def test_legacy_stage0_input_is_always_synthetic() -> None:
    with pytest.raises(ValidationError, match="Input should be True"):
        Stage0EvidenceRequest(
            measurement="fail",
            profiler="none",
            hot_patch="none",
            hardware_fingerprint="fixture-hardware",
            software_fingerprint="fixture-software",
            synthetic=False,
        )


@pytest.mark.parametrize(
    ("probe_type", "replacement", "message"),
    [
        (Stage0ProbeType.NOISE, {"gate_result": {}}, "gate_result=pass\\|fail"),
        (Stage0ProbeType.PROFILER, {"capability": {}}, "unknown capability"),
        (Stage0ProbeType.HOTPATCH, {"capability": {}}, "unknown capability"),
    ],
)
def test_malformed_capability_summaries_fail_as_contract_errors(
    probe_type: Stage0ProbeType,
    replacement: dict[str, object],
    message: str,
) -> None:
    probes = _probe_summaries()
    probes[probe_type] = replacement

    with pytest.raises(ContractError, match=message):
        evidence_from_probe_summaries(
            probes,
            evidence_uri="stage0://runs/malformed-fixture",
        )
