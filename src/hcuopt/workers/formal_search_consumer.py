# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Claim-bound, non-measuring consumer for closing one Formal Search batch."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from hcuopt.contracts.m2 import SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import FormalAuthorityContextRef
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.formal_search import (
    SearchBarrierRepository,
    SearchEvidencePublisher,
    SearchEvidenceVerifier,
    close_formal_search,
)
from hcuopt.evaluation.m2_formal_authority import FormalBarrierPersistence
from hcuopt.evaluation.m2_models import BarrierMemberResult
from hcuopt.measurement.m2_models import RoundMeasurementRef
from hcuopt.storage.formal_claim import PostgresFormalClaimStore


@dataclass(frozen=True)
class FormalSearchBatchMaterials:
    """Durable batch snapshot; its reader owns all database and receipt reads."""

    round_authority: SearchRound
    context: FormalAuthorityContextRef
    members: tuple[BarrierMemberResult, ...]
    references: tuple[RoundMeasurementRef, ...]


class FormalSearchMaterialReader(Protocol):
    def load(self) -> FormalSearchBatchMaterials: ...


class FormalSearchConsumer:
    """Consume already-recorded Search receipts, never invoke B or reserve a lease."""

    def __init__(
        self,
        claims: PostgresFormalClaimStore,
        *,
        intent_id: UUID,
        worker_id: str,
        claim_token: UUID,
        material_reader: FormalSearchMaterialReader,
        verifier: SearchEvidenceVerifier,
        publisher: SearchEvidencePublisher,
        repository: SearchBarrierRepository,
        enabled: bool = False,
    ) -> None:
        self.claims = claims
        self.intent_id = intent_id
        self.worker_id = worker_id
        self.claim_token = claim_token
        self.material_reader = material_reader
        self.verifier = verifier
        self.publisher = publisher
        self.repository = repository
        self.enabled = enabled

    def execute_current_once(
        self, *, closed_by: str, closed_at, idempotency_key: str
    ) -> FormalBarrierPersistence:
        if not self.enabled:
            raise Conflict("Formal Search consumer is disabled")
        # D work has no HCU side effect, but must remain inside the same dispatch
        # claim. The repository owns the final transactional state recheck.
        self.claims.assert_active(self.intent_id, self.worker_id, self.claim_token)
        batch = self.material_reader.load()
        record = close_formal_search(
            round_authority=batch.round_authority,
            context=batch.context,
            members=batch.members,
            references=batch.references,
            verifier=self.verifier,
            publisher=self.publisher,
            repository=self.repository,
            closed_at=closed_at,
            closed_by=closed_by,
            idempotency_key=idempotency_key,
        )
        self.claims.assert_active(self.intent_id, self.worker_id, self.claim_token)
        return record
