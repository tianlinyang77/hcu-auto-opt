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
    """Publish canonical bytes once and reject attempts to replace different evidence."""

    return write_evidence_bytes(path, canonical_json_bytes(value))


def write_evidence_bytes(path: Path, encoded: bytes) -> EvidenceArtifact:
    """Publish immutable evidence bytes and return their content identity."""

    final_path = path.parent.resolve() / path.name
    final_path.parent.mkdir(parents=True, exist_ok=True)
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    artifact = EvidenceArtifact(
        uri=final_path.as_uri(),
        sha256=digest,
        byte_count=len(encoded),
    )
    if final_path.exists() or final_path.is_symlink():
        _verify_existing_evidence(final_path, encoded)
        final_path.chmod(0o444)
        return artifact

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
        if os.name != "nt":
            temporary_path.chmod(0o444)
        try:
            os.link(temporary_path, final_path)
        except FileExistsError:
            _verify_existing_evidence(final_path, encoded)
        else:
            temporary_path.unlink()
            temporary_path = None
            final_path.chmod(0o444)
    except Exception:
        raise
    finally:
        if temporary_path is not None:
            temporary_path.chmod(0o600)
            temporary_path.unlink(missing_ok=True)
    return artifact


def _verify_existing_evidence(path: Path, expected: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"evidence destination is not a regular file: {path}")
    if path.read_bytes() != expected:
        raise ValueError(f"immutable evidence already exists with different content: {path}")


def verify_evidence(path: Path, expected_sha256: str) -> bool:
    if not expected_sha256.startswith("sha256:"):
        return False
    try:
        if path.is_symlink() or not path.is_file():
            return False
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual == expected_sha256.removeprefix("sha256:")
