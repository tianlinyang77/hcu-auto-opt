from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.adapters.build_cache import BuildCache
from hcuopt.adapters.git_source import GitSourceManager
from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.platform_v1 import AdapterProvenance, ArtifactManifest, SourceSnapshot
from hcuopt.contracts.v1 import ManualCandidateBuildResult
from hcuopt.domain.enums import ManualCandidateKind
from hcuopt.domain.errors import ArtifactIntegrityError, SourceArtifactError
from hcuopt.source_hash import file_uri_to_path

M1_OVERLAY_RECIPE_VERSION = "m1-python-triton-overlay-v1"


@dataclass(frozen=True, slots=True)
class LoadedCandidateSourcePackage:
    manifest: CandidateSourcePackageManifest
    manifest_hash: str
    files_root: Path


class CandidateSourcePackageStore:
    """Read reviewed Candidate inputs from a deployment-owned immutable root."""

    def __init__(
        self,
        root: Path,
        *,
        profile: str,
        allowed_overlay_roots: tuple[str, ...],
        approved_mount_targets: Mapping[str, str],
    ) -> None:
        if not allowed_overlay_roots:
            raise ValueError("at least one deployment-approved overlay root is required")
        self.root = root.resolve()
        self.allowed_overlay_roots = tuple(
            self._normalize_root(value) for value in allowed_overlay_roots
        )
        self.approved_mount_targets = dict(approved_mount_targets)
        if not self.approved_mount_targets:
            raise ValueError("at least one approved replacement point is required")
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="candidate_source_intake",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def load(
        self,
        *,
        candidate_id: UUID,
        hotspot_id: UUID,
        baseline_source_hash: str,
        candidate_source_hash: str,
        replacement_point: str,
        candidate_kind: ManualCandidateKind,
    ) -> LoadedCandidateSourcePackage:
        package_dir = self._package_dir(candidate_source_hash)
        if package_dir.is_symlink() or not package_dir.is_dir():
            raise SourceArtifactError(
                f"trusted Candidate source package does not exist: {package_dir}"
            )
        manifest_path = package_dir / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SourceArtifactError(
                f"Candidate source package has no regular manifest: {manifest_path}"
            )
        manifest_bytes = manifest_path.read_bytes()
        manifest = CandidateSourcePackageManifest.model_validate_json(manifest_bytes)
        expected = (
            candidate_id,
            hotspot_id,
            baseline_source_hash,
            candidate_source_hash,
            replacement_point,
            candidate_kind,
        )
        actual = (
            manifest.candidate_id,
            manifest.hotspot_id,
            manifest.baseline_source_hash,
            manifest.candidate_source_hash,
            manifest.replacement_point,
            manifest.candidate_kind,
        )
        if actual != expected:
            raise SourceArtifactError(
                "trusted Candidate source manifest does not match the durable M1 Job"
            )
        approved_target = self.approved_mount_targets.get(manifest.replacement_point)
        if approved_target != manifest.overlay_mount_target:
            raise SourceArtifactError(
                "Candidate source manifest names an unapproved Overlay mount target"
            )

        files_root = package_dir / "files"
        if files_root.is_symlink() or not files_root.is_dir():
            raise SourceArtifactError("Candidate source package has no regular files directory")
        for item in manifest.files:
            self._require_allowed_path(item.path)
            source = self._resolve_package_file(files_root, item.path)
            actual_hash = self._sha256(source)
            if actual_hash != item.content_hash:
                raise SourceArtifactError(
                    f"reviewed replacement hash mismatch for {item.path}: "
                    f"expected {item.content_hash}, got {actual_hash}"
                )
        return LoadedCandidateSourcePackage(
            manifest=manifest,
            manifest_hash=f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
            files_root=files_root,
        )

    def apply(self, package: LoadedCandidateSourcePackage, candidate_root: Path) -> tuple[str, ...]:
        candidate_root = candidate_root.resolve(strict=True)
        changed: list[str] = []
        for item in package.manifest.files:
            source = self._resolve_package_file(package.files_root, item.path)
            destination = candidate_root / item.path
            if destination.is_symlink() or not destination.is_file():
                raise SourceArtifactError(
                    f"M1 Overlay may replace only an existing regular source file: {item.path}"
                )
            resolved_destination = destination.resolve(strict=True)
            if candidate_root not in resolved_destination.parents:
                raise SourceArtifactError(f"replacement escapes Candidate Worktree: {item.path}")
            mode = stat.S_IMODE(destination.stat(follow_symlinks=False).st_mode)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=f".{destination.name}.", dir=destination.parent, delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    with source.open("rb") as replacement:
                        while chunk := replacement.read(1024 * 1024):
                            temporary.write(chunk)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                temporary_path.chmod(mode)
                os.replace(temporary_path, destination)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            changed.append(item.path)
        return tuple(sorted(changed))

    def _package_dir(self, source_hash: str) -> Path:
        digest = source_hash.removeprefix("sha256:")
        if not source_hash.startswith("sha256:") or len(digest) != 64:
            raise SourceArtifactError(f"invalid Candidate source hash: {source_hash}")
        try:
            int(digest, 16)
        except ValueError as error:
            raise SourceArtifactError(
                f"invalid Candidate source hash: {source_hash}"
            ) from error
        return self.root / "sha256" / digest[:2] / digest[2:]

    def _require_allowed_path(self, path: str) -> None:
        allowed = any(
            path == root[:-1] or path.startswith(root)
            for root in self.allowed_overlay_roots
        )
        if not allowed:
            raise SourceArtifactError(
                f"replacement path is outside deployment-approved Overlay roots: {path}"
            )

    @staticmethod
    def _normalize_root(value: str) -> str:
        normalized = value.strip("/")
        if not normalized or "\\" in normalized or ".." in normalized.split("/"):
            raise ValueError(f"invalid deployment-approved overlay root: {value}")
        return f"{normalized}/"

    @staticmethod
    def _resolve_package_file(files_root: Path, relative_path: str) -> Path:
        path = files_root / relative_path
        if path.is_symlink() or not path.is_file():
            raise SourceArtifactError(
                f"reviewed replacement is not a regular file: {relative_path}"
            )
        resolved = path.resolve(strict=True)
        root = files_root.resolve(strict=True)
        if root not in resolved.parents:
            raise SourceArtifactError(
                f"reviewed replacement escapes the trusted package: {relative_path}"
            )
        return resolved

    @staticmethod
    def _sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"


