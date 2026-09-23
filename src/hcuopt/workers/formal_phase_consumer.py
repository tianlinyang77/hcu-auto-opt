# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in consumption of one already prepared phase; never scans or acquires HCU resources."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import FormalAuthorityContextDescriptor
from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalPhaseExecutionReceipt,
    M2FormalPhaseExecutionReceiptRef,
    M2FormalPhaseExecutionRequest,
)
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.m2_formal_runner import (
    M2FormalExecutionFailure,
    M2FormalPhaseExecutionAdapter,
)
from hcuopt.measurement.m2_models import M2PhaseBudgetReservationPlan
from hcuopt.storage.formal_phase_journal import PostgresFormalPhaseJournal
from hcuopt.storage.formal_phase_materials import (
    FormalPhaseMaterials,
    PostgresFormalPhaseMaterialReader,
)
from hcuopt.workers.formal_checkpoint import FormalClaimCheckpoint
from hcuopt.workers.formal_phase_prepare import prepare_formal_phase_request


class FormalPhaseConsumer:
    def __init__(
        self,
        journal: PostgresFormalPhaseJournal,
        adapter: M2FormalPhaseExecutionAdapter,
        *,
        enabled: bool = False,
        material_reader: PostgresFormalPhaseMaterialReader | None = None,
    ) -> None:
        if material_reader is not None and material_reader.journal is not journal:
            raise Conflict("Formal material reader belongs to another phase journal")
        self.journal, self.adapter, self.enabled = journal, adapter, enabled
        self.material_reader = material_reader

    def execute_current_once(
        self,
        *,
        candidate_id: UUID,
        authorization_id: UUID,
        resolved_plan_hash: str,
        reservation: M2PhaseBudgetReservationPlan,
        deployment: Mapping[str, Any],
        output_dir: Path,
        harness_payload: Mapping[str, Any] | None = None,
    ) -> M2FormalPhaseExecutionReceipt:
        """Prepare from durable records, with a re-read at every B checkpoint."""
        if not self.enabled:
            raise Conflict("Formal phase consumer is disabled")
        if self.material_reader is None:
            raise Conflict("Formal current execution requires a deployment material reader")
        materials = self.material_reader.load(candidate_id)
        return self.prepare_and_execute_once(
            round_authority=materials.round_authority,
            formal_authority=materials.formal_authority,
            member=materials.member,
            authorization_id=authorization_id,
            resolved_plan_hash=resolved_plan_hash,
            reservation=reservation,
            deployment=deployment,
            output_dir=output_dir,
            harness_payload=harness_payload,
        )

    def prepare_and_execute_once(
        self,
        *,
        round_authority: SearchRound,
        formal_authority: FormalAuthorityContextDescriptor,
        member: RoundCandidate,
        authorization_id: UUID,
        resolved_plan_hash: str,
        reservation: M2PhaseBudgetReservationPlan,
        deployment: Mapping[str, Any],
        output_dir: Path,
        harness_payload: Mapping[str, Any] | None = None,
    ) -> M2FormalPhaseExecutionReceipt:
        """Deployment-only composition; invalid materials do not consume a phase slot.

        For historical receipt replay after authority expiry use execute_once with
        the exact persisted request, not a newly prepared request.
        """
        if not self.enabled:
            raise Conflict("Formal phase consumer is disabled")
        request = prepare_formal_phase_request(
            adapter=self.adapter,
            round_authority=round_authority,
            formal_authority=formal_authority,
            member=member,
            authorization_id=authorization_id,
            resolved_plan_hash=resolved_plan_hash,
            reservation=reservation,
            deployment=deployment,
            harness_payload=harness_payload,
        )
        return self.execute_once(
            round_authority=round_authority,
            formal_authority=formal_authority,
            member=member,
            request=request,
            output_dir=output_dir,
        )

    def execute_once(
        self,
        *,
        round_authority: SearchRound,
        formal_authority: FormalAuthorityContextDescriptor,
        member: RoundCandidate,
        request: M2FormalPhaseExecutionRequest,
        output_dir: Path,
    ) -> M2FormalPhaseExecutionReceipt:
        """Return a terminal receipt, including failed receipts; not an acceptance verdict."""
        if not self.enabled:
            raise Conflict("Formal phase consumer is disabled")
        row, acquired = self.journal.begin(request)
        if not acquired:
            if row["state"] != "receipt_recorded":
                raise Conflict("Formal phase invocation is uncertain; recovery required, no retry")
            return self.journal.receipt_store.load_for_request(
                M2FormalPhaseExecutionReceiptRef.model_validate(row["receipt_ref"]),
                request,
            )
        claim_checkpoint = FormalClaimCheckpoint(
            self.journal.claims,
            self.journal.intent_id,
            self.journal.worker_id,
            self.journal.claim_token,
        )

        def checkpoint(current_request: M2FormalPhaseExecutionRequest) -> None:
            claim_checkpoint(current_request)
            if self.material_reader is not None:
                current = self.material_reader.load(current_request.binding.candidate_id)
                expected = FormalPhaseMaterials(round_authority, formal_authority, member)
                if current != expected:
                    raise Conflict("Formal phase durable materials changed; stop execution")

        try:
            try:
                outcome = self.adapter.run(
                    round_authority=round_authority,
                    formal_authority=formal_authority,
                    member=member,
                    request=request,
                    output_dir=output_dir,
                    execution_checkpoint=checkpoint,
                )
                reference = outcome.receipt_ref
            except M2FormalExecutionFailure as error:
                # B has already settled usage and produced a terminal failure receipt.
                reference = error.receipt_ref
            self.journal.record_receipt(request, reference)
            return self.journal.receipt_store.load_for_request(reference, request)
        except Exception:
            # Includes unknown adapter errors and receipt/journal write failures.
            # If this write also fails, the prior invoking fact still forbids another call.
            self.journal.mark_unknown(request)
            raise
