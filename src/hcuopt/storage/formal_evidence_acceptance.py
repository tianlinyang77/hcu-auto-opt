# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceAcceptanceReview,
)
from hcuopt.domain.errors import Conflict, NotFound, SourceArtifactError
from hcuopt.evaluation.formal_evidence_acceptance import (
    FormalEvidenceAcceptanceReviewSignatureVerifier,
    FormalEvidenceAcceptanceSnapshot,
    verify_formal_evidence_acceptance_review_signature,
)
from hcuopt.measurement.evidence import canonical_json_bytes


def formal_evidence_acceptance_snapshot_hash(
    snapshot: FormalEvidenceAcceptanceSnapshot,
) -> str:
    value = FormalEvidenceAcceptanceSnapshot.model_validate(snapshot.model_dump(mode="json"))
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _snapshot_from_row(row: dict[str, Any]) -> FormalEvidenceAcceptanceSnapshot:
    try:
        snapshot = FormalEvidenceAcceptanceSnapshot.model_validate(row["snapshot"])
    except (KeyError, ValidationError, ValueError, TypeError) as error:
        raise SourceArtifactError("Formal acceptance Snapshot registration is malformed") from error
    expected_hash = formal_evidence_acceptance_snapshot_hash(snapshot)
    if (
        not hmac.compare_digest(str(row.get("snapshot_hash")), expected_hash)
        or row.get("round_id") != snapshot.round_id
        or row.get("readiness_audit_id") != snapshot.readiness_audit_id
    ):
        raise SourceArtifactError("Formal acceptance Snapshot registration changed")
    return snapshot


def _review_from_row(row: dict[str, Any]) -> FormalEvidenceAcceptanceReview:
    try:
        review = FormalEvidenceAcceptanceReview.model_validate(row["review"])
    except (KeyError, ValidationError, ValueError, TypeError) as error:
        raise SourceArtifactError("Formal acceptance Review registration is malformed") from error
    if (
        not hmac.compare_digest(str(row.get("review_hash")), review.review_hash)
        or row.get("review_id") != review.review_id
        or row.get("round_id") != review.round_id
        or row.get("readiness_audit_id") != review.readiness_audit_id
        or row.get("decision") != review.decision
        or row.get("snapshot_hash") != review.verification_input_digest
        or row.get("reviewed_at") != review.reviewed_at
    ):
        raise SourceArtifactError("Formal acceptance Review registration changed")
    return review


def _require_review_snapshot_binding(
    snapshot: FormalEvidenceAcceptanceSnapshot,
    snapshot_hash: str,
    review: FormalEvidenceAcceptanceReview,
) -> None:
    bound = (
        review.round_id == snapshot.round_id
        and review.readiness_audit_id == snapshot.readiness_audit_id
        and review.readiness_audit_base_commit == snapshot.readiness_audit_base_commit
        and review.readiness_manifest_hash == snapshot.readiness_manifest_hash
        and review.readiness_report_hash == snapshot.readiness_report_hash
        and review.target_lock_hash == snapshot.target_lock_hash
        and review.terminal_path == snapshot.terminal_path
        and review.authority_context_id == snapshot.authority_context_id
        and review.authority_context_hash == snapshot.authority_context_hash
        and review.round_evidence_bundle_id == snapshot.round_evidence_bundle_id
        and review.round_evidence_bundle_hash == snapshot.round_evidence_bundle_hash
        and review.signoff_artifact_hash == snapshot.signoff_artifact_hash
        and review.signoff_signer_identity_hash == snapshot.signoff_signer_identity_hash
        and review.evidence_root == snapshot.evidence_root
        and review.verifier == snapshot.verifier
        and review.verification_input_digest == snapshot_hash
    )
    if not bound:
        raise SourceArtifactError("Formal acceptance Review is bound to another Snapshot")


