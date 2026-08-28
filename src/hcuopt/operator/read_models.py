# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from hcuopt.contracts.m2 import SearchRoundReconcileResult, SearchRoundSummary
from hcuopt.contracts.operator_v1 import (
    OperatorCandidateSummary,
    OperatorRoundReport,
    OperatorRoundSummary,
    OperatorStartIntentView,
)
from hcuopt.domain.enums import (
    RoundBudgetEntryType,
    RoundCandidateState,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.operator.errors import OperatorReadModelUnavailable


class OperatorReadModelRepository(Protocol):
    def list_operator_round_ids(self, limit: int) -> tuple[UUID, ...]: ...

    def get_operator_start_intent_by_round_id(
        self, round_id: UUID
    ) -> OperatorStartIntentView: ...

    def search_round_summary(self, round_id: UUID) -> dict[str, Any]: ...

    def reconcile_scripted_search_round(self, round_id: UUID) -> dict[str, Any]: ...


class OperatorReadModelService:
    """Reduce existing authorities into a versioned, read-only Operator view."""

    _BUILD_TERMINAL_STATES = {
        RoundCandidateState.BUILT,
        RoundCandidateState.BUILD_FAILED,
        RoundCandidateState.INVALID,
        RoundCandidateState.CORRECTNESS_FAILED,
        RoundCandidateState.CORRECTNESS_PASSED,
        RoundCandidateState.SEARCH_FAILED,
        RoundCandidateState.SEARCH_MEASURED,
        RoundCandidateState.NOT_PROMOTED,
        RoundCandidateState.HOLDOUT_FAILED,
        RoundCandidateState.HOLDOUT_MEASURED,
    }
    _TERMINAL_ROUND_STATES = {
        SearchRoundState.SCRIPTED_COMPLETED,
        SearchRoundState.CANCELLED,
    }

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def summary(
        self,
        round_id: UUID,
        repository: OperatorReadModelRepository,
    ) -> OperatorRoundSummary:
        intent, authority, reconciliation = self._authorities(round_id, repository)
        return self._reduce(round_id, intent, authority, reconciliation)

    def summaries(
        self,
        repository: OperatorReadModelRepository,
        *,
        limit: int,
    ) -> list[OperatorRoundSummary]:
        """List recent executable Scripted Rounds through the same reducer."""

        round_ids = repository.list_operator_round_ids(limit)
        return [self.summary(round_id, repository) for round_id in round_ids]

    def report(
        self,
        round_id: UUID,
        repository: OperatorReadModelRepository,
    ) -> OperatorRoundReport:
        intent, authority, reconciliation = self._authorities(round_id, repository)
        summary = self._reduce(round_id, intent, authority, reconciliation)
        return OperatorRoundReport(
            generated_at=self.clock(),
            report_status="final" if summary.terminal else "interim",
            summary=summary,
            round_authority=authority.round.model_dump(mode="json"),
            evidence_bundle=authority.evidence_bundle,
        )

    @staticmethod
    def _authorities(
        round_id: UUID,
        repository: OperatorReadModelRepository,
    ) -> tuple[
        OperatorStartIntentView,
        SearchRoundSummary,
        SearchRoundReconcileResult,
    ]:
        intent = repository.get_operator_start_intent_by_round_id(round_id)
        authority = SearchRoundSummary.model_validate(
            repository.search_round_summary(round_id)
        )
        reconciliation = SearchRoundReconcileResult.model_validate(
            repository.reconcile_scripted_search_round(round_id)
        )
        return intent, authority, reconciliation

    def _reduce(
        self,
        round_id: UUID,
        intent: OperatorStartIntentView,
        authority: SearchRoundSummary,
        reconciliation: SearchRoundReconcileResult,
    ) -> OperatorRoundSummary:
        round_authority = authority.round
        if (
            intent.round_id != round_id
            or intent.task_id != round_authority.task_id
            or intent.state != "finalized"
            or round_authority.run_mode is not SearchRoundRunMode.SCRIPTED
            or reconciliation.round.round_id != round_id
            or reconciliation.round.version != round_authority.version
            or intent.candidate_family_hash != round_authority.candidate_family_hash
            or len(authority.candidates) != round_authority.declared_candidate_count
            or tuple(member.round_candidate_id for member in intent.candidate_members)
            != tuple(member.round_candidate_id for member in authority.candidates)
        ):
            raise OperatorReadModelUnavailable(
                "Operator Read Model authority bindings do not match"
            )

        candidates = tuple(
            OperatorCandidateSummary(
                ordinal=member.ordinal,
                round_candidate_id=member.round_candidate_id,
                candidate_id=member.candidate_id,
                state=member.state,
                artifact_id=member.artifact_id,
                artifact_hash=member.artifact_hash,
                terminal_failure_code=member.terminal_failure_code,
                failure_evidence_hash=member.failure_evidence_hash,
            )
            for member in authority.candidates
        )
        settled = sum(
            entry.entry_type is RoundBudgetEntryType.SETTLE
            for entry in authority.budget_ledger
        )
        return OperatorRoundSummary(
            generated_at=self.clock(),
            intent_id=intent.intent_id,
            task_id=intent.task_id,
            round_id=round_id,
            round_version=round_authority.version,
            state=round_authority.state,
            next_action=reconciliation.next_action,
            reason=reconciliation.reason,
            candidates=candidates,
            candidate_count=len(candidates),
            build_terminal_count=sum(
                member.state in self._BUILD_TERMINAL_STATES for member in candidates
            ),
            settled_budget_entry_count=settled,
            evidence_status=(
                "available" if authority.evidence_bundle is not None else "not_available"
            ),
            terminal=round_authority.state in self._TERMINAL_ROUND_STATES,
        )
