from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    uri: str
    sha256: str
    byte_count: int


def canonical_json_bytes(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (payload + "\n").encode("utf-8")


def write_evidence(path: Path, value: Any) -> EvidenceArtifact:
    """Write canonical bytes atomically; the returned hash is over those exact bytes."""

    final_path = path.resolve()
    final_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(value)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=final_path.parent,
        prefix=f".{final_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return EvidenceArtifact(
        uri=final_path.as_uri(),
        sha256="sha256:" + hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
    )


def verify_evidence(path: Path, expected_sha256: str) -> bool:
    if not expected_sha256.startswith("sha256:"):
        return False
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual == expected_sha256.removeprefix("sha256:")
