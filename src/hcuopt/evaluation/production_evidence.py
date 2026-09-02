# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from pydantic import ConfigDict

from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceAuthoritySet,
    FormalEvidenceVerifierIdentity,
    FormalProducerIdentity,
    ProductionEvidenceRootDescriptor,
    ProductionFormalEvidenceRef,
)
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextDescriptor,
    FormalEvidenceStoreRef,
    FormalVerifierRef,
)
from hcuopt.evaluation.evidence_reader import EvidenceReadError, HashedEvidenceReader
from hcuopt.measurement.evidence import canonical_json_bytes


class ProductionEvidenceError(ValueError):
    """Production Formal evidence cannot be trusted or is bound to another authority."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VerifiedProductionEvidence(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    authority_context_hash: str
    evidence_root_hash: str
    verifier_identity_hash: str
    verified_objects: tuple[ProductionFormalEvidenceRef, ...]
    input_digest: str
    status: Literal["objects_verified"] = "objects_verified"
    verification_scope: Literal["content_addressing_and_role_binding"] = (
        "content_addressing_and_role_binding"
    )
    acceptance_blocker_codes: tuple[
        Literal[
            "recursive_semantic_verification_not_bound",
            "allowlisted_signature_verification_not_bound",
        ],
        ...,
    ] = (
        "recursive_semantic_verification_not_bound",
        "allowlisted_signature_verification_not_bound",
    )
    accepted_for_formal_window: Literal[False] = False
    hcu_accessed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


_EXPECTED_ROLE = {
    "authority_context": "control_plane",
    "search_plan": "control_plane",
    "search_measurement": "measurement_producer",
    "search_cleanup": "measurement_producer",
    "holdout_reveal": "holdout_plan_authority",
    "holdout_measurement": "measurement_producer",
    "holdout_cleanup": "measurement_producer",
    "source_family": "source_artifact_producer",
    "artifact_family": "source_artifact_producer",
    "correctness": "independent_verifier",
    "barrier": "independent_verifier",
    "fwer": "independent_verifier",
    "evidence_bundle": "independent_verifier",
    "signoff": "project_owner",
}


class ProductionEvidenceVerifier:
    """Re-read content-addressed Formal evidence from one deployment-owned root."""

    def __init__(
        self,
        configured_root: Path,
        *,
        root: ProductionEvidenceRootDescriptor,
        verifier: FormalEvidenceVerifierIdentity,
    ) -> None:
        if configured_root.is_symlink():
            raise ProductionEvidenceError(
                "production_evidence_root_invalid", "production Evidence Root cannot be a symlink"
            )
        try:
            configured = configured_root.resolve(strict=True)
        except OSError as exc:
            raise ProductionEvidenceError(
                "production_evidence_root_unavailable", "production Evidence Root is unavailable"
            ) from exc
        declared = _path_from_file_uri(root.root_uri)
        try:
            if declared.resolve(strict=True) != configured:
                raise ProductionEvidenceError(
                    "production_evidence_root_mismatch",
                    "configured Evidence Root differs from its frozen descriptor",
                )
        except OSError as exc:
            raise ProductionEvidenceError(
                "production_evidence_root_unavailable", "declared Evidence Root is unavailable"
            ) from exc
        self.configured_root = configured
        self.root = root
        self.verifier = verifier
        self.reader = HashedEvidenceReader(configured, max_bytes=root.max_object_bytes)

    def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes:
        """Implement the existing Formal finalizer reader without accepting arbitrary paths."""

        return self._read_content_addressed(uri, expected_hash)

    def verify(
        self,
        *,
        context: FormalAuthorityContextDescriptor,
        authorities: FormalEvidenceAuthoritySet,
        references: tuple[ProductionFormalEvidenceRef, ...],
    ) -> VerifiedProductionEvidence:
        self._bind_context(context)
        if authorities.independent_verifier != self.verifier:
            raise ProductionEvidenceError(
                "formal_verifier_identity_drift",
                "runtime Formal Verifier differs from the frozen authority set",
            )
        self._require_holdout_authority_separation(context, authorities)
        self._read_content_addressed(
            self.verifier.identity_evidence_uri,
            self.verifier.identity_evidence_hash,
        )
        if not references:
            raise ProductionEvidenceError(
                "formal_evidence_incomplete", "Formal verification requires evidence objects"
            )

        ordered = tuple(
            sorted(
                references,
                key=lambda item: (item.evidence_class, item.sha256, item.uri),
            )
        )
        seen_uris: set[str] = set()
        hash_classes: dict[str, str] = {}
        for reference in ordered:
            previous_class = hash_classes.get(reference.sha256)
            if previous_class is not None and previous_class != reference.evidence_class:
                raise ProductionEvidenceError(
                    "formal_evidence_cross_stage_reuse",
                    "one Evidence object is reused across Formal stages",
                )
            hash_classes[reference.sha256] = reference.evidence_class
            if reference.uri in seen_uris:
                raise ProductionEvidenceError(
                    "formal_evidence_identity_reused", "Formal Evidence URI is reused"
                )
            seen_uris.add(reference.uri)
            self._verify_role(reference, authorities, context)
            encoded = self._read_content_addressed(reference.uri, reference.sha256)
            if len(encoded) != reference.byte_count:
                raise ProductionEvidenceError(
                    "formal_evidence_size_mismatch", "Formal Evidence byte count drifted"
                )

        digest_payload = {
            "schema_version": "m2a-production-evidence-verification-input-v1",
            "authority_context_hash": context.context_hash,
            "evidence_root_hash": self.root.root_hash,
            "verifier_identity_hash": self.verifier.identity_hash,
            "authorities": authorities.model_dump(mode="json"),
            "objects": [item.model_dump(mode="json") for item in ordered],
        }
        return VerifiedProductionEvidence(
            authority_context_hash=context.context_hash,
            evidence_root_hash=self.root.root_hash,
            verifier_identity_hash=self.verifier.identity_hash,
            verified_objects=ordered,
            input_digest=_hash_bytes(canonical_json_bytes(digest_payload)),
        )

    def _bind_context(self, context: FormalAuthorityContextDescriptor) -> None:
        expected_store = FormalEvidenceStoreRef(
            store_id=self.root.root_id,
            store_version=self.root.root_version,
            store_hash=self.root.root_hash,
            access_policy_hash=self.root.access_policy_hash,
        )
        expected_verifier = FormalVerifierRef(
            verifier_id=self.verifier.verifier_id,
            verifier_version=self.verifier.verifier_version,
            verifier_hash=self.verifier.identity_hash,
        )
        if context.evidence_store != expected_store:
            raise ProductionEvidenceError(
                "formal_evidence_root_binding_mismatch",
                "Formal Authority Context binds another Evidence Root",
            )
        if context.verifier != expected_verifier:
            raise ProductionEvidenceError(
                "formal_verifier_binding_mismatch",
                "Formal Authority Context binds another Verifier identity",
            )

    def _verify_role(
        self,
        reference: ProductionFormalEvidenceRef,
        authorities: FormalEvidenceAuthoritySet,
        context: FormalAuthorityContextDescriptor,
    ) -> None:
        expected_role = _EXPECTED_ROLE[reference.evidence_class]
        if reference.producer_role != expected_role:
            raise ProductionEvidenceError(
                "formal_evidence_role_mismatch",
                f"{reference.evidence_class} Evidence is signed by the wrong role",
            )
        if expected_role == "holdout_plan_authority":
            expected_identity = (
                context.holdout_plan_authority_id,
                context.holdout_plan_authority_hash,
            )
        elif expected_role == "independent_verifier":
            expected_identity = (
                self.verifier.verifier_id,
                self.verifier.identity_hash,
            )
        else:
            producer = getattr(authorities, expected_role)
            if not isinstance(producer, FormalProducerIdentity):
                raise ProductionEvidenceError(
                    "formal_evidence_role_mismatch", "Formal authority set is malformed"
                )
            expected_identity = (producer.producer_id, producer.producer_hash)
        if (reference.producer_id, reference.producer_hash) != expected_identity:
            raise ProductionEvidenceError(
                "formal_evidence_producer_mismatch",
                "Formal Evidence producer identity drifted",
            )

    def _require_holdout_authority_separation(
        self,
        context: FormalAuthorityContextDescriptor,
        authorities: FormalEvidenceAuthoritySet,
    ) -> None:
        other_ids = {
            authorities.control_plane.producer_id,
            authorities.measurement_producer.producer_id,
            authorities.source_artifact_producer.producer_id,
            authorities.project_owner.producer_id,
            self.verifier.verifier_id,
        }
        other_hashes = {
            authorities.control_plane.producer_hash,
            authorities.measurement_producer.producer_hash,
            authorities.source_artifact_producer.producer_hash,
            authorities.project_owner.producer_hash,
            self.verifier.identity_hash,
        }
        if (
            context.holdout_plan_authority_id in other_ids
            or context.holdout_plan_authority_hash in other_hashes
        ):
            raise ProductionEvidenceError(
                "holdout_plan_authority_not_separated",
                "Holdout Plan Authority must be role-separated from every other Formal authority",
            )

    def _read_content_addressed(self, uri: str, expected_hash: str) -> bytes:
        digest = expected_hash.removeprefix("sha256:")
        expected_path = self.configured_root / "objects" / "sha256" / digest[:2] / digest[2:]
        actual_path = _path_from_file_uri(uri)
        if actual_path != expected_path:
            raise ProductionEvidenceError(
                "formal_evidence_uri_not_content_addressed",
                "Formal Evidence URI does not match its SHA-256 object path",
            )
        try:
            return self.reader.read_raw_bytes(uri, expected_hash)
        except EvidenceReadError as exc:
            if exc.code == "evidence_path_escape":
                raise ProductionEvidenceError(
                    "formal_evidence_missing",
                    "content-addressed Formal Evidence object is unavailable",
                ) from exc
            raise ProductionEvidenceError(exc.code, str(exc)) from exc


def content_addressed_evidence_path(root: Path, content_hash: str) -> Path:
    digest = content_hash.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("content_hash must be a lowercase SHA-256 digest")
    return root / "objects" / "sha256" / digest[:2] / digest[2:]


def _path_from_file_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ProductionEvidenceError(
            "formal_evidence_uri_invalid", "Formal Evidence requires a local file URI"
        )
    return Path(unquote(parsed.path))


def _hash_bytes(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
