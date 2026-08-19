from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_immutable_json(
    output_dir: Path,
    *,
    stage0_run_id: str,
    probe_type: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """Atomically persist canonical JSON and return its file URI and SHA256."""

    evidence_dir = output_dir.resolve() / "stage0" / stage0_run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    destination = evidence_dir / f"{probe_type}.json"
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ValueError(f"evidence destination is not a regular file: {destination}")
        existing = destination.read_bytes()
        if existing != encoded:
            raise ValueError(
                f"immutable evidence already exists with different content: {destination}"
            )
        return destination.as_uri(), digest

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{probe_type}-",
            suffix=".tmp",
            dir=evidence_dir,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(0o444)
        os.replace(temporary_path, destination)
        temporary_path = None
        destination.chmod(0o444)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return destination.as_uri(), digest


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"
