# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hcuopt.adapters.m2_candidate import (
    ScriptedCandidateIntake,
    candidate_source_package_hash,
)
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.m2 import (
    RoundBudget,
    ScriptedCandidatePackageInput,
    SearchRound,
)
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes

REPLACEMENT_POINT = "sglang.fixture.layer_norm"
OVERLAY_PATH = "sglang/fixture_kernel.py"
MOUNT_TARGET = "/opt/hcuopt/overlay/sglang/fixture_kernel.py"
PROFILER_URI = "fixture:///m2/profiler.json"


def _hash(value: str) -> str:
    return "sha256:" + value * 64


def _round(hotspot_id, **updates) -> SearchRound:
    payload = {
        "round_id": uuid4(),
        "task_id": uuid4(),
        "idempotency_key": "m2-candidate-intake-round",
        "state": "intake_open",
        "run_mode": "scripted",
        "project_mode": None,
        "target_snapshot_id": uuid4(),
        "stage0_run_id": uuid4(),
        "stage0_protocol_hash": _hash("1"),
        "baseline_epoch_id": uuid4(),
        "hotspot_id": hotspot_id,
        "replacement_point": REPLACEMENT_POINT,
        "workload_id": "m2-scripted-workload-v1",
        "workload_hash": _hash("2"),
        "configuration_hash": _hash("3"),
        "image_digest": _hash("4"),
        "adapter_profile": "m2-scripted-v1",
        "declared_candidate_count": 2,
        "max_promoted": 2,
        "family_alpha": 0.05,
        "search_plan_hash": _hash("5"),
        "holdout_plan_commitment": _hash("6"),
        "holdout_plan_authority_id": "synthetic-holdout-v1",
        "holdout_plan_authority_hash": _hash("7"),
        "selection_rule_hash": _hash("8"),
        "budget": RoundBudget(
            max_candidates=2,
            max_build_attempts=4,
            max_correctness_attempts=4,
            max_search_samples=200,
            max_holdout_samples=200,
            max_wall_seconds=600,
            max_exclusive_lease_seconds=300,
        ),
        "version": 1,
        "created_at": datetime.now(timezone.utc),
    }
    payload.update(updates)
    return SearchRound.model_validate(payload)


def _publish_package(
    root: Path,
    *,
    hotspot_id,
    candidate_id,
    baseline_source_hash: str,
    candidate_source_hash: str,
) -> tuple[CandidateSourcePackageManifest, str, str]:
    content = b"def fixture_kernel(value):\n    return value\n"
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_source_hash=baseline_source_hash,
        candidate_source_hash=candidate_source_hash,
        replacement_point=REPLACEMENT_POINT,
        candidate_kind="fixture",
        overlay_mount_target=MOUNT_TARGET,
        files=[{"path": OVERLAY_PATH, "content_hash": content_hash}],
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=_hash("9"),
        reviewed_by="m2-scripted-fixture",
        reviewed_at=datetime.now(timezone.utc),
    )
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_hash = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    package_hash = candidate_source_package_hash(manifest_hash, manifest.files)
    digest = candidate_source_hash.removeprefix("sha256:")
    package_root = root / "sha256" / digest[:2] / digest[2:]
    source = package_root / "files" / OVERLAY_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    (package_root / "manifest.json").write_bytes(manifest_bytes)
    return manifest, manifest_hash, package_hash


def _store(root: Path) -> CandidateSourcePackageStore:
    return CandidateSourcePackageStore(
        root,
        profile="m2-scripted-v1",
        allowed_overlay_roots=("sglang",),
        approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
    )


def test_scripted_candidate_intake_replays_one_deterministic_member(
    tmp_path: Path,
) -> None:
    hotspot_id = uuid4()
    candidate_id = uuid4()
    baseline_source_hash = _hash("a")
    candidate_source_hash = _hash("b")
    manifest, manifest_hash, package_hash = _publish_package(
        tmp_path,
        hotspot_id=hotspot_id,
        candidate_id=candidate_id,
        baseline_source_hash=baseline_source_hash,
        candidate_source_hash=candidate_source_hash,
    )
    source_packages = _store(tmp_path)
    intake = ScriptedCandidateIntake(
        source_packages,
        store_id="m2-scripted-store-v1",
        store_hash=_hash("c"),
    )
    round_authority = _round(hotspot_id)
    candidate_input = ScriptedCandidatePackageInput(
        ordinal=0,
        source_package_ref={
            "candidate_source_hash": candidate_source_hash,
            "source_package_hash": package_hash,
            "manifest_hash": manifest_hash,
            "manifest_schema_version": "m1-candidate-source-v1",
        },
        optimization_intent="fixture package replay",
    )

    first = intake.resolve(
        round_authority=round_authority,
        candidate_input=candidate_input,
        baseline_source_hash=baseline_source_hash,
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=manifest.profiler_evidence_hash,
    )
    replay = intake.resolve(
        round_authority=round_authority,
        candidate_input=candidate_input,
        baseline_source_hash=baseline_source_hash,
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=manifest.profiler_evidence_hash,
    )

    assert replay.member == first.member
    assert first.member.candidate_id == candidate_id
    assert first.member.source_package_hash == package_hash
    assert first.member.source_manifest_hash == manifest_hash
    assert first.member.candidate_kind.value == "fixture"
    assert first.package.files_root.is_dir()


