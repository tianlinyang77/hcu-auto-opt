from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence


@dataclass(frozen=True, slots=True)
class PublishedEvidence:
    uri: str
    sha256: str
    publication_authority: str


class EvidencePublisher(Protocol):
    """Deployment trust boundary for publishing runtime-probe evidence."""

    publication_authority: str
    authorizes_formal_results: bool

    def publish(
        self,
        output_dir: Path,
        *,
        stage0_run_id: str,
        probe_type: str,
        payload: dict[str, Any],
    ) -> PublishedEvidence: ...


class LocalContentAddressedEvidencePublisher:
    """Race-free local publisher for Dry Run; local ownership is not formal authority."""

    publication_authority = "worker-local-content-addressed-v2"
    authorizes_formal_results = False

    def publish(
        self,
        output_dir: Path,
        *,
        stage0_run_id: str,
        probe_type: str,
        payload: dict[str, Any],
    ) -> PublishedEvidence:
        uri, digest = write_content_addressed_json(
            output_dir,
            stage0_run_id=stage0_run_id,
            probe_type=probe_type,
            payload=payload,
        )
        return PublishedEvidence(uri, digest, self.publication_authority)


def write_content_addressed_json(
    output_dir: Path,
    *,
    stage0_run_id: str,
    probe_type: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """Publish canonical JSON at its digest path without replacing existing bytes."""

    run_component = str(UUID(stage0_run_id))
    probe_component = Stage0ProbeType(probe_type).value
    encoded = canonical_json_bytes(payload)
    digest_hex = hashlib.sha256(encoded).hexdigest()
    destination = (
        output_dir.resolve()
        / "stage0"
        / run_component
        / probe_component
        / f"sha256-{digest_hex}.json"
    )
    artifact = write_evidence(destination, payload)
    return artifact.uri, artifact.sha256


# Kept as a source-compatible name for existing callers. Formal authority is decided
# by EvidencePublisher, never by Unix mode bits on this local file.
write_immutable_json = write_content_addressed_json


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"