class FormalEvidenceAcceptanceRepositoryMixin:
    """Write-once PostgreSQL registry for deployment Snapshots and signed D Reviews."""

    def register_formal_evidence_acceptance_snapshot(
        self,
        snapshot: FormalEvidenceAcceptanceSnapshot,
    ) -> tuple[FormalEvidenceAcceptanceSnapshot, bool]:
        try:
            value = FormalEvidenceAcceptanceSnapshot.model_validate(
                snapshot.model_dump(mode="json")
            )
        except (AttributeError, ValidationError, ValueError, TypeError) as error:
            raise SourceArtifactError("Formal acceptance Snapshot is malformed") from error
        snapshot_hash = formal_evidence_acceptance_snapshot_hash(value)
        with self.connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                """
                INSERT INTO formal_evidence_acceptance_snapshots (
                    round_id, readiness_audit_id, snapshot_hash, snapshot
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    value.round_id,
                    value.readiness_audit_id,
                    snapshot_hash,
                    Jsonb(value.model_dump(mode="json")),
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                rows = connection.execute(
                    """
                    SELECT * FROM formal_evidence_acceptance_snapshots
                    WHERE (round_id = %s AND readiness_audit_id = %s)
                       OR snapshot_hash = %s
                    FOR UPDATE
                    """,
                    (value.round_id, value.readiness_audit_id, snapshot_hash),
                ).fetchall()
                row = rows[0] if len(rows) == 1 else None
            if row is None:
                raise Conflict("Formal acceptance Snapshot identity was reused")
            stored = _snapshot_from_row(row)
            if not hmac.compare_digest(
                canonical_json_bytes(stored), canonical_json_bytes(value)
            ):
                raise Conflict("Formal acceptance Snapshot identity was reused with other content")
        return stored, created

    def read_formal_evidence_acceptance_snapshot(
        self,
        *,
        round_id: UUID,
        readiness_audit_id: str,
    ) -> FormalEvidenceAcceptanceSnapshot:
        with self.connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                """
                SELECT * FROM formal_evidence_acceptance_snapshots
                WHERE round_id = %s AND readiness_audit_id = %s
                """,
                (round_id, readiness_audit_id),
            ).fetchone()
        if row is None:
            raise NotFound("Formal acceptance Snapshot is not registered")
        return _snapshot_from_row(row)

    def publish_formal_evidence_acceptance_review(
        self,
        review: FormalEvidenceAcceptanceReview,
        *,
        signature_verifier: FormalEvidenceAcceptanceReviewSignatureVerifier,
    ) -> tuple[FormalEvidenceAcceptanceReview, bool]:
        try:
            value = FormalEvidenceAcceptanceReview.model_validate(review.model_dump(mode="json"))
            verify_formal_evidence_acceptance_review_signature(value, signature_verifier)
        except Exception as error:
            raise SourceArtifactError(
                "Formal acceptance Review is malformed or its signature is not allowlisted"
            ) from error
        with self.connection() as connection:  # type: ignore[attr-defined]
            snapshot_row = connection.execute(
                """
                SELECT * FROM formal_evidence_acceptance_snapshots
                WHERE round_id = %s AND readiness_audit_id = %s
                FOR SHARE
                """,
                (value.round_id, value.readiness_audit_id),
            ).fetchone()
            if snapshot_row is None:
                raise NotFound("Formal acceptance Snapshot must be registered before its Review")
            snapshot = _snapshot_from_row(snapshot_row)
            snapshot_hash = formal_evidence_acceptance_snapshot_hash(snapshot)
            _require_review_snapshot_binding(snapshot, snapshot_hash, value)
            row = connection.execute(
                """
                INSERT INTO formal_evidence_acceptance_reviews (
                    review_id, review_hash, round_id, readiness_audit_id,
                    snapshot_hash, decision, review, reviewed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    value.review_id,
                    value.review_hash,
                    value.round_id,
                    value.readiness_audit_id,
                    snapshot_hash,
                    value.decision,
                    Jsonb(value.model_dump(mode="json")),
                    value.reviewed_at,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                rows = connection.execute(
                    """
                    SELECT * FROM formal_evidence_acceptance_reviews
                    WHERE review_id = %s OR review_hash = %s
                       OR (round_id = %s AND readiness_audit_id = %s)
                    FOR UPDATE
                    """,
                    (
                        value.review_id,
                        value.review_hash,
                        value.round_id,
                        value.readiness_audit_id,
                    ),
                ).fetchall()
                row = rows[0] if len(rows) == 1 else None
            if row is None:
                raise Conflict("Formal acceptance Review identity was reused")
            stored = _review_from_row(row)
            if not hmac.compare_digest(
                canonical_json_bytes(stored), canonical_json_bytes(value)
            ):
                raise Conflict("Formal acceptance Review identity was reused with other content")
        return stored, created

    def read_formal_evidence_acceptance_review(
        self,
        *,
        round_id: UUID,
        readiness_audit_id: str,
    ) -> FormalEvidenceAcceptanceReview:
        with self.connection() as connection:  # type: ignore[attr-defined]
            snapshot_row = connection.execute(
                """
                SELECT * FROM formal_evidence_acceptance_snapshots
                WHERE round_id = %s AND readiness_audit_id = %s
                """,
                (round_id, readiness_audit_id),
            ).fetchone()
            review_row = connection.execute(
                """
                SELECT * FROM formal_evidence_acceptance_reviews
                WHERE round_id = %s AND readiness_audit_id = %s
                """,
                (round_id, readiness_audit_id),
            ).fetchone()
        if snapshot_row is None or review_row is None:
            raise NotFound("Formal acceptance Review is not published")
        snapshot = _snapshot_from_row(snapshot_row)
        review = _review_from_row(review_row)
        _require_review_snapshot_binding(
            snapshot,
            formal_evidence_acceptance_snapshot_hash(snapshot),
            review,
        )
        return review


class FormalEvidenceAcceptancePersistenceRepository(Protocol):
    def register_formal_evidence_acceptance_snapshot(
        self, snapshot: FormalEvidenceAcceptanceSnapshot
    ) -> tuple[FormalEvidenceAcceptanceSnapshot, bool]: ...

    def read_formal_evidence_acceptance_snapshot(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceSnapshot: ...

    def publish_formal_evidence_acceptance_review(
        self,
        review: FormalEvidenceAcceptanceReview,
        *,
        signature_verifier: FormalEvidenceAcceptanceReviewSignatureVerifier,
    ) -> tuple[FormalEvidenceAcceptanceReview, bool]: ...

    def read_formal_evidence_acceptance_review(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceReview: ...


class DeploymentFormalEvidenceAcceptanceRegistry:
    """Deployment adapter used as the #142 Snapshot Reader and signed Review Store."""

    def __init__(
        self,
        repository: FormalEvidenceAcceptancePersistenceRepository,
        *,
        signature_verifier: FormalEvidenceAcceptanceReviewSignatureVerifier,
    ) -> None:
        self.repository = repository
        self.signature_verifier = signature_verifier

    def register(
        self, snapshot: FormalEvidenceAcceptanceSnapshot
    ) -> tuple[FormalEvidenceAcceptanceSnapshot, bool]:
        return self.repository.register_formal_evidence_acceptance_snapshot(snapshot)

    def read(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceSnapshot:
        return self.repository.read_formal_evidence_acceptance_snapshot(
            round_id=round_id,
            readiness_audit_id=readiness_audit_id,
        )

    def publish_review(
        self, review: FormalEvidenceAcceptanceReview
    ) -> tuple[FormalEvidenceAcceptanceReview, bool]:
        return self.repository.publish_formal_evidence_acceptance_review(
            review,
            signature_verifier=self.signature_verifier,
        )

    def read_review(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceReview:
        review = self.repository.read_formal_evidence_acceptance_review(
            round_id=round_id,
            readiness_audit_id=readiness_audit_id,
        )
        verify_formal_evidence_acceptance_review_signature(
            review, self.signature_verifier
        )
        return review


__all__ = [
    "DeploymentFormalEvidenceAcceptanceRegistry",
    "FormalEvidenceAcceptanceRepositoryMixin",
    "FormalEvidenceAcceptancePersistenceRepository",
    "formal_evidence_acceptance_snapshot_hash",
]
