from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from hcuopt.domain.enums import Stage0ProbeType
from hcuopt.measurement.evidence import (
    canonical_json_bytes,
    write_evidence,
    write_evidence_bytes,
)

_EVIDENCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


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

    def publish_bytes(
        self,
        output_dir: Path,
        *,
        stage0_run_id: str,
        probe_type: str,
        artifact_name: str,
        encoded: bytes,
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

    def publish_bytes(
        self,
        output_dir: Path,
        *,
        stage0_run_id: str,
        probe_type: str,
        artifact_name: str,
        encoded: bytes,
    ) -> PublishedEvidence:
        return _publish_content_addressed_bytes(
            output_dir,
            stage0_run_id=stage0_run_id,
            probe_type=probe_type,
            artifact_name=artifact_name,
            encoded=encoded,
            publication_authority=self.publication_authority,
        )


class DeploymentContentAddressedEvidencePublisher:
    """Deployment-injected Formal publisher rooted in verifier-readable storage.

    The worker cannot turn its ordinary output directory into Formal authority.  A
    deployment must explicitly construct this publisher with the same fixed root that
    D opens through ``FileStage0EvidenceReader``.
    """

    authorizes_formal_results = True

    def __init__(
        self,
        evidence_root: Path,
        *,
        publication_authority: str = "deployment-stage0-evidence-v2",
    ) -> None:
        if not _EVIDENCE_NAME.fullmatch(publication_authority):
            raise ValueError("publication_authority must be an auditable identifier")
        evidence_root.mkdir(parents=True, exist_ok=True)
        if evidence_root.is_symlink() or not evidence_root.is_dir():
            raise ValueError("Formal evidence root must be a real directory")
        self.evidence_root = evidence_root.resolve(strict=True)
        self.publication_authority = publication_authority

    def publish(
        self,
        output_dir: Path,
        *,
        stage0_run_id: str,
        probe_type: str,
        payload: dict[str, Any],
    ) -> PublishedEvidence:
        del output_dir
        uri, digest = write_content_addressed_json(
            self.evidence_root,
            stage0_run_id=stage0_run_id,
            probe_type=probe_type,
            payload=payload,
        )
        return PublishedEvidence(uri, digest, self.publication_authority)

    def publish_bytes(
        self,
        output_dir: Path,
        *,
        stage0_run_id: str,
        probe_type: str,
        artifact_name: str,
        encoded: bytes,
    ) -> PublishedEvidence:
        del output_dir
        return _publish_content_addressed_bytes(
            self.evidence_root,
            stage0_run_id=stage0_run_id,
            probe_type=probe_type,
            artifact_name=artifact_name,
            encoded=encoded,
            publication_authority=self.publication_authority,
        )


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


def _publish_content_addressed_bytes(
    root: Path,
    *,
    stage0_run_id: str,
    probe_type: str,
    artifact_name: str,
    encoded: bytes,
    publication_authority: str,
) -> PublishedEvidence:
    run_component = str(UUID(stage0_run_id))
    probe_component = Stage0ProbeType(probe_type).value
    if not _EVIDENCE_NAME.fullmatch(artifact_name):
        raise ValueError("evidence artifact_name must be a safe filename")
    if not isinstance(encoded, bytes):
        raise TypeError("raw evidence must be bytes")
    digest_hex = hashlib.sha256(encoded).hexdigest()
    suffix = Path(artifact_name).suffix
    destination = (
        root.resolve()
        / "stage0"
        / run_component
        / probe_component
        / "raw"
        / f"sha256-{digest_hex}{suffix}"
    )
    artifact = write_evidence_bytes(destination, encoded)
    return PublishedEvidence(
        artifact.uri,
        artifact.sha256,
        publication_authority,
    )


# Kept as a source-compatible name for existing callers. Formal authority is decided
# by EvidencePublisher, never by Unix mode bits on this local file.
write_immutable_json = write_content_addressed_json


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"
