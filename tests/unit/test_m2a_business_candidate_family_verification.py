# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from hcuopt.adapters.business_candidate_family import (
    BusinessCandidateFamilyVerifier,
    business_candidate_source_family_hash,
)
from hcuopt.adapters.m2_candidate import candidate_source_package_hash
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.contracts.m1 import CandidateOverlayFile, CandidateSourcePackageManifest
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.m2_candidate_family_v1 import (
    BusinessCandidateFamilyManifest,
    BusinessCandidatePackageStoreDescriptor,
)
from hcuopt.deployment.m2a_business_candidate_family import (
    verify_business_candidate_family,
)
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path

FIXED_TIME = datetime(2026, 9, 2, 2, 30, tzinfo=timezone.utc)
OVERLAY_PATH = "python/sglang/kernel.py"
REPLACEMENT_POINT = "sglang.kernel.optimized"
MOUNT_TARGET = "/opt/sglang/python/sglang/kernel.py"
HOTSPOT_ID = UUID("ec941d47-206a-5f18-8538-ae9c18c1e0ec")
FIRST_CANDIDATE_ID = UUID("11111111-1111-5111-8111-111111111111")
SECOND_CANDIDATE_ID = UUID("22222222-2222-5222-8222-222222222222")


def _hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _publish_package(
    root: Path,
    baseline_root: Path,
    *,
    candidate_id: UUID,
    content: bytes,
    claimed_source_hash: str | None = None,
) -> CandidateSourcePackageRef:
    candidate_root = root.parent / f"candidate-{candidate_id}"
    shutil.copytree(baseline_root, candidate_root)
    destination = candidate_root / OVERLAY_PATH
    destination.write_bytes(content)
    candidate_source_hash = claimed_source_hash or canonical_source_hash(candidate_root)
    shutil.rmtree(candidate_root)

    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=HOTSPOT_ID,
        baseline_source_hash=canonical_source_hash(baseline_root),
        candidate_source_hash=candidate_source_hash,
        replacement_point=REPLACEMENT_POINT,
        candidate_kind="business",
        overlay_mount_target=MOUNT_TARGET,
        files=[CandidateOverlayFile(path=OVERLAY_PATH, content_hash=_hash(content))],
        profiler_evidence_uri="evidence://profiler/frozen",
        profiler_evidence_hash=_hash(b"profiler"),
        reviewed_by="candidate-reviewer",
        reviewed_at=FIXED_TIME,
    )
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_hash = _hash(manifest_bytes)
    package = root / "sha256" / candidate_source_hash[7:9] / candidate_source_hash[9:]
    source = package / "files" / OVERLAY_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    (package / "manifest.json").write_bytes(manifest_bytes)
    return CandidateSourcePackageRef(
        candidate_source_hash=candidate_source_hash,
        source_package_hash=candidate_source_package_hash(manifest_hash, manifest.files),
        manifest_hash=manifest_hash,
        manifest_schema_version=manifest.schema_version,
    )


def _fixture(
    tmp_path: Path,
    *,
    second_claimed_source_hash: str | None = None,
) -> tuple[Path, str, str]:
    baseline = tmp_path / "baseline"
    baseline_source = baseline / OVERLAY_PATH
    baseline_source.parent.mkdir(parents=True)
    baseline_source.write_bytes(b"def optimized(value):\n    return value\n")
    (baseline / "LICENSE").write_text("test license\n", encoding="utf-8")

    store_root = tmp_path / "candidate-store"
    refs = tuple(
        sorted(
            (
                _publish_package(
                    store_root,
                    baseline,
                    candidate_id=FIRST_CANDIDATE_ID,
                    content=b"def optimized(value):\n    return value + 1\n",
                ),
                _publish_package(
                    store_root,
                    baseline,
                    candidate_id=SECOND_CANDIDATE_ID,
                    content=b"def optimized(value):\n    return 1 + value\n",
                    claimed_source_hash=second_claimed_source_hash,
                ),
            ),
            key=lambda item: item.candidate_source_hash,
        )
    )
    descriptor = BusinessCandidatePackageStoreDescriptor(
        store_id="test-business-candidate-store",
        store_version=1,
        profile="test-business-family-verifier",
        baseline_repository="https://example.invalid/sglang.git",
        baseline_commit="a" * 40,
        baseline_source_hash=canonical_source_hash(baseline),
        allowed_overlay_roots=("python/sglang",),
        approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
        packages=refs,
    )
    descriptor_path = "store.json"
    descriptor_bytes = canonical_json_bytes(descriptor)
    (tmp_path / descriptor_path).write_bytes(descriptor_bytes)
    store_hash = _hash(descriptor_bytes)

    manifests = {
        package.manifest.candidate_id: package.manifest
        for package in CandidateSourcePackageStore(
            store_root,
            profile=descriptor.profile,
            allowed_overlay_roots=descriptor.allowed_overlay_roots,
            approved_mount_targets=descriptor.approved_mount_targets,
        ).list_verified()
    }
    family = BusinessCandidateFamilyManifest(
        family_id="test-business-family",
        source_package_store_id=descriptor.store_id,
        source_package_store_hash=store_hash,
        target_snapshot_id=UUID("33333333-3333-5333-8333-333333333333"),
        stage0_run_id=UUID("44444444-4444-5444-8444-444444444444"),
        baseline_epoch_id=UUID("55555555-5555-5555-8555-555555555555"),
        baseline_source_hash=descriptor.baseline_source_hash,
        workload_id="test-business-workload",
        hotspot_id=HOTSPOT_ID,
        replacement_point=REPLACEMENT_POINT,
        profiler_evidence_uri="evidence://profiler/frozen",
        profiler_evidence_hash=_hash(b"profiler"),
        overlay_mount_target=MOUNT_TARGET,
        overlay_file_path=OVERLAY_PATH,
        members=tuple(
            {
                "candidate_id": str(candidate_id),
                "source_package_ref": next(
                    item
                    for item in refs
                    if item.candidate_source_hash
                    == manifests[candidate_id].candidate_source_hash
                ).model_dump(mode="json"),
                "optimization_intent": f"exercise real source Candidate {ordinal}",
            }
            for ordinal, candidate_id in (
                (1, FIRST_CANDIDATE_ID),
                (2, SECOND_CANDIDATE_ID),
            )
        ),
        reviewed_by="family-reviewer",
        reviewed_at=FIXED_TIME,
    )
    family_path = "family.json"
    (tmp_path / family_path).write_bytes(canonical_json_bytes(family))
    return baseline, descriptor_path, family_path


