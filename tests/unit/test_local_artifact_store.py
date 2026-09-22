from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from hcuopt.adapters.local_artifact_store import LocalArtifactStore
from hcuopt.contracts.platform_v1 import ArtifactManifest
from hcuopt.domain.errors import ArtifactIntegrityError
from hcuopt.source_hash import file_uri_to_path

HASH = "sha256:" + "a" * 64


def _manifest(content_hash: str) -> ArtifactManifest:
    return ArtifactManifest(
        kind="test",
        uri="file:///not-published",
        content_hash=content_hash,
        build_recipe={"name": "test-v1"},
        metadata={"adapter_provenance": []},
    )


def test_publish_is_content_addressed_immutable_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "artifact.tar"
    source.write_bytes(b"stable artifact")
    actual_hash = LocalArtifactStore._sha256(source)
    store = LocalArtifactStore(tmp_path / "store")

    first = store.publish(_manifest(actual_hash), source)
    second = store.publish(_manifest(actual_hash), source)
    published_path = file_uri_to_path(first.uri)

    assert second.uri == first.uri
    assert published_path.read_bytes() == b"stable artifact"
    assert published_path.stat().st_mode & 0o222 == 0
    assert first.metadata["immutable"] is True
    assert first.metadata["adapter_provenance"][-1]["capability"] == "artifact_store"
    assert not list(store.root.rglob(".publish-*"))


@pytest.mark.parametrize("collision", [False, True])
def test_atomic_publish_preserves_destination_and_cleans_temp(tmp_path, monkeypatch, collision):
    source = tmp_path / "source"
    source.write_bytes(b"new content")
    destination = tmp_path / "destination"
    if collision:
        destination.write_bytes(b"existing content")
        destination.chmod(0o444)
    else:
        def fail(*args):
            raise OSError(errno.EIO, "injected publication failure")
        monkeypatch.setattr(os, "rename" if os.name == "nt" else "link", fail)
    if collision:
        LocalArtifactStore._publish_atomic(source, destination)
        assert destination.read_bytes() == b"existing content"
        assert destination.stat().st_mode & 0o222 == 0
    else:
        with pytest.raises(OSError, match="injected publication failure"):
            LocalArtifactStore._publish_atomic(source, destination)
        assert not destination.exists()
    assert source.read_bytes() == b"new content"
    assert not list(tmp_path.glob(".publish-*"))


def test_publish_rejects_manifest_hash_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "artifact.tar"
    source.write_bytes(b"not the declared hash")

    with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
        LocalArtifactStore(tmp_path / "store").publish(_manifest(HASH), source)


def test_publish_detects_existing_store_corruption(tmp_path: Path) -> None:
    source = tmp_path / "artifact.tar"
    source.write_bytes(b"stable artifact")
    store = LocalArtifactStore(tmp_path / "store")
    manifest = _manifest(store._sha256(source))
    published = store.publish(manifest, source)
    published_path = file_uri_to_path(published.uri)
    published_path.chmod(0o644)
    published_path.write_bytes(b"corrupt")

    with pytest.raises(ArtifactIntegrityError, match="corruption"):
        store.publish(manifest, source)
