# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    LoadedCandidateSourcePackage,
)
from hcuopt.contracts.m1 import CandidateOverlayFile
from hcuopt.contracts.m2 import (
    RoundCandidate,
    ScriptedCandidateFixtureSpec,
    ScriptedCandidatePackageInput,
    SearchRound,
)
from hcuopt.domain.enums import (
    ManualCandidateKind,
    RoundCandidateState,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes

SHA256_VALUE = re.compile(r"^sha256:[0-9a-f]{64}$")

SCRIPTED_CANDIDATE_FIXTURES = (
    ScriptedCandidateFixtureSpec(
        fixture_id="noop",
        build_outcome="built",
        optimization_intent="exercise an equivalent source change",
    ),
    ScriptedCandidateFixtureSpec(
        fixture_id="known_faster",
        build_outcome="built",
        optimization_intent="exercise the synthetic favorable-signal path",
    ),
    ScriptedCandidateFixtureSpec(
        fixture_id="known_slower",
        build_outcome="built",
        optimization_intent="exercise the synthetic unfavorable-signal path",
    ),
    ScriptedCandidateFixtureSpec(
        fixture_id="build_failure",
        build_outcome="build_failed",
        optimization_intent="exercise immutable Build failure evidence",
        terminal_failure_code="scripted_build_failure",
    ),
)


@dataclass(frozen=True, slots=True)
class ResolvedScriptedCandidate:
    member: RoundCandidate
    package: LoadedCandidateSourcePackage


def candidate_source_package_hash(
    manifest_hash: str, files: list[CandidateOverlayFile]
) -> str:
    """Bind the original Manifest bytes and its verified file identities."""

    if SHA256_VALUE.fullmatch(manifest_hash) is None:
        raise ValueError("M2a Candidate Manifest Hash is invalid")
    value = {
        "manifest_hash": manifest_hash,
        "files": [
            {"path": item.path, "content_hash": item.content_hash}
            for item in sorted(files, key=lambda item: item.path)
        ],
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class ScriptedCandidateIntake:
    """Resolve synthetic M2a inputs through the existing trusted package Store."""

    def __init__(
        self,
        source_packages: CandidateSourcePackageStore,
        *,
        store_id: str,
        store_hash: str,
    ) -> None:
        if not store_id or store_id.strip() != store_id or len(store_id) > 300:
            raise ValueError("M2a Candidate Package Store ID is invalid")
        if SHA256_VALUE.fullmatch(store_hash) is None:
            raise ValueError("M2a Candidate Package Store Hash is invalid")
        self.source_packages = source_packages
        self.store_id = store_id
        self.store_hash = store_hash

    def resolve(
        self,
        *,
        round_authority: SearchRound,
        candidate_input: ScriptedCandidatePackageInput,
        baseline_source_hash: str,
        profiler_evidence_uri: str,
        profiler_evidence_hash: str,
    ) -> ResolvedScriptedCandidate:
        if (
            round_authority.run_mode is not SearchRoundRunMode.SCRIPTED
            or round_authority.state is not SearchRoundState.INTAKE_OPEN
        ):
            raise SourceArtifactError(
                "M2a scripted Candidate Intake requires an intake_open Scripted Round"
            )
        if candidate_input.ordinal >= round_authority.declared_candidate_count:
            raise SourceArtifactError(
                "M2a Candidate ordinal exceeds the declared Round family"
            )

        reference = candidate_input.source_package_ref
        package = self.source_packages.read(
            candidate_source_hash=reference.candidate_source_hash
        )
        manifest = package.manifest
        package_hash = candidate_source_package_hash(
            package.manifest_hash, manifest.files
        )
        expected = (
            reference.manifest_schema_version,
            reference.manifest_hash,
            reference.source_package_hash,
            round_authority.hotspot_id,
            baseline_source_hash,
            reference.candidate_source_hash,
            round_authority.replacement_point,
            ManualCandidateKind.FIXTURE,
            profiler_evidence_uri,
            profiler_evidence_hash,
        )
        actual = (
            manifest.schema_version,
            package.manifest_hash,
            package_hash,
            manifest.hotspot_id,
            manifest.baseline_source_hash,
            manifest.candidate_source_hash,
            manifest.replacement_point,
            manifest.candidate_kind,
            manifest.profiler_evidence_uri,
            manifest.profiler_evidence_hash,
        )
        if actual != expected:
            raise SourceArtifactError(
                "M2a Candidate Package does not match its frozen Round authority"
            )

        identity = {
            "round_id": str(round_authority.round_id),
            "ordinal": candidate_input.ordinal,
            "candidate_id": str(manifest.candidate_id),
            "store_id": self.store_id,
            "store_hash": self.store_hash,
            "source_package_hash": package_hash,
            "manifest_version": manifest.schema_version,
            "manifest_hash": package.manifest_hash,
            "baseline_source_hash": manifest.baseline_source_hash,
            "candidate_source_hash": manifest.candidate_source_hash,
            "hotspot_id": str(manifest.hotspot_id),
            "replacement_point": manifest.replacement_point,
            "candidate_kind": manifest.candidate_kind.value,
            "optimization_intent": candidate_input.optimization_intent,
        }
        input_digest = "sha256:" + hashlib.sha256(
            canonical_json_bytes(identity)
        ).hexdigest()
        member_id = uuid5(
            NAMESPACE_URL,
            "hcuopt:m2-round-candidate-v1:"
            f"{round_authority.round_id}:{candidate_input.ordinal}:"
            f"{manifest.candidate_id}:{input_digest}",
        )
        member = RoundCandidate(
            round_candidate_id=member_id,
            round_id=round_authority.round_id,
            candidate_id=manifest.candidate_id,
            ordinal=candidate_input.ordinal,
            source_package_store_id=self.store_id,
            source_package_store_hash=self.store_hash,
            source_package_hash=package_hash,
            source_manifest_version=manifest.schema_version,
            source_manifest_hash=package.manifest_hash,
            baseline_source_hash=manifest.baseline_source_hash,
            candidate_source_hash=manifest.candidate_source_hash,
            optimization_intent=candidate_input.optimization_intent,
            replacement_point=manifest.replacement_point,
            candidate_kind=manifest.candidate_kind,
            state=RoundCandidateState.INTAKE_ACCEPTED,
            idempotency_key=f"m2-round-candidate:{member_id}",
        )
        return ResolvedScriptedCandidate(member=member, package=package)
