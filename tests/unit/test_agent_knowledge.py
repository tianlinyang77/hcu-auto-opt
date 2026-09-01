# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
from hcuopt.agent.identity import knowledge_snapshot_hash
from hcuopt.contracts.agent_v1 import KnowledgeSnapshot
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes

NOW = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _snapshot() -> tuple[KnowledgeSnapshot, dict[tuple[str, str], bytes]]:
    skill = b"name: hcu-operator\nversion: 1\n"
    evidence = b'{"hotspot":"allocator.free","status":"verified"}\n'
    snapshot = KnowledgeSnapshot(
        snapshot_id=UUID("61000000-0000-0000-0000-000000000001"),
        sources=(
            {
                "knowledge_id": "operator/evidence/allocator-free",
                "source_kind": "operator_evidence",
                "version": "evidence-v1",
                "source_uri": "evidence:///allocator-free.json",
                "content_hash": _hash(evidence),
                "license_id": "project-evidence",
            },
            {
                "knowledge_id": "skill/hcu-operator",
                "source_kind": "skill",
                "version": "1.0.0",
                "source_uri": "skill:///hcu-operator/SKILL.md",
                "content_hash": _hash(skill),
                "license_id": "MulanPSL-2.0",
            },
        ),
        created_by="m2b-candidate-curator",
        created_at=NOW,
    )
    return snapshot, {
        ("skill/hcu-operator", "1.0.0"): skill,
        ("operator/evidence/allocator-free", "evidence-v1"): evidence,
    }


def _store(root: Path) -> KnowledgeSnapshotStore:
    return KnowledgeSnapshotStore(root, profile="m2b-knowledge-store-v1")


def test_publish_and_reread_freezes_snapshot_and_source_bytes(tmp_path: Path) -> None:
    snapshot, payloads = _snapshot()
    store = _store(tmp_path)

    first = store.publish(snapshot, payloads)
    replay = store.publish(snapshot, payloads)

    assert first == replay
    assert first.snapshot_hash == knowledge_snapshot_hash(snapshot)
    assert tuple(item.reference.knowledge_id for item in first.sources) == (
        "operator/evidence/allocator-free",
        "skill/hcu-operator",
    )
    assert all(not item.reference.runtime_code_import_allowed for item in first.sources)
    assert store.provenance.capability == "knowledge_snapshot_store"


def test_publish_rejects_missing_or_changed_knowledge_payload(tmp_path: Path) -> None:
    snapshot, payloads = _snapshot()
    store = _store(tmp_path)
    missing = dict(payloads)
    missing.pop(("skill/hcu-operator", "1.0.0"))

    with pytest.raises(SourceArtifactError, match="payload family"):
        store.publish(snapshot, missing)

    changed = dict(payloads)
    changed[("skill/hcu-operator", "1.0.0")] = b"changed\n"
    with pytest.raises(SourceArtifactError, match="content Hash"):
        store.publish(snapshot, changed)


def test_reread_rejects_source_or_snapshot_tampering(tmp_path: Path) -> None:
    snapshot, payloads = _snapshot()
    store = _store(tmp_path)
    verified = store.publish(snapshot, payloads)
    source_hash = snapshot.sources[0].content_hash.removeprefix("sha256:")
    source_path = (
        tmp_path
        / "sources"
        / "sha256"
        / source_hash[:2]
        / source_hash[2:]
        / "content.bin"
    )
    source_path.write_bytes(b"tampered\n")

    with pytest.raises(SourceArtifactError, match="content changed"):
        store.load(snapshot.snapshot_id, verified.snapshot_hash)

    source_path.write_bytes(payloads[("operator/evidence/allocator-free", "evidence-v1")])
    changed_snapshot = snapshot.model_copy(update={"created_by": "different-curator"})
    snapshot_hash = verified.snapshot_hash.removeprefix("sha256:")
    snapshot_path = (
        tmp_path
        / "snapshots"
        / "sha256"
        / snapshot_hash[:2]
        / snapshot_hash[2:]
        / "snapshot.json"
    )
    snapshot_path.write_bytes(canonical_json_bytes(changed_snapshot))

    with pytest.raises(SourceArtifactError, match="authority Hash"):
        store.load(snapshot.snapshot_id, verified.snapshot_hash)
