# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from hcuopt.agent.identity import knowledge_snapshot_hash
from hcuopt.contracts.agent_v1 import KnowledgeSnapshot, KnowledgeSourceRef
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes

MAX_KNOWLEDGE_SOURCE_BYTES = 4 * 1024 * 1024
MAX_KNOWLEDGE_SNAPSHOT_BYTES = 16 * 1024 * 1024

KnowledgeIdentity = tuple[str, str]


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest_path(root: Path, namespace: str, digest: str, filename: str) -> Path:
    value = digest.removeprefix("sha256:")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SourceArtifactError("knowledge Store received an invalid SHA256 identity")
    return root / namespace / "sha256" / value[:2] / value[2:] / filename


def _read_regular(path: Path, *, maximum_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SourceArtifactError(f"knowledge Store entry is not a regular file: {path}")
    size = path.stat().st_size
    if size > maximum_bytes:
        raise SourceArtifactError(f"knowledge Store entry exceeds its size limit: {path}")
    return path.read_bytes()


def _publish_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _read_regular(path, maximum_bytes=len(payload)) != payload:
            raise SourceArtifactError(
                "knowledge Store immutable identity already contains different bytes"
            )
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".publish-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            if _read_regular(path, maximum_bytes=len(payload)) != payload:
                raise SourceArtifactError(
                    "knowledge Store concurrent publication changed immutable bytes"
                ) from error
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class VerifiedKnowledgeSource:
    reference: KnowledgeSourceRef
    payload: bytes


@dataclass(frozen=True, slots=True)
class VerifiedKnowledgeSnapshot:
    snapshot: KnowledgeSnapshot
    snapshot_hash: str
    sources: tuple[VerifiedKnowledgeSource, ...]


class KnowledgeSnapshotStore:
    """Deployment-owned immutable knowledge bytes; never a Python import source."""

    def __init__(self, root: Path, *, profile: str) -> None:
        self.root = root.resolve()
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="knowledge_snapshot_store",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    @staticmethod
    def _identity(source: KnowledgeSourceRef) -> KnowledgeIdentity:
        return source.knowledge_id, source.version

    def publish(
        self,
        snapshot: KnowledgeSnapshot,
        payloads: Mapping[KnowledgeIdentity, bytes],
    ) -> VerifiedKnowledgeSnapshot:
        expected = {self._identity(source) for source in snapshot.sources}
        if set(payloads) != expected:
            raise SourceArtifactError(
                "knowledge Snapshot payload family does not match its frozen sources"
            )
        if sum(len(payload) for payload in payloads.values()) > MAX_KNOWLEDGE_SNAPSHOT_BYTES:
            raise SourceArtifactError("knowledge Snapshot payload family exceeds its size limit")

        for source in snapshot.sources:
            payload = payloads[self._identity(source)]
            if len(payload) > MAX_KNOWLEDGE_SOURCE_BYTES:
                raise SourceArtifactError("knowledge source exceeds its size limit")
            if _sha256(payload) != source.content_hash:
                raise SourceArtifactError(
                    "knowledge source bytes do not match the declared content Hash"
                )
            _publish_once(
                _digest_path(
                    self.root,
                    "sources",
                    source.content_hash,
                    "content.bin",
                ),
                payload,
            )

        snapshot_hash = knowledge_snapshot_hash(snapshot)
        _publish_once(
            _digest_path(
                self.root,
                "snapshots",
                snapshot_hash,
                "snapshot.json",
            ),
            canonical_json_bytes(snapshot),
        )
        return self.load(snapshot.snapshot_id, snapshot_hash)

    def load(
        self,
        snapshot_id: UUID,
        expected_hash: str,
    ) -> VerifiedKnowledgeSnapshot:
        snapshot_path = _digest_path(
            self.root,
            "snapshots",
            expected_hash,
            "snapshot.json",
        )
        snapshot_bytes = _read_regular(
            snapshot_path,
            maximum_bytes=MAX_KNOWLEDGE_SOURCE_BYTES,
        )
        snapshot = KnowledgeSnapshot.model_validate_json(snapshot_bytes)
        if snapshot.snapshot_id != snapshot_id:
            raise SourceArtifactError("knowledge Snapshot identity does not match its Request")
        actual_hash = knowledge_snapshot_hash(snapshot)
        if actual_hash != expected_hash:
            raise SourceArtifactError(
                "knowledge Snapshot bytes do not match the requested authority Hash"
            )

        sources: list[VerifiedKnowledgeSource] = []
        total_bytes = 0
        for source in sorted(
            snapshot.sources,
            key=lambda item: (item.knowledge_id, item.version, item.content_hash),
        ):
            payload = _read_regular(
                _digest_path(
                    self.root,
                    "sources",
                    source.content_hash,
                    "content.bin",
                ),
                maximum_bytes=MAX_KNOWLEDGE_SOURCE_BYTES,
            )
            total_bytes += len(payload)
            if total_bytes > MAX_KNOWLEDGE_SNAPSHOT_BYTES:
                raise SourceArtifactError(
                    "knowledge Snapshot payload family exceeds its size limit"
                )
            if _sha256(payload) != source.content_hash:
                raise SourceArtifactError("knowledge source content changed after publication")
            sources.append(VerifiedKnowledgeSource(reference=source, payload=payload))

        return VerifiedKnowledgeSnapshot(
            snapshot=snapshot,
            snapshot_hash=actual_hash,
            sources=tuple(sources),
        )


__all__ = [
    "KnowledgeIdentity",
    "KnowledgeSnapshotStore",
    "VerifiedKnowledgeSnapshot",
    "VerifiedKnowledgeSource",
]
