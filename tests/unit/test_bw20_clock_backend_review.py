# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import json
from pathlib import Path

import pytest

from hcuopt.deployment.bw20_clock_backend_review import (
    BW20ClockBackendReviewManifest,
    inspect_clock_backend_review,
    main,
)
from hcuopt.deployment.bw20_clock_journal import ClockJournal, ClockJournalError
from hcuopt.deployment.bw20_runtime_verify import checked_digest
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE

TARGET_HASH = "sha256:" + "a" * 64


def fixture(tmp_path, *, with_acceptance=False):
    backend = tmp_path / "reviewed-backend.bin"
    # Deliberately not executable Python: static review must never import it.
    backend.write_bytes(b"not-an-executable-backend")
    journal = ClockJournal(tmp_path / "clock.sqlite")
    evidence = tmp_path / "hardware-acceptance.json"
    if with_acceptance:
        evidence.write_text('{"scope":"fixture-only"}')
    manifest = BW20ClockBackendReviewManifest(
        target_id="bw20-sglang-0.5.12",
        target_fingerprint=TARGET_HASH,
        backend_artifact=backend,
        backend_sha256=checked_digest(backend),
        journal_path=journal.path,
        authority_provider_id="operator/reviewed-clock-authority-v1",
        hardware_acceptance_evidence=evidence if with_acceptance else None,
        hardware_acceptance_sha256=checked_digest(evidence) if with_acceptance else None,
    )
    path = tmp_path / "clock-backend-review.json"
    path.write_text(manifest.model_dump_json())
    return path, checked_digest(path), backend, journal, evidence


def inspect(path, pin, journal):
    return inspect_clock_backend_review(
        path,
        manifest_sha256=pin,
        expected_target_id="bw20-sglang-0.5.12",
        expected_target_fingerprint=TARGET_HASH,
        journal=journal,
    )


@pytest.mark.parametrize("with_acceptance", [False, True])
def test_static_review_never_imports_executes_or_authorizes(tmp_path, with_acceptance):
    path, pin, _, journal, _ = fixture(tmp_path, with_acceptance=with_acceptance)
    report = inspect(path, pin, journal)
    assert report["static_inputs_verified"] is True
    assert report["backend_artifact_hash_verified"] is True
    assert report["hardware_acceptance_hash_verified"] is with_acceptance
    for field in (
        "backend_imported", "backend_executed", "clock_mutation_performed",
        "hardware_acceptance_semantics_verified", "clock_control_bound",
        "execution_allowed", "stage0_accepted", "automatic_release_allowed",
    ):
        assert report[field] is False
    assert report["review_blockers"] == [
        "hardware_acceptance_semantics_not_adjudicated"
        if with_acceptance
        else "hardware_acceptance_evidence_missing"
    ]


@pytest.mark.parametrize("damage", ["manifest", "backend", "target", "journal", "acceptance"])
def test_changed_binding_or_retained_bytes_are_rejected(tmp_path, damage):
    path, pin, backend, journal, evidence = fixture(tmp_path, with_acceptance=True)
    expected_target = TARGET_HASH
    if damage == "manifest":
        path.write_text("{}")
    elif damage == "backend":
        backend.write_bytes(b"changed")
    elif damage == "target":
        expected_target = "sha256:" + "b" * 64
    elif damage == "journal":
        journal = ClockJournal(tmp_path / "different-clock.sqlite")
    else:
        evidence.write_text("changed")
    with pytest.raises(ValueError):
        inspect_clock_backend_review(
            path,
            manifest_sha256=pin,
            expected_target_id="bw20-sglang-0.5.12",
            expected_target_fingerprint=expected_target,
            journal=journal,
        )


def test_unresolved_clock_intent_is_reported_without_recovery_or_write(tmp_path):
    path, pin, _, journal, _ = fixture(tmp_path)
    journal.begin(
        resource_id=RESOURCE,
        authorization_id="fixture",
        original={"mode": "auto"},
    )
    before = checked_digest(journal.path)
    report = inspect(path, pin, journal)
    after = checked_digest(journal.path)
    assert before == after
    assert report["journal_clear"] is False
    assert "clock_journal_requires_reconciliation" in report["review_blockers"]
    assert report["execution_allowed"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"backend_artifact": "relative.bin"},
        {"journal_path": "relative.sqlite"},
        {"hardware_acceptance_evidence": Path("relative.json"),
         "hardware_acceptance_sha256": "sha256:" + "b" * 64},
        {"hardware_acceptance_sha256": "sha256:" + "b" * 64},
        {"password": "must-not-be-accepted"},
    ],
)
def test_manifest_rejects_relative_paths_partial_evidence_and_secrets(tmp_path, change):
    path, _, backend, journal, _ = fixture(tmp_path)
    raw = json.loads(path.read_text())
    raw.update(change)
    raw.setdefault("backend_artifact", str(backend))
    raw.setdefault("journal_path", str(journal.path))
    with pytest.raises(ValueError):
        BW20ClockBackendReviewManifest.model_validate(raw)


def test_read_only_cli_opens_existing_journal_without_changing_bytes(tmp_path, capsys):
    path, pin, _, journal, _ = fixture(tmp_path)
    before = checked_digest(journal.path)
    assert main([
        str(path), "--sha256", pin,
        "--target-id", "bw20-sglang-0.5.12",
        "--target-fingerprint", TARGET_HASH,
    ]) == 0
    after = checked_digest(journal.path)
    report = json.loads(capsys.readouterr().out)
    assert before == after
    assert report["static_inputs_verified"] is True
    assert report["clock_control_bound"] is False
    assert report["execution_allowed"] is False


def test_read_only_cli_never_creates_missing_journal(tmp_path):
    path, _, _, journal, _ = fixture(tmp_path)
    missing = tmp_path / "missing-clock.sqlite"
    raw = json.loads(path.read_text())
    raw["journal_path"] = str(missing)
    path.write_text(json.dumps(raw))
    with pytest.raises(ClockJournalError, match="unavailable"):
        main([
            str(path), "--sha256", checked_digest(path),
            "--target-id", "bw20-sglang-0.5.12",
            "--target-fingerprint", TARGET_HASH,
        ])
    assert not missing.exists()
    assert journal.path.exists()
