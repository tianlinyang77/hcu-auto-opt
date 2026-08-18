from __future__ import annotations

from typing import Protocol

from hcuopt.contracts.platform_v1 import ArtifactManifest


class BuildCache(Protocol):
    """Reserved F1 boundary; search-cache policy is deliberately out of scope."""

    def lookup(self, cache_key: str) -> ArtifactManifest | None: ...

    def record(self, cache_key: str, manifest: ArtifactManifest) -> None: ...
