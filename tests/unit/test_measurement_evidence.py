from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hcuopt.measurement.evidence import canonical_json_bytes, verify_evidence, write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint


def test_evidence_is_canonical_atomic_and_hash_verifiable(tmp_path) -> None:
    target = tmp_path / "evidence.json"
    artifact = write_evidence(target, {"z": 1, "a": {"beta": 2, "alpha": 3}})

    assert target.read_bytes() == b'{"a":{"alpha":3,"beta":2},"z":1}\n'
    assert artifact.uri == target.resolve().as_uri()
    assert artifact.sha256.startswith("sha256:")
    assert verify_evidence(target, artifact.sha256) is True
    assert not list(tmp_path.glob("*.tmp"))
    assert stat.S_IMODE(target.stat().st_mode) & 0o222 == 0

    target.chmod(0o644)
    target.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    assert verify_evidence(target, artifact.sha256) is False


def test_evidence_publish_is_idempotent_but_cannot_replace_content(tmp_path) -> None:
    target = tmp_path / "evidence.json"
    first = write_evidence(target, {"value": 1})
    second = write_evidence(target, {"value": 1})

    assert first == second
    with pytest.raises(ValueError, match="immutable evidence"):
        write_evidence(target, {"value": 2})


def test_concurrent_evidence_publish_never_replaces_the_winner(tmp_path) -> None:
    target = tmp_path / "evidence.json"
    barrier = Barrier(2)

    def publish(value: int) -> str:
        barrier.wait()
        try:
            write_evidence(target, {"value": value})
        except ValueError:
            return "rejected"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, (1, 2)))

    assert sorted(outcomes) == ["published", "rejected"]
    assert target.read_bytes() in {
        canonical_json_bytes({"value": 1}),
        canonical_json_bytes({"value": 2}),
    }


def test_stable_fingerprint_excludes_dynamic_observations() -> None:
    identity = {
        "target": "nmz36-sglang-0.5.12",
        "image": "registry/image@sha256:" + "a" * 64,
        "source": "b" * 40,
    }

    first = stable_fingerprint(identity)
    second = stable_fingerprint(dict(reversed(list(identity.items()))))

    assert first == second
    assert first.startswith("sha256:")
