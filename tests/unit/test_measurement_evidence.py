from __future__ import annotations

import json

from hcuopt.measurement.evidence import verify_evidence, write_evidence
from hcuopt.measurement.fingerprint import stable_fingerprint


def test_evidence_is_canonical_atomic_and_hash_verifiable(tmp_path) -> None:
    target = tmp_path / "evidence.json"
    artifact = write_evidence(target, {"z": 1, "a": {"beta": 2, "alpha": 3}})

    assert target.read_bytes() == b'{"a":{"alpha":3,"beta":2},"z":1}\n'
    assert artifact.uri == target.resolve().as_uri()
    assert artifact.sha256.startswith("sha256:")
    assert verify_evidence(target, artifact.sha256) is True
    assert not list(tmp_path.glob("*.tmp"))

    target.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    assert verify_evidence(target, artifact.sha256) is False


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
