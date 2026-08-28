# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from hcuopt.adapters.m2_candidate import (
    ScriptedCandidateIntake,
    candidate_source_package_hash,
)
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.operator_v1 import (
    OperatorCandidatePackageView,
    OperatorHotspotAuthorityView,
    OperatorHotspotView,
    OperatorProfileDescriptor,
    OperatorProfileRef,
    OperatorWorkloadView,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.domain.enums import ManualCandidateKind, SearchRoundRunMode
from hcuopt.domain.errors import NotFound, SourceArtifactError
from hcuopt.operator.errors import OperatorCandidatePackageInvalid
from hcuopt.operator.profiles import OperatorProfileCatalog


class OperatorDiscoveryRepository(Protocol):
    def list_scripted_operator_hotspots(
        self,
        target: TargetOperatorProfileRefs,
        workload: WorkloadOperatorProfileRefs,
    ) -> tuple[OperatorHotspotAuthorityView, ...]: ...


def _profile_ref(profile: OperatorProfileDescriptor) -> OperatorProfileRef:
    return OperatorProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_kind=profile.profile_kind,
        profile_hash=profile.profile_hash,
    )


class OperatorDiscoveryService:
    """Expose only Profile, Hotspot, and Package facts from trusted authorities."""

    def __init__(
        self,
        profiles: OperatorProfileCatalog,
        *,
        candidate_intake: ScriptedCandidateIntake | None = None,
    ) -> None:
        self.profiles = profiles
        self.candidate_intake = candidate_intake

    def workloads(self) -> list[OperatorWorkloadView]:
        return [self._workload_view(item) for item in self.profiles.list("workload")]

    def workload(self, profile_id: str, profile_version: int) -> OperatorWorkloadView:
        return self._workload_view(
            self.profiles.get("workload", profile_id, profile_version)
        )

    def hotspots(
        self,
        *,
        target_profile_id: str,
        target_profile_version: int,
        workload_profile_id: str,
        workload_profile_version: int,
        repository: OperatorDiscoveryRepository,
    ) -> list[OperatorHotspotView]:
        target_profile = self.profiles.get(
            "target", target_profile_id, target_profile_version
        )
        workload_profile = self.profiles.get(
            "workload", workload_profile_id, workload_profile_version
        )
        target_ref = _profile_ref(target_profile)
        workload_ref = _profile_ref(workload_profile)
        self.profiles.require(target_ref, SearchRoundRunMode.SCRIPTED)
        self.profiles.require(workload_ref, SearchRoundRunMode.SCRIPTED)
        target = TargetOperatorProfileRefs.model_validate(target_profile.authority_refs)
        workload = WorkloadOperatorProfileRefs.model_validate(
            workload_profile.authority_refs
        )
        packages = self._verified_packages(target)
        views: list[OperatorHotspotView] = []
        for discovered in repository.list_scripted_operator_hotspots(target, workload):
            candidate_packages = tuple(
                package
                for package in packages
                if self._package_matches(package, discovered)
            )
            views.append(
                OperatorHotspotView(
                    hotspot=discovered.hotspot,
                    target_profile=target_ref,
                    workload_profile=workload_ref,
                    baseline_epoch_id=discovered.authority.baseline_epoch_id,
                    baseline_source_hash=discovered.authority.baseline_source_hash,
                    symbol=discovered.symbol,
                    share_ratio=discovered.share_ratio,
                    opportunity_score=discovered.opportunity_score,
                    patchability=discovered.patchability,
                    candidate_packages=candidate_packages,
                )
            )
        return views

    def hotspot(
        self,
        hotspot_id: UUID,
        *,
        target_profile_id: str,
        target_profile_version: int,
        workload_profile_id: str,
        workload_profile_version: int,
        repository: OperatorDiscoveryRepository,
    ) -> OperatorHotspotView:
        views = self.hotspots(
            target_profile_id=target_profile_id,
            target_profile_version=target_profile_version,
            workload_profile_id=workload_profile_id,
            workload_profile_version=workload_profile_version,
            repository=repository,
        )
        try:
            return next(item for item in views if item.hotspot.hotspot_id == hotspot_id)
        except StopIteration as error:
            raise NotFound(f"Operator Hotspot not found: {hotspot_id}") from error

    @staticmethod
    def _workload_view(profile: OperatorProfileDescriptor) -> OperatorWorkloadView:
        return OperatorWorkloadView(
            profile=_profile_ref(profile),
            state=profile.state,
            display_name=profile.display_name,
            summary=profile.summary,
            authority_refs=WorkloadOperatorProfileRefs.model_validate(
                profile.authority_refs
            ),
            synthetic=True,
        )

    def _verified_packages(
        self, target: TargetOperatorProfileRefs
    ) -> tuple[OperatorCandidatePackageView, ...]:
        intake = self.candidate_intake
        if intake is None:
            return ()
        if (
            intake.store_id != target.candidate_package_store_id
            or intake.store_hash != target.candidate_package_store_hash
        ):
            raise OperatorCandidatePackageInvalid(
                "Operator Candidate Store Authority does not match the Target Profile"
            )
        try:
            loaded = intake.source_packages.list_verified()
        except (OSError, SourceArtifactError, ValueError) as error:
            raise OperatorCandidatePackageInvalid(
                "Operator Candidate Package Store failed immutable verification"
            ) from error
        views = []
        candidate_ids: set[UUID] = set()
        for package in loaded:
            manifest = package.manifest
            if manifest.candidate_kind is not ManualCandidateKind.FIXTURE:
                continue
            if manifest.candidate_id in candidate_ids:
                raise OperatorCandidatePackageInvalid(
                    "Operator Candidate Package Store contains a duplicate Candidate identity"
                )
            candidate_ids.add(manifest.candidate_id)
            reference = CandidateSourcePackageRef(
                candidate_source_hash=manifest.candidate_source_hash,
                source_package_hash=candidate_source_package_hash(
                    package.manifest_hash, manifest.files
                ),
                manifest_hash=package.manifest_hash,
                manifest_schema_version=manifest.schema_version,
            )
            views.append(
                OperatorCandidatePackageView(
                    candidate_id=manifest.candidate_id,
                    source_package_ref=reference,
                    reviewed_by=manifest.reviewed_by,
                    reviewed_at=manifest.reviewed_at,
                    replacement_path=manifest.files[0].path,
                    suggested_optimization_intent=(
                        "evaluate reviewed fixture Candidate "
                        f"{str(manifest.candidate_id)[:8]} for "
                        f"{manifest.replacement_point}"
                    ),
                )
            )
        return tuple(sorted(views, key=lambda item: str(item.candidate_id)))

    def _package_matches(
        self,
        package: OperatorCandidatePackageView,
        discovered: OperatorHotspotAuthorityView,
    ) -> bool:
        intake = self.candidate_intake
        assert intake is not None
        loaded = intake.source_packages.read(
            candidate_source_hash=package.source_package_ref.candidate_source_hash
        )
        manifest = loaded.manifest
        authority = discovered.authority
        return (
            manifest.hotspot_id == discovered.hotspot.hotspot_id
            and manifest.baseline_source_hash == authority.baseline_source_hash
            and manifest.replacement_point == authority.replacement_point
            and manifest.profiler_evidence_uri == authority.profiler_evidence_uri
            and manifest.profiler_evidence_hash == authority.profiler_evidence_hash
        )