def test_scripted_candidate_identity_changes_with_frozen_input(
    tmp_path: Path,
) -> None:
    hotspot_id = uuid4()
    baseline_source_hash = _hash("a")
    candidate_source_hash = _hash("b")
    manifest, manifest_hash, package_hash = _publish_package(
        tmp_path,
        hotspot_id=hotspot_id,
        candidate_id=uuid4(),
        baseline_source_hash=baseline_source_hash,
        candidate_source_hash=candidate_source_hash,
    )
    intake = ScriptedCandidateIntake(
        _store(tmp_path), store_id="m2-scripted-store-v1", store_hash=_hash("c")
    )
    round_authority = _round(hotspot_id)

    def resolve(ordinal: int, intent: str):
        return intake.resolve(
            round_authority=round_authority,
            candidate_input=ScriptedCandidatePackageInput(
                ordinal=ordinal,
                source_package_ref={
                    "candidate_source_hash": candidate_source_hash,
                    "source_package_hash": package_hash,
                    "manifest_hash": manifest_hash,
                    "manifest_schema_version": "m1-candidate-source-v1",
                },
                optimization_intent=intent,
            ),
            baseline_source_hash=baseline_source_hash,
            profiler_evidence_uri=PROFILER_URI,
            profiler_evidence_hash=manifest.profiler_evidence_hash,
        ).member.round_candidate_id

    assert resolve(0, "intent-a") != resolve(1, "intent-a")
    assert resolve(0, "intent-a") != resolve(0, "intent-b")


def test_scripted_candidate_intake_rejects_authority_drift(tmp_path: Path) -> None:
    hotspot_id = uuid4()
    baseline_source_hash = _hash("a")
    candidate_source_hash = _hash("b")
    manifest, manifest_hash, package_hash = _publish_package(
        tmp_path,
        hotspot_id=hotspot_id,
        candidate_id=uuid4(),
        baseline_source_hash=baseline_source_hash,
        candidate_source_hash=candidate_source_hash,
    )
    intake = ScriptedCandidateIntake(
        _store(tmp_path), store_id="m2-scripted-store-v1", store_hash=_hash("c")
    )
    candidate_input = ScriptedCandidatePackageInput(
        ordinal=0,
        source_package_ref={
            "candidate_source_hash": candidate_source_hash,
            "source_package_hash": _hash("d"),
            "manifest_hash": manifest_hash,
            "manifest_schema_version": "m1-candidate-source-v1",
        },
        optimization_intent="tampered package assertion",
    )

    with pytest.raises(SourceArtifactError, match="frozen Round authority"):
        intake.resolve(
            round_authority=_round(hotspot_id),
            candidate_input=candidate_input,
            baseline_source_hash=baseline_source_hash,
            profiler_evidence_uri=PROFILER_URI,
            profiler_evidence_hash=manifest.profiler_evidence_hash,
        )

    valid_input = candidate_input.model_copy(
        update={
            "source_package_ref": candidate_input.source_package_ref.model_copy(
                update={"source_package_hash": package_hash}
            )
        }
    )
    with pytest.raises(SourceArtifactError, match="frozen Round authority"):
        intake.resolve(
            round_authority=_round(uuid4()),
            candidate_input=valid_input,
            baseline_source_hash=baseline_source_hash,
            profiler_evidence_uri=PROFILER_URI,
            profiler_evidence_hash=manifest.profiler_evidence_hash,
        )


def test_scripted_candidate_input_rejects_untrusted_location_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScriptedCandidatePackageInput.model_validate(
            {
                "ordinal": 0,
                "source_package_ref": {
                    "candidate_source_hash": _hash("a"),
                    "source_package_hash": _hash("b"),
                    "manifest_hash": _hash("c"),
                    "manifest_schema_version": "m1-candidate-source-v1",
                    "uri": "file:///caller-controlled/package",
                },
                "optimization_intent": "caller tries to override Store",
            }
        )
