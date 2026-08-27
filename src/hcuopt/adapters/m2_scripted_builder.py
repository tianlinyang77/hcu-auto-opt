# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from hcuopt.adapters.build_cache import BuildCache
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.adapters.m2_candidate import (
    SCRIPTED_CANDIDATE_FIXTURES,
    SHA256_VALUE,
    candidate_source_package_hash,
)
from hcuopt.adapters.manual_candidate import (
    CandidateSourcePackageStore,
    LoadedCandidateSourcePackage,
    ManualOverlayCandidateBuilder,
)
from hcuopt.contracts.m2 import (
    RoundCandidate,
    RoundCandidateBuildTerminal,
    ScriptedCandidateFixtureSpec,
    SearchRound,
)
from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    SourceSnapshot,
)
from hcuopt.domain.enums import (
    ManualCandidateKind,
    RoundCandidateState,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.domain.errors import ArtifactIntegrityError, SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path

M2_SCRIPTED_OVERLAY_RECIPE_VERSION = "m2a-scripted-overlay-v1"
M2_SCRIPTED_BUILD_FAILURE_EVIDENCE_VERSION = "m2a-scripted-build-failure-v1"


@dataclass(frozen=True, slots=True)
class ScriptedCandidateBuildResult:
    fixture_id: str
    terminal: RoundCandidateBuildTerminal
    source: SourceSnapshot
    artifact: ArtifactManifest | None
    failure_evidence_uri: str | None
    adapter_provenance: tuple[AdapterProvenance, ...]
    synthetic: bool = True


class ScriptedRoundCandidateBuilder:
    """Build one frozen M2a fixture through the existing Overlay pipeline."""

    def __init__(
        self,
        source_manager: GitSourceManager,
        source_packages: CandidateSourcePackageStore,
        artifact_store: LocalArtifactStore,
        build_cache: BuildCache,
        *,
        profile: str,
        store_id: str,
        store_hash: str,
    ) -> None:
        for dependency in (source_manager, source_packages, artifact_store):
            if dependency.provenance.profile != profile:
                raise ValueError("M2a scripted build dependencies must share one profile")
        if not store_id or store_id.strip() != store_id or len(store_id) > 300:
            raise ValueError("M2a Candidate Package Store ID is invalid")
        if SHA256_VALUE.fullmatch(store_hash) is None:
            raise ValueError("M2a Candidate Package Store Hash is invalid")
        self.source_manager = source_manager
        self.source_packages = source_packages
        self.artifact_store = artifact_store
        self.build_cache = build_cache
        self.store_id = store_id
        self.store_hash = store_hash
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="candidate_builder",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def build_member(
        self,
        *,
        round_authority: SearchRound,
        member: RoundCandidate,
        baseline: SourceSnapshot,
        fixture_id: str,
        output_dir: Path,
    ) -> ScriptedCandidateBuildResult:
        fixture = self._fixture(fixture_id)
        package = self.source_packages.read(
            candidate_source_hash=member.candidate_source_hash
        )
        self._validate_bindings(round_authority, member, baseline, package)

        candidate = self.source_manager.create_candidate(
            baseline, member.candidate_id, output_dir
        )
        try:
            changed_paths = self.source_packages.apply(
                package, file_uri_to_path(candidate.worktree_uri)
            )
            source = self.source_manager.finalize_candidate(
                baseline,
                candidate,
                member.candidate_id,
                changed_paths,
                member.candidate_source_hash,
                output_dir,
            ).model_copy(
                update={
                    "snapshot_id": uuid5(
                        NAMESPACE_URL,
                        "hcuopt:m2-source:"
                        f"{member.round_candidate_id}:{member.candidate_source_hash}",
                    )
                }
            )

            if fixture.build_outcome == "build_failed":
                evidence_uri, evidence_hash = self._record_expected_failure(
                    round_authority, member, source, fixture, output_dir
                )
                return ScriptedCandidateBuildResult(
                    fixture_id=fixture.fixture_id,
                    terminal=RoundCandidateBuildTerminal(
                        round_id=round_authority.round_id,
                        round_candidate_id=member.round_candidate_id,
                        candidate_id=member.candidate_id,
                        state=RoundCandidateState.BUILD_FAILED,
                        terminal_failure_code=fixture.terminal_failure_code,
                        failure_evidence_hash=evidence_hash,
                    ),
                    source=source,
                    artifact=None,
                    failure_evidence_uri=evidence_uri,
                    adapter_provenance=tuple(self._build_provenance()),
                )

            artifact = self._build_artifact(
                round_authority, member, source, fixture, package, output_dir
            )
            return ScriptedCandidateBuildResult(
                fixture_id=fixture.fixture_id,
                terminal=RoundCandidateBuildTerminal(
                    round_id=round_authority.round_id,
                    round_candidate_id=member.round_candidate_id,
                    candidate_id=member.candidate_id,
                    state=RoundCandidateState.BUILT,
                    artifact_id=artifact.artifact_id,
                    artifact_hash=artifact.content_hash,
                ),
                source=source,
                artifact=artifact,
                failure_evidence_uri=None,
                adapter_provenance=tuple(self._provenance_chain()),
            )
        finally:
            self.source_manager.remove_candidate(baseline, candidate, output_dir)

    def _build_artifact(
        self,
        round_authority: SearchRound,
        member: RoundCandidate,
        source: SourceSnapshot,
        fixture: ScriptedCandidateFixtureSpec,
        package: LoadedCandidateSourcePackage,
        output_dir: Path,
    ) -> ArtifactManifest:
        recipe = self._recipe(round_authority, member, fixture, package)
        cache_key = self._cache_key(source, recipe)
        artifact = self.build_cache.lookup(cache_key)
        if artifact is None:
            overlay = ManualOverlayCandidateBuilder._build_overlay_artifact(
                file_uri_to_path(source.worktree_uri),
                package.manifest.files[0].path,
                package.manifest.files[0].content_hash,
                member.candidate_id,
                output_dir,
            )
            try:
                content_hash = self._sha256(overlay)
                artifact = ArtifactManifest(
                    artifact_id=uuid5(
                        NAMESPACE_URL,
                        "hcuopt:m2-artifact:"
                        f"{member.round_candidate_id}:{content_hash}",
                    ),
                    candidate_id=member.candidate_id,
                    kind="python_overlay",
                    uri=overlay.as_uri(),
                    content_hash=content_hash,
                    source_snapshot_id=source.snapshot_id,
                    build_recipe=recipe,
                    metadata={
                        "round_id": str(round_authority.round_id),
                        "round_candidate_id": str(member.round_candidate_id),
                        "candidate_family_hash": round_authority.candidate_family_hash,
                        "source_hash": source.source_hash,
                        "source_package_store_id": member.source_package_store_id,
                        "source_package_store_hash": member.source_package_store_hash,
                        "source_package_hash": member.source_package_hash,
                        "source_manifest_hash": member.source_manifest_hash,
                        "fixture_id": fixture.fixture_id,
                        "performance_conclusion": fixture.performance_conclusion,
                        "replacement_point": member.replacement_point,
                        "overlay_mount_target": package.manifest.overlay_mount_target,
                        "overlay_files": [
                            item.model_dump(mode="json")
                            for item in package.manifest.files
                        ],
                        "read_only": True,
                        "build_cache_key": cache_key,
                        "adapter_provenance": [
                            item.model_dump(mode="json")
                            for item in self._build_provenance()
                        ],
                    },
                    synthetic=True,
                )
                artifact = self.artifact_store.publish(artifact, overlay)
            finally:
                overlay.unlink(missing_ok=True)
            self.build_cache.record(cache_key, artifact)
        self._validate_artifact(
            artifact, round_authority, member, source, fixture, package, cache_key
        )
        return artifact

    def _validate_bindings(
        self,
        round_authority: SearchRound,
        member: RoundCandidate,
        baseline: SourceSnapshot,
        package: LoadedCandidateSourcePackage,
    ) -> None:
        if (
            round_authority.run_mode is not SearchRoundRunMode.SCRIPTED
            or round_authority.state is not SearchRoundState.BUILDING
            or round_authority.candidate_family_hash is None
        ):
            raise SourceArtifactError(
                "M2a build requires a Scripted Round with frozen Candidate family"
            )
        if baseline.kind != "baseline" or not baseline.clean:
            raise SourceArtifactError("M2a build requires one clean Baseline SourceSnapshot")
        manifest = package.manifest
        package_hash = candidate_source_package_hash(
            package.manifest_hash, manifest.files
        )
        expected = (
            round_authority.round_id,
            round_authority.hotspot_id,
            round_authority.replacement_point,
            baseline.source_hash,
            self.store_id,
            self.store_hash,
            package_hash,
            package.manifest_hash,
            manifest.candidate_id,
            manifest.baseline_source_hash,
            manifest.candidate_source_hash,
            manifest.replacement_point,
            ManualCandidateKind.FIXTURE,
        )
        actual = (
            member.round_id,
            manifest.hotspot_id,
            member.replacement_point,
            member.baseline_source_hash,
            member.source_package_store_id,
            member.source_package_store_hash,
            member.source_package_hash,
            member.source_manifest_hash,
            member.candidate_id,
            member.baseline_source_hash,
            member.candidate_source_hash,
            member.replacement_point,
            member.candidate_kind,
        )
        if actual != expected:
            raise SourceArtifactError(
                "M2a Build input does not match its frozen Round Candidate"
            )
        if member.state is not RoundCandidateState.INTAKE_ACCEPTED:
            raise SourceArtifactError("M2a Build accepts only intake_accepted members")

    def _validate_artifact(
        self,
        artifact: ArtifactManifest,
        round_authority: SearchRound,
        member: RoundCandidate,
        source: SourceSnapshot,
        fixture: ScriptedCandidateFixtureSpec,
        package: LoadedCandidateSourcePackage,
        cache_key: str,
    ) -> None:
        expected_metadata = {
            "round_id": str(round_authority.round_id),
            "round_candidate_id": str(member.round_candidate_id),
            "candidate_family_hash": round_authority.candidate_family_hash,
            "source_hash": source.source_hash,
            "source_package_store_id": member.source_package_store_id,
            "source_package_store_hash": member.source_package_store_hash,
            "source_package_hash": member.source_package_hash,
            "source_manifest_hash": member.source_manifest_hash,
            "fixture_id": fixture.fixture_id,
            "performance_conclusion": "not_measured",
            "replacement_point": member.replacement_point,
            "overlay_mount_target": package.manifest.overlay_mount_target,
            "overlay_files": [
                item.model_dump(mode="json") for item in package.manifest.files
            ],
            "read_only": True,
            "immutable": True,
            "build_cache_key": cache_key,
        }
        if (
            artifact.candidate_id != member.candidate_id
            or artifact.source_snapshot_id != source.snapshot_id
            or artifact.kind != "python_overlay"
            or not artifact.synthetic
            or artifact.build_recipe
            != self._recipe(round_authority, member, fixture, package)
            or any(
                artifact.metadata.get(name) != value
                for name, value in expected_metadata.items()
            )
        ):
            raise ArtifactIntegrityError(
                "cached M2a Artifact has mismatched frozen identity bindings"
            )
        path = file_uri_to_path(artifact.uri).resolve(strict=True)
        digest = artifact.content_hash.removeprefix("sha256:")
        expected_path = (
            self.artifact_store.root / "sha256" / digest[:2] / digest[2:]
        ).resolve(strict=True)
        if path != expected_path or not path.is_file() or path.stat().st_mode & 0o222:
            raise ArtifactIntegrityError(
                "cached M2a Artifact is outside the content-addressed Store"
            )
        if self._sha256(path) != artifact.content_hash:
            raise ArtifactIntegrityError("cached M2a Artifact content hash does not match")

    @staticmethod
    def _fixture(fixture_id: str) -> ScriptedCandidateFixtureSpec:
        try:
            return next(
                item for item in SCRIPTED_CANDIDATE_FIXTURES if item.fixture_id == fixture_id
            )
        except StopIteration as error:
            raise SourceArtifactError(f"unknown M2a scripted Fixture: {fixture_id}") from error

    @staticmethod
    def _recipe(
        round_authority: SearchRound,
        member: RoundCandidate,
        fixture: ScriptedCandidateFixtureSpec,
        package: LoadedCandidateSourcePackage,
    ) -> dict[str, object]:
        return {
            "name": M2_SCRIPTED_OVERLAY_RECIPE_VERSION,
            "round_id": str(round_authority.round_id),
            "round_candidate_id": str(member.round_candidate_id),
            "candidate_family_hash": round_authority.candidate_family_hash,
            "source_package_hash": member.source_package_hash,
            "package_manifest_hash": package.manifest_hash,
            "fixture_id": fixture.fixture_id,
            "artifact_format": "raw-python-source",
            "files": [item.path for item in package.manifest.files],
        }

    def _cache_key(self, source: SourceSnapshot, recipe: dict[str, object]) -> str:
        value = {
            "source_hash": source.source_hash,
            "builder": self.provenance.model_dump(mode="json"),
            "build_recipe": recipe,
        }
        return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    def _record_expected_failure(
        self,
        round_authority: SearchRound,
        member: RoundCandidate,
        source: SourceSnapshot,
        fixture: ScriptedCandidateFixtureSpec,
        output_dir: Path,
    ) -> tuple[str, str]:
        payload = {
            "schema_version": M2_SCRIPTED_BUILD_FAILURE_EVIDENCE_VERSION,
            "round_id": str(round_authority.round_id),
            "round_candidate_id": str(member.round_candidate_id),
            "candidate_id": str(member.candidate_id),
            "candidate_family_hash": round_authority.candidate_family_hash,
            "source_package_hash": member.source_package_hash,
            "source_manifest_hash": member.source_manifest_hash,
            "candidate_source_hash": source.source_hash,
            "fixture_id": fixture.fixture_id,
            "terminal_failure_code": fixture.terminal_failure_code,
            "synthetic": True,
            "performance_conclusion": "not_measured",
        }
        encoded = canonical_json_bytes(payload)
        evidence_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
        digest = evidence_hash.removeprefix("sha256:")
        destination = (
            output_dir.resolve()
            / "failure-evidence"
            / "sha256"
            / digest[:2]
            / f"{digest[2:]}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != encoded:
                raise ArtifactIntegrityError(
                    "M2a failure evidence Store contains conflicting content"
                )
        else:
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".failure-", dir=destination.parent, delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(encoded)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                try:
                    os.link(temporary_path, destination)
                except FileExistsError:
                    if destination.read_bytes() != encoded:
                        raise ArtifactIntegrityError(
                            "M2a failure evidence Store contains conflicting content"
                        ) from None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        destination.chmod(0o444)
        return destination.as_uri(), evidence_hash

    def _build_provenance(self) -> list[AdapterProvenance]:
        return [
            self.source_manager.provenance,
            self.source_packages.provenance,
            self.provenance,
        ]

    def _provenance_chain(self) -> list[AdapterProvenance]:
        return [*self._build_provenance(), self.artifact_store.provenance]

    @staticmethod
    def _sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"
