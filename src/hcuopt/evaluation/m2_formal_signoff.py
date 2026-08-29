# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from hcuopt.contracts.m2_formal_signoff_v1 import (
    FormalDecisionSignature,
    FormalRoundSignoffArtifactPublication,
    FormalRoundSignoffDecisionArtifact,
    FormalRoundSignoffIntent,
    formal_round_signoff_artifact_content,
    formal_round_signoff_artifact_hash,
)
from hcuopt.evaluation.evidence_reader import EvidenceReadError
from hcuopt.evaluation.m2_verifier import EvidenceByteReader
from hcuopt.measurement.evidence import canonical_json_bytes, write_evidence_bytes


class M2FormalSignoffError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FormalDecisionSigner(Protocol):
    def sign(self, payload: bytes) -> FormalDecisionSignature: ...


class FormalDecisionSignatureVerifier(Protocol):
    def verify(self, payload: bytes, signature: FormalDecisionSignature) -> None: ...


class FormalRoundSignoffFinalizationService(Protocol):
    def verify(
        self,
        *,
        intent: FormalRoundSignoffIntent,
        publication: FormalRoundSignoffArtifactPublication,
    ) -> FormalRoundSignoffDecisionArtifact: ...


class LocalFormalSignoffArtifactPublisher:
    """Publish one deterministic signed decision Artifact into a local CAS."""

    def __init__(self, root: Path, signer: FormalDecisionSigner) -> None:
        self.root = root.resolve()
        self.signer = signer

    def publish(
        self, intent: FormalRoundSignoffIntent
    ) -> FormalRoundSignoffArtifactPublication:
        content = formal_round_signoff_artifact_content(intent)
        signature = self.signer.sign(canonical_json_bytes(content))
        artifact = FormalRoundSignoffDecisionArtifact(
            content=content,
            signature=signature,
        )
        encoded = canonical_json_bytes(artifact)
        digest = hashlib.sha256(encoded).hexdigest()
        destination = self.root / "sha256" / digest[:2] / f"{digest[2:]}.json"
        published = write_evidence_bytes(destination, encoded)
        return FormalRoundSignoffArtifactPublication(
            signoff_intent_id=intent.signoff_intent_id,
            round_signoff_id=intent.round_signoff_id,
            decision_artifact_uri=published.uri,
            decision_artifact_hash=published.sha256,
            signature=signature,
        )


class M2FormalRoundSignoffFinalizer:
    """Reread and authenticate one published Formal Round decision Artifact."""

    def __init__(
        self,
        evidence_reader: EvidenceByteReader,
        signature_verifier: FormalDecisionSignatureVerifier,
    ) -> None:
        self.evidence_reader = evidence_reader
        self.signature_verifier = signature_verifier

    def verify(
        self,
        *,
        intent: FormalRoundSignoffIntent,
        publication: FormalRoundSignoffArtifactPublication,
    ) -> FormalRoundSignoffDecisionArtifact:
        if (
            publication.signoff_intent_id != intent.signoff_intent_id
            or publication.round_signoff_id != intent.round_signoff_id
        ):
            raise M2FormalSignoffError(
                "round_signoff_artifact_identity_mismatch",
                "Formal Signoff Artifact belongs to another Intent",
            )
        try:
            encoded = self.evidence_reader.read_raw_bytes(
                publication.decision_artifact_uri,
                publication.decision_artifact_hash,
            )
            artifact = FormalRoundSignoffDecisionArtifact.model_validate_json(encoded)
        except EvidenceReadError as exc:
            raise M2FormalSignoffError(exc.code, str(exc)) from exc
        except (ValidationError, ValueError, OSError) as exc:
            raise M2FormalSignoffError(
                "round_signoff_artifact_unavailable",
                "Formal Signoff Artifact cannot be read and validated",
            ) from exc
        if canonical_json_bytes(artifact) != encoded:
            raise M2FormalSignoffError(
                "round_signoff_artifact_noncanonical",
                "Formal Signoff Artifact must be canonical JSON",
            )
        if formal_round_signoff_artifact_hash(artifact) != publication.decision_artifact_hash:
            raise M2FormalSignoffError(
                "round_signoff_artifact_hash_mismatch",
                "Formal Signoff Artifact Hash changed",
            )
        expected = formal_round_signoff_artifact_content(intent)
        if canonical_json_bytes(artifact.content) != canonical_json_bytes(expected):
            raise M2FormalSignoffError(
                "round_signoff_evidence_mismatch",
                "Formal Signoff Artifact changed frozen Round inputs",
            )
        if artifact.signature != publication.signature:
            raise M2FormalSignoffError(
                "round_signoff_signature_mismatch",
                "Formal Signoff publication changed the signature envelope",
            )
        try:
            self.signature_verifier.verify(
                canonical_json_bytes(artifact.content), artifact.signature
            )
        except (ValueError, OSError) as exc:
            raise M2FormalSignoffError(
                "round_signoff_signature_invalid",
                "Formal Signoff decision signature is invalid",
            ) from exc
        return artifact
