# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.adapters.business_candidate_family import (
    BusinessCandidateFamilyVerifier,
    business_candidate_source_family_hash,
)
from hcuopt.adapters.m2_candidate import candidate_source_package_hash
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes

REPLACEMENT_POINT = "sglang.srt.mem_cache.allocator.PagedTokenToKVPoolAllocator.free"
OVERLAY_PATH = "sglang/srt/mem_cache/allocator.py"
MOUNT_TARGET = "/opt/hcuopt/overlay/sglang/srt/mem_cache/allocator.py"
PROFILER_URI = "evidence:///m1/profiler/allocator-prefill.json"
STORE_ID = "nmz36-business-candidate-store-v1"
FIXED_TIME = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
HOTSPOT_ID = UUID("ec941d47-206a-5f18-8538-ae9c18c1e0ec")


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _publish_package(
    root: Path,
    *,
    candidate_id: UUID,
    candidate_source_hash: str,
    baseline_source_hash: str,
    content: bytes,
    candidate_kind: str = "business",
) -> tuple[CandidateSourcePackageManifest, dict]:
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=HOTSPOT_ID,
        baseline_source_hash=baseline_source_hash,
        candidate_source_hash=candidate_source_hash,
        replacement_point=REPLACEMENT_POINT,
        candidate_kind=candidate_kind,
        overlay_mount_target=MOUNT_TARGET,
        files=[{"path": OVERLAY_PATH, "content_hash": content_hash}],
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=_hash("9"),
        reviewed_by="candidate-family-test-reviewer",
        reviewed_at=FIXED_TIME,
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
    return manifest, {
        "candidate_id": candidate_id,
        "source_package_ref": {
            "candidate_source_hash": candidate_source_hash,
            "source_package_hash": package_hash,
            "manifest_hash": manifest_hash,
            "manifest_schema_version": manifest.schema_version,
        },
        "optimization_intent": f"evaluate business Candidate {str(candidate_id)[:8]}",
    }


def _store(root: Path) -> CandidateSourcePackageStore:
    return CandidateSourcePackageStore(
        root,
        profile="nmz36-m2a-business-v1",
        allowed_overlay_roots=("sglang",),
        approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
    )


def _family(
    tmp_path: Path,
    *,
    second_kind: str = "business",
    second_content: bytes = b"def free_v2(value):\n    return value + 0\n",
):
    baseline_source_hash = _hash("a")
    first_manifest, first = _publish_package(
        tmp_path,
        candidate_id=UUID("00000000-0000-0000-0000-000000000011"),
        candidate_source_hash=_hash("b"),
        baseline_source_hash=baseline_source_hash,
        content=b"def free_v1(value):\n    return value\n",
    )
    _second_manifest, second = _publish_package(
        tmp_path,
        candidate_id=UUID("00000000-0000-0000-0000-000000000022"),
        candidate_source_hash=_hash("c"),
        baseline_source_hash=baseline_source_hash,
        content=second_content,
        candidate_kind=second_kind,
    )
    manifest = BusinessCandidateFamilyManifest(
        family_id="nmz36-allocator-prefill-family-v1",
        source_package_store_id=STORE_ID,
        source_package_store_hash=_hash("d"),
        target_snapshot_id=UUID("ffb9f94e-2035-5ff6-9b06-f1b5fb163196"),
        stage0_run_id=UUID("dd50c381-75dd-5a64-9211-640c602dc817"),
        baseline_epoch_id=UUID("1ab0480c-6f16-544c-a835-655599eaea6c"),
        baseline_source_hash=baseline_source_hash,
        hotspot_id=HOTSPOT_ID,
        replacement_point=REPLACEMENT_POINT,
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=first_manifest.profiler_evidence_hash,
        overlay_mount_target=MOUNT_TARGET,
        overlay_file_path=OVERLAY_PATH,
        members=(first, second),
        reviewed_by="candidate-family-test-reviewer",
        reviewed_at=FIXED_TIME,
    )
    return manifest, first, second


def _verifier(root: Path) -> BusinessCandidateFamilyVerifier:
    return BusinessCandidateFamilyVerifier(
        _store(root),
        store_id=STORE_ID,
        store_hash=_hash("d"),
    )


def test_business_family_verifies_two_real_packages_and_hashes_order_independently(
    tmp_path: Path,
) -> None:
    manifest, _first, _second = _family(tmp_path)

    verified = _verifier(tmp_path).verify(manifest)
    reordered = BusinessCandidateFamilyManifest.model_validate(
        {
            **manifest.model_dump(mode="json"),
            "members": list(reversed(manifest.model_dump(mode="json")["members"])),
        }
    )

    assert len(verified.packages) == 2
    assert verified.source_family_hash == business_candidate_source_family_hash(reordered)
    assert tuple(
        str(item.manifest.candidate_id) for item in verified.packages
    ) == tuple(sorted(str(item.candidate_id) for item in manifest.members))
    assert all(
        item.manifest.candidate_kind.value == "business" for item in verified.packages
    )


def test_business_family_rejects_fixture_package(tmp_path: Path) -> None:
    manifest, _first, _second = _family(tmp_path, second_kind="fixture")

    with pytest.raises(SourceArtifactError, match="frozen Family authority"):
        _verifier(tmp_path).verify(manifest)


def test_business_family_rejects_tampered_package_file(tmp_path: Path) -> None:
    manifest, _first, second = _family(tmp_path)
    digest = second["source_package_ref"]["candidate_source_hash"].removeprefix(
        "sha256:"
    )
    source = tmp_path / "sha256" / digest[:2] / digest[2:] / "files" / OVERLAY_PATH
    source.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(SourceArtifactError, match="hash mismatch"):
        _verifier(tmp_path).verify(manifest)


def test_business_family_rejects_distinct_packages_with_duplicate_overlay_content(
    tmp_path: Path,
) -> None:
    shared_content = b"def free_v1(value):\n    return value\n"
    manifest, first, second = _family(tmp_path, second_content=shared_content)

    assert first["candidate_id"] != second["candidate_id"]
    assert first["source_package_ref"] != second["source_package_ref"]
    with pytest.raises(SourceArtifactError, match="duplicate Overlay source content"):
        _verifier(tmp_path).verify(manifest)


def test_business_family_rejects_package_reference_or_authority_drift(
    tmp_path: Path,
) -> None:
    manifest, _first, _second = _family(tmp_path)
    changed_members = list(manifest.model_dump(mode="json")["members"])
    changed_members[1]["source_package_ref"]["source_package_hash"] = _hash("e")
    drifted = BusinessCandidateFamilyManifest.model_validate(
        {**manifest.model_dump(mode="json"), "members": changed_members}
    )

    with pytest.raises(SourceArtifactError, match="frozen Family authority"):
        _verifier(tmp_path).verify(drifted)
    with pytest.raises(SourceArtifactError, match="Store authority"):
        _verifier(tmp_path).verify(
            manifest.model_copy(update={"source_package_store_hash": _hash("e")})
        )


def test_business_family_contract_requires_two_distinct_members(tmp_path: Path) -> None:
    manifest, first, _second = _family(tmp_path)
    raw = manifest.model_dump(mode="json")

    with pytest.raises(ValidationError):
        BusinessCandidateFamilyManifest.model_validate({**raw, "members": [first]})
    with pytest.raises(ValidationError, match="duplicate Candidate identity"):
        BusinessCandidateFamilyManifest.model_validate({**raw, "members": [first, first]})
