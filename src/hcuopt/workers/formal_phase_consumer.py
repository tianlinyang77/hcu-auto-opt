# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in consumption of one already prepared phase; never scans or acquires HCU resources."""

from pathlib import Path

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
from hcuopt.storage.formal_phase_journal import PostgresFormalPhaseJournal
from hcuopt.workers.formal_checkpoint import FormalClaimCheckpoint


class FormalPhaseConsumer:
    def __init__(
        self,
        journal: PostgresFormalPhaseJournal,
        adapter: M2FormalPhaseExecutionAdapter,
        *,
        enabled: bool = False,
    ) -> None:
        self.journal, self.adapter, self.enabled = journal, adapter, enabled

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
        checkpoint = FormalClaimCheckpoint(
            self.journal.claims,
            self.journal.intent_id,
            self.journal.worker_id,
            self.journal.claim_token,
        )
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
