from __future__ import annotations

import hashlib
from typing import Any

from hcuopt.measurement.evidence import canonical_json_bytes


def stable_fingerprint(identity: dict[str, Any]) -> str:
    """Hash only lockable target/image/source/runtime identity, never run telemetry."""

    return "sha256:" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