class ManualOverlayCandidateBuilder:
    """Build a minimal immutable startup Overlay from reviewed source replacements."""

    def __init__(
        self,
        source_manager: GitSourceManager,
        source_packages: CandidateSourcePackageStore,
        artifact_store: LocalArtifactStore,
        build_cache: BuildCache,
        *,
        profile: str,
    ) -> None:
        for dependency in (source_manager, source_packages, artifact_store):
            if dependency.provenance.profile != profile:
                raise ValueError("M1 Candidate builder dependencies must share one profile")
        self.source_manager = source_manager
        self.source_packages = source_packages
        self.artifact_store = artifact_store
        self.build_cache = build_cache
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="candidate_builder",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def build_candidate(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> ManualCandidateBuildResult:
        candidate_id = UUID(str(payload["candidate_id"]))
        hotspot_id = UUID(str(payload["hotspot_id"]))
        baseline = SourceSnapshot.model_validate(payload["baseline_source"])
        candidate_source_hash = str(payload["candidate_source_hash"])
        replacement_point = str(payload["replacement_point"])
        candidate_kind = ManualCandidateKind(str(payload["candidate_kind"]))
        package = self.source_packages.load(
            candidate_id=candidate_id,
            hotspot_id=hotspot_id,
            baseline_source_hash=baseline.source_hash,
            candidate_source_hash=candidate_source_hash,
            replacement_point=replacement_point,
            candidate_kind=candidate_kind,
        )

        candidate = self.source_manager.create_candidate(
            baseline, candidate_id, output_dir
        )
        try:
            changed_paths = self.source_packages.apply(
                package, file_uri_to_path(candidate.worktree_uri)
            )
            source = self.source_manager.finalize_candidate(
                baseline,
                candidate,
                candidate_id,
                changed_paths,
                candidate_source_hash,
                output_dir,
            )
            source = source.model_copy(
                update={
                    "snapshot_id": uuid5(
                        NAMESPACE_URL,
                        f"hcuopt:m1-source:{candidate_id}:{candidate_source_hash}",
                    )
                }
            )
            recipe = self._recipe(package)
            cache_key = self.cache_key(source, recipe)
            artifact = self.build_cache.lookup(cache_key)
            if artifact is None:
                built_path = self._build_overlay_artifact(
                    package, candidate_id, output_dir
                )
                artifact = ArtifactManifest(
                    artifact_id=uuid5(
                        NAMESPACE_URL,
                        f"hcuopt:m1-artifact:{candidate_id}:{self._sha256(built_path)}",
                    ),
                    candidate_id=candidate_id,
                    kind="python_overlay",
                    uri=built_path.as_uri(),
                    content_hash=self._sha256(built_path),
                    source_snapshot_id=source.snapshot_id,
                    build_recipe=recipe,
                    metadata={
                        "source_hash": source.source_hash,
                        "hotspot_id": str(hotspot_id),
                        "replacement_point": replacement_point,
                        "overlay_mount_target": package.manifest.overlay_mount_target,
                        "candidate_kind": candidate_kind.value,
                        "package_manifest_hash": package.manifest_hash,
                        "overlay_files": [
                            item.model_dump(mode="json") for item in package.manifest.files
                        ],
                        "read_only": True,
                        "build_cache_key": cache_key,
                        "adapter_provenance": [
                            item.model_dump(mode="json")
                            for item in self._provenance_chain()
                            if item.capability != "artifact_store"
                        ],
                    },
                    synthetic=False,
                )
                artifact = self.artifact_store.publish(artifact, built_path)
                built_path.unlink(missing_ok=True)
                self.build_cache.record(cache_key, artifact)
            self._validate_cached_artifact(artifact, candidate_id, source, cache_key)
            provenance = self._provenance_chain()
            return ManualCandidateBuildResult(
                candidate_id=candidate_id,
                source=source,
                artifact=artifact,
                adapter_provenance=provenance,
                synthetic=False,
            )
        finally:
            self.source_manager.remove_candidate(
                baseline, candidate, output_dir
            )

    def cache_key(self, source: SourceSnapshot, recipe: Mapping[str, Any]) -> str:
        payload = {
            "source_hash": source.source_hash,
            "builder": self.provenance.model_dump(mode="json"),
            "build_recipe": dict(recipe),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def _recipe(self, package: LoadedCandidateSourcePackage) -> dict[str, Any]:
        return {
            "name": M1_OVERLAY_RECIPE_VERSION,
            "package_manifest_hash": package.manifest_hash,
            "artifact_format": "raw-python-source",
            "files": [item.path for item in package.manifest.files],
        }

    @staticmethod
    def _build_overlay_artifact(
        package: LoadedCandidateSourcePackage,
        candidate_id: UUID,
        output_dir: Path,
    ) -> Path:
        build_root = output_dir.resolve() / "builds"
        build_root.mkdir(parents=True, exist_ok=True)
        destination = build_root / f"{candidate_id}.overlay.py"
        source_item = package.manifest.files[0]
        source = CandidateSourcePackageStore._resolve_package_file(
            package.files_root, source_item.path
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{candidate_id}.", suffix=".py.tmp", dir=build_root, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
            with source.open("rb") as content, temporary_path.open("wb") as artifact:
                while chunk := content.read(1024 * 1024):
                    artifact.write(chunk)
                artifact.flush()
                os.fsync(artifact.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            return destination
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _validate_cached_artifact(
        self,
        artifact: ArtifactManifest,
        candidate_id: UUID,
        source: SourceSnapshot,
        cache_key: str,
    ) -> None:
        if (
            artifact.candidate_id != candidate_id
            or artifact.source_snapshot_id != source.snapshot_id
            or artifact.kind != "python_overlay"
            or artifact.synthetic
            or artifact.metadata.get("build_cache_key") != cache_key
        ):
            raise ArtifactIntegrityError("cached M1 artifact has mismatched identity bindings")
        path = file_uri_to_path(artifact.uri).resolve(strict=True)
        if self._sha256(path) != artifact.content_hash:
            raise ArtifactIntegrityError("cached M1 artifact content hash does not match")

    def _provenance_chain(self) -> list[AdapterProvenance]:
        return [
            self.source_manager.provenance,
            self.source_packages.provenance,
            self.provenance,
            self.artifact_store.provenance,
        ]

    @staticmethod
    def _sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"
