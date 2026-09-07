# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceAcceptanceReport,
    FormalEvidenceAcceptanceReview,
)
from hcuopt.domain.errors import NotFound
from hcuopt.evaluation.formal_evidence_acceptance import (
    FormalEvidenceAcceptanceReviewSignatureVerifier,
    FormalEvidenceAcceptanceSnapshot,
    verify_formal_evidence_acceptance_review_signature,
)
from hcuopt.storage.formal_evidence_acceptance import (
    formal_evidence_acceptance_snapshot_hash,
)


class FormalEvidenceAcceptanceReportError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FormalEvidenceAcceptanceReportRepository(Protocol):
    def read_formal_evidence_acceptance_snapshot(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceSnapshot: ...

    def read_formal_evidence_acceptance_review(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceReview: ...


class FormalEvidenceAcceptanceReportService:
    """Revalidates a persisted signed D Review before returning a read-only report."""

    def __init__(
        self,
        repository: FormalEvidenceAcceptanceReportRepository,
        signature_verifier: FormalEvidenceAcceptanceReviewSignatureVerifier,
    ) -> None:
        self.repository = repository
        self.signature_verifier = signature_verifier

    def get(
        self, *, round_id: UUID, readiness_audit_id: str
    ) -> FormalEvidenceAcceptanceReport:
        try:
            snapshot = self.repository.read_formal_evidence_acceptance_snapshot(
                round_id=round_id,
                readiness_audit_id=readiness_audit_id,
            )
            review = self.repository.read_formal_evidence_acceptance_review(
                round_id=round_id,
                readiness_audit_id=readiness_audit_id,
            )
            snapshot_hash = formal_evidence_acceptance_snapshot_hash(snapshot)
            verify_formal_evidence_acceptance_review_signature(
                review, self.signature_verifier
            )
            return FormalEvidenceAcceptanceReport(
                round_id=round_id,
                readiness_audit_id=readiness_audit_id,
                snapshot_hash=snapshot_hash,
                review=review,
            )
        except NotFound:
            raise
        except Exception as error:
            raise FormalEvidenceAcceptanceReportError(
                "formal_evidence_acceptance_record_invalid",
                "Persisted Formal acceptance evidence failed closed validation",
            ) from error


__all__ = [
    "FormalEvidenceAcceptanceReportError",
    "FormalEvidenceAcceptanceReportRepository",
    "FormalEvidenceAcceptanceReportService",
]