def test_store_reread_and_baseline_replay_publish_acceptance(tmp_path: Path) -> None:
    baseline, descriptor_path, family_path = _fixture(tmp_path)

    result = verify_business_candidate_family(
        repository_root=tmp_path,
        baseline_root=baseline,
        store_root=tmp_path / "candidate-store",
        store_descriptor_path=descriptor_path,
        family_manifest_path=family_path,
        evidence_root=tmp_path / "evidence",
        verified_by="independent-c-verifier",
        verified_at=FIXED_TIME,
    )

    assert result.record.decision == "accepted_for_formal_window"
    assert result.record.baseline_source_replay_verified is True
    assert result.record.hcu_accessed is False
    assert result.record.performance_conclusion == "not_measured"
    assert len(result.record.members) == 2
    assert result.evidence.sha256 == _hash(file_uri_to_path(result.evidence.uri).read_bytes())


def test_claimed_candidate_source_hash_must_replay_from_baseline(tmp_path: Path) -> None:
    baseline, descriptor_path, family_path = _fixture(
        tmp_path,
        second_claimed_source_hash="sha256:" + "f" * 64,
    )

    with pytest.raises(SourceArtifactError, match="cannot be reproduced"):
        verify_business_candidate_family(
            repository_root=tmp_path,
            baseline_root=baseline,
            store_root=tmp_path / "candidate-store",
            store_descriptor_path=descriptor_path,
            family_manifest_path=family_path,
            evidence_root=tmp_path / "evidence",
            verified_by="independent-c-verifier",
            verified_at=FIXED_TIME,
        )


def test_repository_nmz36_family_is_canonical_and_store_verified() -> None:
    root = Path(__file__).resolve().parents[2]
    descriptor_path = root / "config/m2/nmz36-business-candidate-store-v1.json"
    family_path = root / "config/m2/nmz36-business-candidate-family-v1.json"
    descriptor = BusinessCandidatePackageStoreDescriptor.model_validate_json(
        descriptor_path.read_bytes()
    )
    family = BusinessCandidateFamilyManifest.model_validate_json(family_path.read_bytes())
    assert canonical_json_bytes(descriptor) == descriptor_path.read_bytes()
    assert canonical_json_bytes(family) == family_path.read_bytes()

    store = CandidateSourcePackageStore(
        root / "config/m2/nmz36-business-candidate-store-v1",
        profile=descriptor.profile,
        allowed_overlay_roots=descriptor.allowed_overlay_roots,
        approved_mount_targets=descriptor.approved_mount_targets,
    )
    verified = BusinessCandidateFamilyVerifier(
        store,
        store_id=descriptor.store_id,
        store_hash=_hash(descriptor_path.read_bytes()),
    ).verify(family)

    assert verified.source_family_hash == business_candidate_source_family_hash(family)
    assert verified.source_family_hash == (
        "sha256:a9f03a6b0a87bf1c80aa29b9eb16e04da28e759ca881af0fa12de456f15a57c1"
    )
    assert {item.manifest.candidate_source_hash for item in verified.packages} == {
        "sha256:f27c1546bc5bd46741ae98ad0b96d51974a0108af316f9d557daa2f82556fbc2",
        "sha256:a830e81d589f2199a223fbbb4afa9d74bfcbf546540259e22f32ffa02b83162a",
    }
    sources = {
        item.manifest.candidate_id: (
            item.files_root / item.manifest.files[0].path
        ).read_text(encoding="utf-8")
        for item in verified.packages
    }
    assert "torch.unique_consecutive(free_index // self.page_size)" in sources[
        UUID("de4e9427-340b-5428-bfa7-49aa52a1acae")
    ]
    assert "free_index[:: self.page_size] // self.page_size" in sources[
        UUID("fd55580b-1e03-5907-bad8-493ceacd1533")
    ]
    for candidate_id, source in sources.items():
        compile(source, f"candidate-{candidate_id}/allocator.py", "exec")


def test_page_head_candidate_preserves_full_page_release_membership() -> None:
    page_size = 4
    released_pages = (7, 2, 11)
    free_index = tuple(
        index
        for page in released_pages
        for index in range(page * page_size, (page + 1) * page_size)
    )

    baseline_pages = {index // page_size for index in free_index}
    candidate_pages = tuple(
        free_index[offset] // page_size
        for offset in range(0, len(free_index), page_size)
    )

    assert candidate_pages == released_pages
    assert set(candidate_pages) == baseline_pages
    assert len(candidate_pages) == len(set(candidate_pages))
