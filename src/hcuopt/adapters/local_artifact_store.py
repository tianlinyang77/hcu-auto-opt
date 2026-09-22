from __future__ import annotations

import errno
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from hcuopt.contracts.platform_v1 import AdapterProvenance, ArtifactManifest
from hcuopt.domain.errors import ArtifactIntegrityError, SourceArtifactError
from hcuopt.source_hash import file_uri_to_path


class LocalArtifactStore:
    """Publish immutable artifacts into a content-addressed local directory."""

    def __init__(self, root: Path, profile: str = "f1-c-local-v1") -> None:
        self.root = root.resolve()
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="artifact_store",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def publish(self, manifest: ArtifactManifest, source_path: Path) -> ArtifactManifest:
        source_path = source_path.resolve(strict=True)
        if source_path.is_dir():
            source_path = file_uri_to_path(manifest.uri).resolve(strict=True)
        if not source_path.is_file():
            raise SourceArtifactError(f"artifact source is not a regular file: {source_path}")
        actual_hash = self._sha256(source_path)
        if actual_hash != manifest.content_hash:
            raise ArtifactIntegrityError(
                f"artifact hash mismatch: expected {manifest.content_hash}, got {actual_hash}"
            )

        digest = actual_hash.removeprefix("sha256:")
        destination = self.root / "sha256" / digest[:2] / digest[2:]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self._verify_existing(destination, actual_hash)
        else:
            self._publish_atomic(source_path, destination)
            self._verify_existing(destination, actual_hash)

        metadata = dict(manifest.metadata)
        provenance = [
            AdapterProvenance.model_validate(item)
            for item in metadata.get("adapter_provenance", [])
        ]
        provenance.append(self.provenance)
        metadata["adapter_provenance"] = [
            item.model_dump(mode="json") for item in self._deduplicate(provenance)
        ]
        metadata["immutable"] = True
        return manifest.model_copy(update={"uri": destination.as_uri(), "metadata": metadata})

    @staticmethod
    def _publish_atomic(source: Path, destination: Path) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".publish-", dir=destination.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                with source.open("rb") as artifact:
                    shutil.copyfileobj(artifact, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(0o444)
            try:
                if os.name == "nt":
                    # Windows rename refuses an existing destination. Unlike a
                    # hard link, it leaves no read-only temporary name to unlink.
                    os.rename(temporary_path, destination)
                else:
                    os.link(temporary_path, destination)
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise
        finally:
            if temporary_path is not None:
                if os.name == "nt" and temporary_path.exists():
                    # On collision/failure this is still our private temporary
                    # file, never a hard link to the published immutable object.
                    temporary_path.chmod(0o600)
                temporary_path.unlink(missing_ok=True)

    @classmethod
    def _verify_existing(cls, path: Path, expected_hash: str) -> None:
        if path.is_symlink() or not path.is_file():
            raise ArtifactIntegrityError(f"artifact store path is not a regular file: {path}")
        actual_hash = cls._sha256(path)
        if actual_hash != expected_hash:
            raise ArtifactIntegrityError(
                f"artifact store corruption at {path}: expected {expected_hash}, got {actual_hash}"
            )
        path.chmod(0o444)

    @staticmethod
    def _deduplicate(items: list[AdapterProvenance]) -> list[AdapterProvenance]:
        found: dict[tuple[str, str, str, str], AdapterProvenance] = {}
        for item in items:
            key = (item.profile, item.capability, item.adapter_name, item.adapter_version)
            found[key] = item
        return list(found.values())

    @staticmethod
    def _sha256(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"
