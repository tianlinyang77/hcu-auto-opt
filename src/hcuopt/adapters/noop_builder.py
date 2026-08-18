from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.contracts.platform_v1 import (
    AdapterProvenance,
    ArtifactManifest,
    SourceSnapshot,
)
from hcuopt.domain.errors import SourceArtifactError, SourceIntegrityError
from hcuopt.source_hash import canonical_source_hash, file_uri_to_path

NOOP_BUILD_RECIPE_VERSION = "noop-source-tar-v1"


class NoopBuilder:
    """Create a deterministic source archive without changing runtime behavior."""

    def __init__(self, profile: str = "f1-c-local-v1") -> None:
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="builder",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def build(self, candidate: Mapping[str, Any], output_dir: Path) -> ArtifactManifest:
        candidate_id = self._candidate_id(candidate)
        snapshot = self._source_snapshot(candidate)
        if snapshot.kind != "candidate" or not snapshot.clean:
            raise SourceIntegrityError("No-op build requires a clean candidate snapshot")

        source_path = file_uri_to_path(snapshot.worktree_uri).resolve(strict=True)
        before_hash = canonical_source_hash(source_path)
        if before_hash != snapshot.source_hash:
            raise SourceIntegrityError(
                "candidate source no longer matches SourceSnapshot: "
                f"expected {snapshot.source_hash}, got {before_hash}"
            )

        build_root = output_dir.resolve() / "builds"
        build_root.mkdir(parents=True, exist_ok=True)
        if build_root == source_path or source_path in build_root.parents:
            raise SourceArtifactError("build output cannot be inside the candidate source tree")

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{candidate_id}.", suffix=".tar.tmp", dir=build_root, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
            self._write_deterministic_tar(source_path, temporary_path)
            content_hash = self._sha256(temporary_path)
            artifact_path = build_root / f"{candidate_id}.tar"
            os.replace(temporary_path, artifact_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        after_hash = canonical_source_hash(source_path)
        if after_hash != snapshot.source_hash:
            artifact_path.unlink(missing_ok=True)
            raise SourceIntegrityError("candidate source changed while the artifact was built")

        recipe = {
            "name": NOOP_BUILD_RECIPE_VERSION,
            "archive_format": "tar-pax",
            "mtime": 0,
            "uid": 0,
            "gid": 0,
            "path_order": "bytewise",
            "excluded_paths": [".git"],
        }
        log_path = self._write_build_log(
            output_dir=output_dir,
            candidate_id=candidate_id,
            snapshot=snapshot,
            content_hash=content_hash,
            recipe=recipe,
        )
        provenance = self._input_provenance(candidate)
        provenance.append(self.provenance)
        return ArtifactManifest(
            candidate_id=candidate_id,
            kind="noop-source-archive",
            uri=artifact_path.as_uri(),
            content_hash=content_hash,
            source_snapshot_id=snapshot.snapshot_id,
            build_recipe=recipe,
            metadata={
                "source_hash": snapshot.source_hash,
                "build_log_uri": log_path.as_uri(),
                "adapter_provenance": [
                    item.model_dump(mode="json") for item in self._deduplicate(provenance)
                ],
            },
            synthetic=False,
        )

    @staticmethod
    def cache_key(
        snapshot: SourceSnapshot,
        provenance: AdapterProvenance,
        recipe: Mapping[str, Any] | None = None,
    ) -> str:
        payload = {
            "source_hash": snapshot.source_hash,
            "builder": provenance.model_dump(mode="json"),
            "build_recipe": dict(recipe or {"name": NOOP_BUILD_RECIPE_VERSION}),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @staticmethod
    def _candidate_id(candidate: Mapping[str, Any]) -> UUID:
        try:
            return UUID(str(candidate["candidate_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise SourceArtifactError("build payload requires a valid candidate_id") from error

    @staticmethod
    def _source_snapshot(candidate: Mapping[str, Any]) -> SourceSnapshot:
        try:
            value = candidate["source_snapshot"]
        except KeyError as error:
            raise SourceArtifactError("build payload requires source_snapshot") from error
        if isinstance(value, SourceSnapshot):
            return value
        return SourceSnapshot.model_validate(value)

    @staticmethod
    def _input_provenance(candidate: Mapping[str, Any]) -> list[AdapterProvenance]:
        raw = candidate.get("adapter_provenance", [])
        if not isinstance(raw, list):
            raise SourceArtifactError("adapter_provenance must be a list")
        return [AdapterProvenance.model_validate(item) for item in raw]

    @staticmethod
    def _deduplicate(items: list[AdapterProvenance]) -> list[AdapterProvenance]:
        found: dict[tuple[str, str, str, str], AdapterProvenance] = {}
        for item in items:
            key = (item.profile, item.capability, item.adapter_name, item.adapter_version)
            found[key] = item
        return list(found.values())

    @classmethod
    def _write_deterministic_tar(cls, source_root: Path, destination: Path) -> None:
        with tarfile.open(destination, mode="w", format=tarfile.PAX_FORMAT) as archive:
            cls._add_directory(archive, source_root, Path())

    @classmethod
    def _add_directory(
        cls, archive: tarfile.TarFile, directory: Path, relative: Path
    ) -> None:
        entries = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        for entry in entries:
            if relative == Path() and entry.name == ".git":
                continue
            entry_relative = relative / entry.name
            archive_name = entry_relative.as_posix()
            entry_stat = entry.stat(follow_symlinks=False)

            info = tarfile.TarInfo(archive_name)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""

            if entry.is_symlink():
                info.type = tarfile.SYMTYPE
                info.mode = 0o777
                info.linkname = os.readlink(entry.path)
                archive.addfile(info)
                continue

            if entry.is_dir(follow_symlinks=False):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
                cls._add_directory(archive, Path(entry.path), entry_relative)
                continue

            if entry.is_file(follow_symlinks=False):
                info.type = tarfile.REGTYPE
                info.mode = 0o755 if entry_stat.st_mode & stat.S_IXUSR else 0o644
                info.size = entry_stat.st_size
                with open(entry.path, "rb") as source:
                    archive.addfile(info, source)
                continue

            raise SourceIntegrityError(f"unsupported special file in source tree: {entry.path}")

    def _write_build_log(
        self,
        *,
        output_dir: Path,
        candidate_id: UUID,
        snapshot: SourceSnapshot,
        content_hash: str,
        recipe: Mapping[str, Any],
    ) -> Path:
        log_dir = output_dir.resolve() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"build-{candidate_id}.log"
        log_path.write_text(
            "\n".join(
                (
                    f"candidate_id={candidate_id}",
                    f"source_snapshot_id={snapshot.snapshot_id}",
                    f"source_hash={snapshot.source_hash}",
                    f"content_hash={content_hash}",
                    f"build_recipe={json.dumps(dict(recipe), sort_keys=True)}",
                    f"adapter={self.provenance.adapter_name}",
                    f"adapter_version={self.provenance.adapter_version}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        return log_path

    @staticmethod
    def _sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"
