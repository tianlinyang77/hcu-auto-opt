# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from hcuopt.adapters.m2_candidate import candidate_source_package_hash
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    LoadedCandidateSourcePackage,
)
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.domain.enums import ManualCandidateKind
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes


def business_candidate_source_family_document(
    manifest: BusinessCandidateFamilyManifest,
) -> dict:
    """Return the pre-Round source-family commitment in canonical member order."""

    value = manifest.model_dump(mode="json")
    value["members"] = sorted(value["members"], key=lambda item: item["candidate_id"])
    return value


def business_candidate_source_family_hash(
    manifest: BusinessCandidateFamilyManifest,
) -> str:
    """Hash C-owned package inputs without inventing Round-specific identities."""

    document = business_candidate_source_family_document(manifest)
    return "sha256:" + hashlib.sha256(canonical_json_bytes(document)).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedBusinessCandidateFamily:
    manifest: BusinessCandidateFamilyManifest
    source_family_hash: str
    packages: tuple[LoadedCandidateSourcePackage, ...]


class BusinessCandidateFamilyVerifier:
    """Reread a deployment-owned Store and fail closed on any Family drift."""

    def __init__(
        self,
        source_packages: CandidateSourcePackageStore,
        *,
        store_id: str,
        store_hash: str,
    ) -> None:
        if not store_id or store_id.strip() != store_id or len(store_id) > 300:
            raise ValueError("M2a business Candidate Store ID is invalid")
        if not store_hash.startswith("sha256:") or len(store_hash) != 71:
            raise ValueError("M2a business Candidate Store Hash is invalid")
        try:
            int(store_hash.removeprefix("sha256:"), 16)
        except ValueError as error:
            raise ValueError("M2a business Candidate Store Hash is invalid") from error
        self.source_packages = source_packages
        self.store_id = store_id
        self.store_hash = store_hash

    def verify(
        self,
        manifest: BusinessCandidateFamilyManifest,
    ) -> VerifiedBusinessCandidateFamily:
        if (
            manifest.source_package_store_id != self.store_id
            or manifest.source_package_store_hash != self.store_hash
        ):
            raise SourceArtifactError(
                "business Candidate Family does not match the deployment Store authority"
            )

        packages: list[LoadedCandidateSourcePackage] = []
        for member in sorted(manifest.members, key=lambda item: str(item.candidate_id)):
            reference = member.source_package_ref
            package = self.source_packages.read(
                candidate_source_hash=reference.candidate_source_hash
            )
            source = package.manifest
            package_hash = candidate_source_package_hash(
                package.manifest_hash,
                source.files,
            )
            expected = (
                member.candidate_id,
                reference.manifest_schema_version,
                reference.manifest_hash,
                reference.source_package_hash,
                manifest.hotspot_id,
                manifest.baseline_source_hash,
                reference.candidate_source_hash,
                manifest.replacement_point,
                ManualCandidateKind.BUSINESS,
                manifest.profiler_evidence_uri,
                manifest.profiler_evidence_hash,
                manifest.overlay_mount_target,
                manifest.overlay_file_path,
            )
            actual = (
                source.candidate_id,
                source.schema_version,
                package.manifest_hash,
                package_hash,
                source.hotspot_id,
                source.baseline_source_hash,
                source.candidate_source_hash,
                source.replacement_point,
                source.candidate_kind,
                source.profiler_evidence_uri,
                source.profiler_evidence_hash,
                source.overlay_mount_target,
                source.files[0].path,
            )
            if actual != expected:
                raise SourceArtifactError(
                    "business Candidate Package does not match its frozen Family authority"
                )
            packages.append(package)

        return VerifiedBusinessCandidateFamily(
            manifest=manifest,
            source_family_hash=business_candidate_source_family_hash(manifest),
            packages=tuple(packages),
        )
