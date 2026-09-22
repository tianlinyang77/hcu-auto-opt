from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from hcuopt.contracts.platform_v1 import ArtifactManifest
from hcuopt.domain.errors import ArtifactIntegrityError
from hcuopt.source_hash import file_uri_to_path


class BuildCache(Protocol):
    """Reserved F1 boundary; search-cache policy is deliberately out of scope."""

    def lookup(self, cache_key: str) -> ArtifactManifest | None: ...

    def record(self, cache_key: str, manifest: ArtifactManifest) -> None: ...


class LocalBuildCache:
    """Small immutable cache index for deterministic M1 Overlay artifacts."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def lookup(self, cache_key: str) -> ArtifactManifest | None:
        path = self._entry_path(cache_key)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ArtifactIntegrityError(f"build cache entry is not a regular file: {path}")
        manifest = ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))
        artifact_path = file_uri_to_path(manifest.uri).resolve(strict=True)
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ArtifactIntegrityError(
                f"cached artifact is not a regular file: {artifact_path}"
            )
        return manifest

    def record(self, cache_key: str, manifest: ArtifactManifest) -> None:
        destination = self._entry_path(cache_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        if destination.exists():
            existing = destination.read_bytes()
            if existing != encoded:
                raise ArtifactIntegrityError(
                    f"build cache key collision for immutable entry: {cache_key}"
                )
            return

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".cache-", dir=destination.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(0o444)
            try:
                if os.name == "nt":
                    os.rename(temporary_path, destination)
                else:
                    os.link(temporary_path, destination)
            except FileExistsError:
                existing = destination.read_bytes()
                if existing != encoded:
                    raise ArtifactIntegrityError(
                        f"build cache key collision for immutable entry: {cache_key}"
                    ) from None
        finally:
            if temporary_path is not None:
                if os.name == "nt" and temporary_path.exists():
                    temporary_path.chmod(0o600)
                temporary_path.unlink(missing_ok=True)

    def _entry_path(self, cache_key: str) -> Path:
        prefix = "sha256:"
        digest = cache_key.removeprefix(prefix)
        if not cache_key.startswith(prefix) or len(digest) != 64:
            raise ArtifactIntegrityError(f"invalid build cache key: {cache_key}")
        try:
            int(digest, 16)
        except ValueError as error:
            raise ArtifactIntegrityError(f"invalid build cache key: {cache_key}") from error
        return self.root / "sha256" / digest[:2] / f"{digest[2:]}.json"
