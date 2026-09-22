# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from pathlib import Path

import pytest

from hcuopt.adapters.build_cache import LocalBuildCache
from hcuopt.contracts.platform_v1 import ArtifactManifest
from hcuopt.domain.errors import ArtifactIntegrityError


def test_cache_publication_is_read_only_idempotent_and_cleans_temporary(tmp_path: Path):
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"artifact")
    manifest = ArtifactManifest(
        kind="test", uri=artifact.as_uri(), content_hash="sha256:" + "a" * 64,
        build_recipe={"name": "test"}, metadata={},
    )
    cache = LocalBuildCache(tmp_path / "cache")
    key = "sha256:" + "b" * 64
    cache.record(key, manifest)
    cache.record(key, manifest)
    assert cache.lookup(key) == manifest
    assert cache._entry_path(key).stat().st_mode & 0o222 == 0
    assert not list(cache.root.rglob(".cache-*"))
    with pytest.raises(ArtifactIntegrityError, match="collision"):
        cache.record(key, manifest.model_copy(update={"metadata": {"changed": True}}))
    assert cache.lookup(key) == manifest
