# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bind a frozen Formal business member to the existing real Overlay builder."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hcuopt.adapters.m2_candidate import candidate_source_package_hash
from hcuopt.adapters.manual_candidate import ManualOverlayCandidateBuilder
from hcuopt.contracts.m2 import RoundCandidate, RoundCandidateBuildTerminal, SearchRound
from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.contracts.v1 import ManualCandidateBuildResult
from hcuopt.domain.enums import RoundCandidateState, SearchRoundState
from hcuopt.domain.errors import SourceArtifactError


@dataclass(frozen=True)
class FormalCandidateBuildResult:
    build: ManualCandidateBuildResult
    terminal: RoundCandidateBuildTerminal


class FormalRoundCandidateBuilder:
    """Deployment-only, disabled by default. Does not grant authority or write DB state."""

    def __init__(
        self, builder: ManualOverlayCandidateBuilder, *, store_id: str,
        store_hash: str, enabled: bool = False,
    ) -> None:
        self.builder = builder
        self.store_id, self.store_hash, self.enabled = store_id, store_hash, enabled

    def build_member(
        self, *, round_authority: SearchRound, member: RoundCandidate,
        baseline: SourceSnapshot, hotspot: Mapping[str, Any],
        hotspot_intake_hash: str, output_dir: Path,
    ) -> FormalCandidateBuildResult:
        if not self.enabled:
            raise SourceArtifactError("Formal candidate builder is disabled")
        round_authority = SearchRound.model_validate(round_authority.model_dump(mode="json"))
        member = RoundCandidate.model_validate(member.model_dump(mode="json"))
        baseline = SourceSnapshot.model_validate(baseline.model_dump(mode="json"))
        if (
            round_authority.run_mode != "formal"
            or round_authority.state not in {
                SearchRoundState.INTAKE_CLOSED, SearchRoundState.BUILDING
            }
            or not round_authority.candidate_family_hash
            or round_authority.artifact_family_hash is not None
            or member.round_id != round_authority.round_id
            or member.candidate_kind != "business"
            or member.state != RoundCandidateState.INTAKE_ACCEPTED
            or member.artifact_id is not None
            or member.terminal_failure_code is not None
            or member.replacement_point != round_authority.replacement_point
            or baseline.kind != "baseline" or not baseline.clean
            or baseline.source_hash != member.baseline_source_hash
            or member.source_package_store_id != self.store_id
            or member.source_package_store_hash != self.store_hash
            or self.builder.provenance.profile != round_authority.adapter_profile
        ):
            raise SourceArtifactError("Formal build inputs differ from frozen business intake")
        package = self.builder.source_packages.read(
            candidate_source_hash=member.candidate_source_hash
        )
        manifest = package.manifest
        if (
            package.manifest_hash != member.source_manifest_hash
            or candidate_source_package_hash(package.manifest_hash, manifest.files)
            != member.source_package_hash
            or manifest.candidate_id != member.candidate_id
            or manifest.hotspot_id != round_authority.hotspot_id
            or manifest.candidate_kind != "business"
        ):
            raise SourceArtifactError("Formal build source package differs from frozen intake")
        result = self.builder.build_candidate(
            {
                "candidate_id": str(member.candidate_id),
                "hotspot_id": str(round_authority.hotspot_id),
                "baseline_source": baseline.model_dump(mode="json"),
                "candidate_source_hash": member.candidate_source_hash,
                "replacement_point": member.replacement_point,
                "candidate_kind": "business",
                "hotspot_intake_hash": hotspot_intake_hash,
                "hotspot": dict(hotspot),
            }, output_dir,
        )
        result = ManualCandidateBuildResult.model_validate(result.model_dump(mode="json"))
        if (
            result.candidate_id != member.candidate_id
            or result.source.source_hash != member.candidate_source_hash
            or result.artifact.synthetic
            or result.artifact.metadata.get("package_manifest_hash") != member.source_manifest_hash
            or any(p.profile != round_authority.adapter_profile for p in result.adapter_provenance)
        ):
            raise SourceArtifactError("Formal build result differs from frozen intake")
        return FormalCandidateBuildResult(
            build=result,
            terminal=RoundCandidateBuildTerminal(
                round_id=round_authority.round_id,
                round_candidate_id=member.round_candidate_id,
                candidate_id=member.candidate_id,
                state=RoundCandidateState.BUILT,
                artifact_id=result.artifact.artifact_id,
                artifact_hash=result.artifact.content_hash,
            ),
        )
