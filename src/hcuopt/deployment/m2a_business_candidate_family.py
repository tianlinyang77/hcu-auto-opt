"""Independently verify a frozen M2a business Candidate Family without HCU access."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from hcuopt.adapters.business_candidate_family import BusinessCandidateFamilyVerifier
from hcuopt.adapters.m2_candidate import candidate_source_package_hash
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    LoadedCandidateSourcePackage,
)
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.m2_candidate_family_v1 import (
    BusinessCandidateFamilyManifest,
    BusinessCandidateFamilyVerificationRecord,
    BusinessCandidatePackageStoreDescriptor,
    VerifiedBusinessCandidatePackage,
)
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import (
    EvidenceArtifact,
    canonical_json_bytes,
    write_evidence,
)
from hcuopt.source_hash import canonical_source_hash

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class PublishedBusinessCandidateFamilyVerification:
    record: BusinessCandidateFamilyVerificationRecord
    evidence: EvidenceArtifact


def _sha256(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _resolve_repository_path(repository_root: Path, relative_path: str) -> Path:
    root = repository_root.resolve(strict=True)
    candidate = root / relative_path
    if candidate.is_symlink():
        raise SourceArtifactError(f"M2a verification input cannot be a symlink: {relative_path}")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SourceArtifactError(
            f"M2a verification input escapes the repository: {relative_path}"
        ) from error
    return resolved


def _load_canonical_model(path: Path, model_type: type[ModelT]) -> tuple[ModelT, str]:
    if path.is_symlink() or not path.is_file():
        raise SourceArtifactError(f"M2a verification input is not a regular file: {path}")
    encoded = path.read_bytes()
    model = model_type.model_validate_json(encoded)
    if canonical_json_bytes(model) != encoded:
        raise SourceArtifactError(f"M2a verification input is not canonical JSON: {path}")
    return model, _sha256(encoded)


def _package_ref(package: LoadedCandidateSourcePackage) -> CandidateSourcePackageRef:
    return CandidateSourcePackageRef(
        candidate_source_hash=package.manifest.candidate_source_hash,
        source_package_hash=candidate_source_package_hash(
            package.manifest_hash,
            package.manifest.files,
        ),
        manifest_hash=package.manifest_hash,
        manifest_schema_version=package.manifest.schema_version,
    )


def _verify_baseline_replay(
    baseline_root: Path,
    *,
    expected_baseline_hash: str,
    packages: tuple[LoadedCandidateSourcePackage, ...],
) -> None:
    if baseline_root.is_symlink() or not baseline_root.is_dir():
        raise SourceArtifactError("M2a Baseline source must be a regular directory")
    baseline_root = baseline_root.resolve(strict=True)
    if canonical_source_hash(baseline_root) != expected_baseline_hash:
        raise SourceArtifactError("M2a Baseline source Hash drifted")

    with tempfile.TemporaryDirectory(prefix="hcuopt-m2a-family-replay-") as temporary:
        replay_root = Path(temporary)
        for package in packages:
            candidate_root = replay_root / str(package.manifest.candidate_id)
            shutil.copytree(baseline_root, candidate_root, symlinks=True)
            for item in package.manifest.files:
                source = package.files_root / item.path
                destination = candidate_root / item.path
                if destination.is_symlink() or not destination.is_file():
                    raise SourceArtifactError(
                        f"M2a Candidate replaces a missing Baseline file: {item.path}"
                    )
                destination.write_bytes(source.read_bytes())
            if canonical_source_hash(candidate_root) != package.manifest.candidate_source_hash:
                raise SourceArtifactError(
                    "M2a Candidate source Hash cannot be reproduced from Baseline and Overlay"
                )


def verify_business_candidate_family(
    *,
    repository_root: Path,
    baseline_root: Path,
    store_root: Path,
    store_descriptor_path: str,
    family_manifest_path: str,
    evidence_root: Path,
    verified_by: str,
    verified_at: datetime,
) -> PublishedBusinessCandidateFamilyVerification:
    """Reread Store bytes, replay source hashes, and publish immutable C evidence."""

    descriptor_file = _resolve_repository_path(repository_root, store_descriptor_path)
    family_file = _resolve_repository_path(repository_root, family_manifest_path)
    descriptor, descriptor_hash = _load_canonical_model(
        descriptor_file,
        BusinessCandidatePackageStoreDescriptor,
    )
    family, family_manifest_hash = _load_canonical_model(
        family_file,
        BusinessCandidateFamilyManifest,
    )
    if (
        family.source_package_store_id != descriptor.store_id
        or family.source_package_store_hash != descriptor_hash
        or family.baseline_source_hash != descriptor.baseline_source_hash
    ):
        raise SourceArtifactError(
            "M2a Candidate Family does not match its Store or Baseline descriptor"
        )

    if store_root.is_symlink() or not store_root.is_dir():
        raise SourceArtifactError("M2a Candidate Package Store root is not a directory")
    store_root = store_root.resolve(strict=True)
    source_packages = CandidateSourcePackageStore(
        store_root,
        profile=descriptor.profile,
        allowed_overlay_roots=descriptor.allowed_overlay_roots,
        approved_mount_targets=descriptor.approved_mount_targets,
    )
    stored_packages = source_packages.list_verified()
    stored_refs = tuple(
        sorted(
            (_package_ref(item) for item in stored_packages),
            key=lambda item: item.candidate_source_hash,
        )
    )
    declared_refs = tuple(
        sorted(descriptor.packages, key=lambda item: item.candidate_source_hash)
    )
    if stored_refs != declared_refs:
        raise SourceArtifactError(
            "M2a Candidate Package Store bytes differ from its frozen descriptor"
        )
    family_refs = tuple(
        sorted(
            (item.source_package_ref for item in family.members),
            key=lambda item: item.candidate_source_hash,
        )
    )
    if family_refs != declared_refs:
        raise SourceArtifactError(
            "M2a Candidate Family members differ from the frozen Store descriptor"
        )

    verifier = BusinessCandidateFamilyVerifier(
        source_packages,
        store_id=descriptor.store_id,
        store_hash=descriptor_hash,
    )
    verified = verifier.verify(family)
    _verify_baseline_replay(
        baseline_root,
        expected_baseline_hash=descriptor.baseline_source_hash,
        packages=verified.packages,
    )

    members = []
    for package in verified.packages:
        source = package.manifest
        members.append(
            VerifiedBusinessCandidatePackage(
                candidate_id=source.candidate_id,
                source_package_ref=_package_ref(package),
                overlay_file_path=source.files[0].path,
                overlay_content_hash=source.files[0].content_hash,
                reviewed_by=source.reviewed_by,
                reviewed_at=source.reviewed_at,
            )
        )
    record = BusinessCandidateFamilyVerificationRecord(
        store_descriptor_path=store_descriptor_path,
        store_descriptor_hash=descriptor_hash,
        family_manifest_path=family_manifest_path,
        family_manifest_hash=family_manifest_hash,
        family_id=family.family_id,
        source_family_hash=verified.source_family_hash,
        source_package_store_id=descriptor.store_id,
        source_package_store_hash=descriptor_hash,
        baseline_repository=descriptor.baseline_repository,
        baseline_commit=descriptor.baseline_commit,
        baseline_source_hash=descriptor.baseline_source_hash,
        target_snapshot_id=family.target_snapshot_id,
        stage0_run_id=family.stage0_run_id,
        baseline_epoch_id=family.baseline_epoch_id,
        hotspot_id=family.hotspot_id,
        workload_id=family.workload_id,
        replacement_point=family.replacement_point,
        members=tuple(members),
        verifier_provenance=verifier.provenance,
        verified_by=verified_by,
        verified_at=verified_at,
    )
    encoded = canonical_json_bytes(record)
    digest = _sha256(encoded)
    destination = (
        evidence_root.resolve()
        / "m2a-business-candidate-family"
        / f"sha256-{digest.removeprefix('sha256:')}.json"
    )
    evidence = write_evidence(destination, record)
    return PublishedBusinessCandidateFamilyVerification(record=record, evidence=evidence)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("verification time must include a UTC offset")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a real M2a business Candidate Family without HCU access"
    )
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--baseline-root", required=True, type=Path)
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--store-descriptor", required=True)
    parser.add_argument("--family-manifest", required=True)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--verified-by", required=True)
    parser.add_argument("--verified-at", required=True, type=_parse_time)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify_business_candidate_family(
        repository_root=args.repository_root,
        baseline_root=args.baseline_root,
        store_root=args.store_root,
        store_descriptor_path=args.store_descriptor,
        family_manifest_path=args.family_manifest,
        evidence_root=args.evidence_root,
        verified_by=args.verified_by,
        verified_at=args.verified_at,
    )
    print(
        json.dumps(
            {
                "decision": result.record.decision,
                "source_family_hash": result.record.source_family_hash,
                "evidence_uri": result.evidence.uri,
                "evidence_hash": result.evidence.sha256,
                "hcu_accessed": result.record.hcu_accessed,
                "performance_conclusion": result.record.performance_conclusion,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
