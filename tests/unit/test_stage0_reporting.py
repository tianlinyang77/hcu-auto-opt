from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
import yaml

from hcuopt.contracts.platform_v1 import AdapterProvenance, TargetSpec
from hcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    ProfilerCapability,
    Stage0ProbeType,
)
from hcuopt.domain.models import Stage0Report
from hcuopt.evaluation.stage0_reporting import (
    SHA256SUMS_NAME,
    STAGE0_REPORT_DISCLAIMER,
    VERIFICATION_JSON_NAME,
    VERIFICATION_MARKDOWN_NAME,
    Stage0ReportConflictError,
    Stage0ReportError,
    build_stage0_verification_document,
    render_stage0_verification_markdown,
    write_stage0_verification_report,
)
from hcuopt.evaluation.stage0_verifier import (
    Stage0VerificationContext,
    Stage0VerificationResult,
    target_fingerprint,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.stage0 import evaluate_stage0

ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"

TASK_ID = UUID(int=8101)
RUN_ID = UUID(int=8102)
TARGET_SNAPSHOT_ID = UUID(int=8103)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def _inputs() -> tuple[Stage0VerificationContext, Stage0VerificationResult, Stage0Report]:
    target = TargetSpec.model_validate(yaml.safe_load(TARGET_PATH.read_text(encoding="utf-8")))
    context = Stage0VerificationContext(
        task_id=TASK_ID,
        stage0_run_id=RUN_ID,
        target_snapshot_id=TARGET_SNAPSHOT_ID,
        target=target,
        target_fingerprint=target_fingerprint(target),
        workload_id="stage0-short-kernel-v1",
        adapter_profile="nmz36-stage0-composite-v1",
        expected_resource_id="hcu-7",
    )
    result = Stage0VerificationResult(
        protocol_version="s0-g0-v1",
        protocol_hash=SHA_A,
        input_digest=SHA_B,
        measurement=GateResult.PASS,
        profiler=ProfilerCapability.FULL,
        hot_patch=HotPatchCapability.HOT_PATCH,
        hardware_fingerprint=SHA_B,
        software_fingerprint=SHA_C,
        timer_resolution_ns=1.0,
        noise_sigma_ns=2.0,
        noise_cv=0.002,
        mde_ratio=0.003,
        failure_codes=(),
        reasons=(),
        statistics={
            "noise": {"cv": 0.002, "restart_means_ns": [1001.0, 999.0]},
            "known_signal": {"effect_ratio": 0.12, "bootstrap_ci": [0.11, 0.13]},
            "null_signal": {"effect_ratio": 0.0, "bootstrap_ci": [-0.01, 0.01]},
        },
        input_evidence=tuple(
            {
                "probe_record_id": str(UUID(int=8200 + ordinal)),
                "probe_type": probe_type.value,
                "uri": f"file:///results/stage0/{RUN_ID}/{probe_type.value}/raw.json",
                "sha256": "sha256:" + f"{ordinal + 1:x}" * 64,
            }
            for ordinal, probe_type in enumerate(Stage0ProbeType)
        ),
        verifier_provenance=AdapterProvenance(
            profile="stage0-d-verifier",
            capability="stage0_independent_verification",
            adapter_name="Stage0Verifier",
            adapter_version="1",
            implementation_kind="real",
            source_commit="1" * 40,
        ),
    )
    evaluation = evaluate_stage0(result.to_stage0_evidence(evidence_uri="file:///pending"))
    return context, result, evaluation


def _sha256(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_document_and_markdown_are_deterministic_without_generated_metadata() -> None:
    assert STAGE0_REPORT_DISCLAIMER == (
        "MDE 仅绑定本次 Target、Workload、指标、协议和样本预算，不是机器永久属性；"
        "Stage 0 不构成优化收益或自动发布授权。"
    )
    context, result, evaluation = _inputs()

    first = build_stage0_verification_document(context, result, evaluation)
    second = build_stage0_verification_document(context, result, evaluation)
    first_json = canonical_json_bytes(first)
    second_json = canonical_json_bytes(second)

    assert first_json == second_json
    assert render_stage0_verification_markdown(first) == render_stage0_verification_markdown(
        second
    )
    assert b"created_at" not in first_json
    assert b"timestamp" not in first_json
    assert first.disclaimer == STAGE0_REPORT_DISCLAIMER
    assert first.final_decision.automatic_release_allowed is False
    assert tuple(item.probe_type.value for item in first.input_evidence) == tuple(
        sorted(item.value for item in Stage0ProbeType)
    )


def test_writer_emits_canonical_hashed_bundle_and_fixed_declaration(tmp_path: Path) -> None:
    context, result, evaluation = _inputs()
    run_root = tmp_path / "run"
    run_root.mkdir()

    artifacts = write_stage0_verification_report(run_root, context, result, evaluation)
    report_dir = run_root / "verification"
    json_bytes = (report_dir / VERIFICATION_JSON_NAME).read_bytes()
    markdown_bytes = (report_dir / VERIFICATION_MARKDOWN_NAME).read_bytes()
    sums_bytes = (report_dir / SHA256SUMS_NAME).read_bytes()

    assert json_bytes == canonical_json_bytes(json.loads(json_bytes))
    assert json.loads(json_bytes)["disclaimer"] == STAGE0_REPORT_DISCLAIMER
    assert json.loads(json_bytes)["final_decision"]["automatic_release_allowed"] is False
    assert STAGE0_REPORT_DISCLAIMER.encode("utf-8") in markdown_bytes
    assert b"Automatic release allowed: `false`" in markdown_bytes
    assert artifacts.verification_json.sha256 == _sha256(json_bytes)
    assert artifacts.verification_markdown.sha256 == _sha256(markdown_bytes)
    assert artifacts.sha256sums.sha256 == _sha256(sums_bytes)
    assert artifacts.verification_json.byte_count == len(json_bytes)
    assert artifacts.verification_markdown.byte_count == len(markdown_bytes)
    assert json.loads(sums_bytes) == {
        "algorithm": "sha256",
        "files": {
            VERIFICATION_JSON_NAME: _sha256(json_bytes),
            VERIFICATION_MARKDOWN_NAME: _sha256(markdown_bytes),
        },
    }


def test_same_content_replay_is_exactly_idempotent(tmp_path: Path) -> None:
    context, result, evaluation = _inputs()
    run_root = tmp_path / "run"
    run_root.mkdir()

    first = write_stage0_verification_report(run_root, context, result, evaluation)
    paths = [
        run_root / "verification" / name
        for name in (VERIFICATION_JSON_NAME, VERIFICATION_MARKDOWN_NAME, SHA256SUMS_NAME)
    ]
    before = [(path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) for path in paths]

    second = write_stage0_verification_report(run_root, context, result, evaluation)
    after = [(path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) for path in paths]

    assert second == first
    assert after == before


def test_different_content_replay_conflicts_without_overwriting(tmp_path: Path) -> None:
    context, result, evaluation = _inputs()
    run_root = tmp_path / "run"
    run_root.mkdir()
    write_stage0_verification_report(run_root, context, result, evaluation)
    report_path = run_root / "verification" / VERIFICATION_JSON_NAME
    original = report_path.read_bytes()
    changed_result = result.model_copy(update={"noise_cv": 0.004})
    changed_evaluation = evaluate_stage0(
        changed_result.to_stage0_evidence(evidence_uri="file:///pending")
    )

    with pytest.raises(Stage0ReportConflictError, match="different bytes"):
        write_stage0_verification_report(
            run_root,
            context,
            changed_result,
            changed_evaluation,
        )

    assert report_path.read_bytes() == original


def test_later_artifact_conflict_is_detected_before_creating_siblings(tmp_path: Path) -> None:
    context, result, evaluation = _inputs()
    run_root = tmp_path / "run"
    report_dir = run_root / "verification"
    report_dir.mkdir(parents=True)
    conflicting_markdown = report_dir / VERIFICATION_MARKDOWN_NAME
    conflicting_markdown.write_bytes(b"different existing report\n")

    with pytest.raises(Stage0ReportConflictError, match="different bytes"):
        write_stage0_verification_report(run_root, context, result, evaluation)

    assert not (report_dir / VERIFICATION_JSON_NAME).exists()
    assert not (report_dir / SHA256SUMS_NAME).exists()
    assert conflicting_markdown.read_bytes() == b"different existing report\n"


def test_concurrent_same_content_writers_publish_one_clean_bundle(tmp_path: Path) -> None:
    context, result, evaluation = _inputs()
    run_root = tmp_path / "run"
    run_root.mkdir()
    barrier = Barrier(8)

    def publish():
        barrier.wait()
        return write_stage0_verification_report(run_root, context, result, evaluation)

    with ThreadPoolExecutor(max_workers=8) as executor:
        artifacts = list(executor.map(lambda _: publish(), range(8)))

    assert all(item == artifacts[0] for item in artifacts)
    assert {path.name for path in (run_root / "verification").iterdir()} == {
        VERIFICATION_JSON_NAME,
        VERIFICATION_MARKDOWN_NAME,
        SHA256SUMS_NAME,
    }
    assert not list((run_root / "verification").glob("*.tmp"))


def test_writer_rejects_nonregular_or_symlink_artifact_paths(tmp_path: Path) -> None:
    context, result, evaluation = _inputs()
    run_root = tmp_path / "nonregular"
    report_dir = run_root / "verification"
    report_dir.mkdir(parents=True)
    (report_dir / VERIFICATION_JSON_NAME).mkdir()

    with pytest.raises(Stage0ReportError, match="regular file"):
        write_stage0_verification_report(run_root, context, result, evaluation)

    linked_root = tmp_path / "linked"
    linked_report_dir = linked_root / "verification"
    linked_report_dir.mkdir(parents=True)
    target = linked_report_dir / "target.json"
    target.write_bytes(b"{}\n")
    link = linked_report_dir / VERIFICATION_JSON_NAME
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - Windows developer-mode policy.
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(Stage0ReportError, match="symbolic link"):
        write_stage0_verification_report(linked_root, context, result, evaluation)


def test_writer_rejects_symlink_verification_directory(tmp_path: Path) -> None:
    context, result, evaluation = _inputs()
    run_root = tmp_path / "run"
    run_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (run_root / "verification").symlink_to(elsewhere, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows developer-mode policy.
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(Stage0ReportError, match="symbolic link"):
        write_stage0_verification_report(run_root, context, result, evaluation)


def test_report_rejects_mismatched_decision_or_automatic_release() -> None:
    context, result, evaluation = _inputs()
    auto_release = Stage0Report(
        mode=evaluation.mode,
        reasons=evaluation.reasons,
        automatic_release_allowed=True,
    )

    with pytest.raises(Stage0ReportError, match="never authorize"):
        build_stage0_verification_document(context, result, auto_release)

    mismatched = Stage0Report(mode=evaluation.mode, reasons=("producer-owned conclusion",))
    with pytest.raises(Stage0ReportError, match="does not match"):
        build_stage0_verification_document(context, result, mismatched)
