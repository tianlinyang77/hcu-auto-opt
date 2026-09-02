# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from hcuopt.cli import main
from hcuopt.contracts.formal_readiness_v1 import (
    FormalReadinessEvidenceRef,
    FormalReadinessManifest,
)
from hcuopt.operator.readiness import (
    FormalReadinessAuditor,
    load_formal_readiness_manifest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / "config" / "m2" / "nmz36-formal-readiness-v1.yaml"
FIXED_TIME = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _single_evidence_manifest(relative_path: str, sha256: str) -> FormalReadinessManifest:
    raw = load_formal_readiness_manifest(MANIFEST_PATH).model_dump(mode="json")
    evidence = {
        "path": relative_path,
        "sha256": sha256,
        "digest_mode": "raw_bytes",
        "evidence_type": "test_readiness_evidence",
    }
    for gate in raw["gates"]:
        gate["evidence"] = [evidence]
    return FormalReadinessManifest.model_validate(raw)


def _auditor() -> FormalReadinessAuditor:
    return FormalReadinessAuditor(clock=lambda: FIXED_TIME)


def test_repository_manifest_reports_a_machine_verifiable_hold() -> None:
    manifest = load_formal_readiness_manifest(MANIFEST_PATH)

    report = _auditor().evaluate(manifest, REPOSITORY_ROOT)

    assert report.decision == "hold"
    assert report.generated_at == FIXED_TIME
    assert report.verified_evidence_count == 23
    assert len(report.gate_results) == 13
    assert set(report.blocker_codes) == {
        "formal_authority_persistence",
        "formal_evidence_finalizer",
        "formal_measurement_adapter",
        "formal_operator_profiles",
        "formal_plan_compiler",
        "formal_start_intent",
        "owner_window_authorization",
        "round_signoff_outbox",
        "target_lock_refresh",
        "review_a_pending",
        "review_b_pending",
        "review_d_pending",
    }
    assert all(
        evidence.status == "verified"
        for gate in report.gate_results
        for evidence in gate.evidence
    )
    formal_persistence = next(
        gate
        for gate in report.gate_results
        if gate.code == "formal_authority_persistence"
    )
    assert formal_persistence.declared_status == "hold"
    assert formal_persistence.effective_status == "hold"
    formal_finalizer = next(
        gate for gate in report.gate_results if gate.code == "formal_evidence_finalizer"
    )
    assert formal_finalizer.declared_status == "hold"
    assert formal_finalizer.effective_status == "hold"
    assert report.profile_registration_allowed is False
    assert report.formal_round_creation_allowed is False
    assert report.window_authorization_required is True
    assert report.hcu_accessed is False
    assert report.automatic_release_allowed is False


def test_evidence_hash_drift_fails_closed(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.txt"
    evidence_path.write_text("before\n", encoding="utf-8")
    manifest = _single_evidence_manifest("evidence.txt", _sha256(b"before\n"))
    evidence_path.write_text("after\n", encoding="utf-8")

    report = _auditor().evaluate(manifest, tmp_path)

    assert report.decision == "hold"
    assert report.verified_evidence_count == 0
    assert all(result.evidence_status == "hash_mismatch" for result in report.gate_results)
    assert all(result.effective_status == "block" for result in report.gate_results)
    assert "historical_stage0_authority_evidence_hash_mismatch" in report.blocker_codes


def test_missing_evidence_fails_closed(tmp_path: Path) -> None:
    manifest = _single_evidence_manifest("missing.txt", _sha256(b"missing\n"))

    report = _auditor().evaluate(manifest, tmp_path)

    assert report.decision == "hold"
    assert report.verified_evidence_count == 0
    assert all(result.evidence_status == "missing" for result in report.gate_results)
    assert "scripted_barrier_fwer_evidence_evidence_missing" in report.blocker_codes


def test_contract_rejects_repository_escape_and_windows_paths() -> None:
    valid = {
        "sha256": "sha256:" + "0" * 64,
        "digest_mode": "raw_bytes",
        "evidence_type": "test",
    }

    with pytest.raises(ValidationError, match="repository-relative"):
        FormalReadinessEvidenceRef.model_validate({**valid, "path": "../outside"})
    with pytest.raises(ValidationError, match="repository-relative"):
        FormalReadinessEvidenceRef.model_validate({**valid, "path": "docs/./evidence.md"})
    with pytest.raises(ValidationError, match="POSIX separators"):
        FormalReadinessEvidenceRef.model_validate({**valid, "path": r"docs\evidence.md"})


def test_auditor_rejects_symlink_in_any_path_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_dir = tmp_path / "linked"
    evidence_dir.mkdir()
    evidence_path = evidence_dir / "evidence.txt"
    evidence_path.write_text("evidence\n", encoding="utf-8")
    manifest = _single_evidence_manifest(
        "linked/evidence.txt",
        _sha256(b"evidence\n"),
    )
    path_type = type(tmp_path)
    original = path_type.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        return path.name == "linked" or original(path)

    monkeypatch.setattr(path_type, "is_symlink", fake_is_symlink)

    report = _auditor().evaluate(manifest, tmp_path)

    assert all(result.evidence_status == "invalid_path" for result in report.gate_results)
    assert "historical_stage0_authority_evidence_invalid_path" in report.blocker_codes


def test_text_lf_digest_is_stable_across_checkout_line_endings(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.txt"
    evidence_path.write_bytes(b"line-one\r\nline-two\r\n")
    raw = load_formal_readiness_manifest(MANIFEST_PATH).model_dump(mode="json")
    evidence = {
        "path": "evidence.txt",
        "sha256": _sha256(b"line-one\nline-two\n"),
        "digest_mode": "text_lf",
        "evidence_type": "portable_text",
    }
    for gate in raw["gates"]:
        gate["evidence"] = [evidence]
    manifest = FormalReadinessManifest.model_validate(raw)

    report = _auditor().evaluate(manifest, tmp_path)

    assert all(result.evidence_status == "verified" for result in report.gate_results)


@pytest.mark.parametrize("mutation", ["missing", "out_of_order"])
def test_manifest_requires_every_gate_in_canonical_order(mutation: str) -> None:
    raw = load_formal_readiness_manifest(MANIFEST_PATH).model_dump(mode="json")
    if mutation == "missing":
        raw["gates"].pop()
    else:
        raw["gates"][0], raw["gates"][1] = raw["gates"][1], raw["gates"][0]

    with pytest.raises(ValidationError, match="complete and canonically sorted"):
        FormalReadinessManifest.model_validate(raw)


def test_missing_candidate_family_cannot_declare_readiness_pass() -> None:
    raw = load_formal_readiness_manifest(MANIFEST_PATH).model_dump(mode="json")
    raw["profile_draft"].update(
        {
            "candidate_family_state": "missing",
            "candidate_packages": [],
            "candidate_family_hash": None,
        }
    )
    raw["gates"][0]["status"] = "pass"

    with pytest.raises(ValidationError, match="missing Candidate Family"):
        FormalReadinessManifest.model_validate(raw)


def test_cli_emits_json_and_uses_exit_two_for_hold(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "formal-readiness",
            str(MANIFEST_PATH),
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output["decision"] == "hold"
    assert output["profile_registration_allowed"] is False
    assert output["formal_round_creation_allowed"] is False
    assert output["hcu_accessed"] is False
    assert output["automatic_release_allowed"] is False
