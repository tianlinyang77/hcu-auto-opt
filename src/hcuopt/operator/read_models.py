# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from hcuopt.contracts.m2 import SearchRoundReconcileResult, SearchRoundSummary
from hcuopt.contracts.operator_v1 import (
    OperatorCandidateBuildEvidence,
    OperatorCandidateCorrectnessEvidence,
    OperatorCandidateEvidence,
    OperatorCandidateEvidenceWorkspace,
    OperatorCandidateSourceEvidence,
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
from hcuopt.evaluation.m2_statistics import SearchBarrierDecision
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

    def candidate_evidence(
        self,
        round_id: UUID,
        repository: OperatorReadModelRepository,
    ) -> OperatorCandidateEvidenceWorkspace:
        """Expose source, Build, and correctness evidence without inventing state."""

        intent, authority, reconciliation = self._authorities(round_id, repository)
        summary = self._reduce(round_id, intent, authority, reconciliation)
        search_barrier = self._search_barrier(authority)
        search_members = (
            {item.candidate_id: item for item in search_barrier.barrier.members}
            if search_barrier is not None
            else {}
        )
        candidates: list[OperatorCandidateEvidence] = []
        for start_member, round_member in zip(
            intent.candidate_members, authority.candidates, strict=True
        ):
            self._require_source_binding(start_member, round_member)
            build = self._build_evidence(round_member)
            correctness = self._correctness_evidence(
                round_member,
                build,
                search_barrier,
                search_members.get(round_member.candidate_id),
            )
            candidates.append(
                OperatorCandidateEvidence(
                    ordinal=round_member.ordinal,
                    round_candidate_id=round_member.round_candidate_id,
                    candidate_id=round_member.candidate_id,
                    state=round_member.state,
                    source=OperatorCandidateSourceEvidence(
                        candidate_input_digest=start_member.candidate_input_digest,
                        source_package_store_id=start_member.source_package_store_id,
                        source_package_store_version=(
                            start_member.source_package_store_version
                        ),
                        source_package_store_hash=(
                            start_member.source_package_store_hash
                        ),
                        source_package_ref=start_member.source_package_ref,
                        baseline_source_hash=start_member.baseline_source_hash,
                        hotspot_id=start_member.hotspot_id,
                        replacement_point=start_member.replacement_point,
                        track=round_member.track,
                        release_mode=round_member.release_mode,
                        candidate_kind=start_member.candidate_kind,
                        optimization_intent=start_member.optimization_intent,
                    ),
                    build=build,
                    correctness=correctness,
                )
            )
        return OperatorCandidateEvidenceWorkspace(
            generated_at=self.clock(),
            intent_id=intent.intent_id,
            task_id=summary.task_id,
            round_id=summary.round_id,
            round_version=summary.round_version,
            round_state=summary.state,
            candidates=tuple(candidates),
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

    @staticmethod
    def _search_barrier(authority: SearchRoundSummary) -> SearchBarrierDecision | None:
        payloads = [
            payload
            for payload in authority.barriers
            if payload.get("barrier", {}).get("phase") == "search"
        ]
        if len(payloads) > 1:
            raise OperatorReadModelUnavailable(
                "Operator Candidate evidence found multiple Search Barriers"
            )
        if not payloads:
            return None
        try:
            decision = SearchBarrierDecision.model_validate(payloads[0])
        except ValueError as error:
            raise OperatorReadModelUnavailable(
                "Operator Candidate Search Barrier cannot be validated"
            ) from error
        if decision.barrier.round_id != authority.round.round_id:
            raise OperatorReadModelUnavailable(
                "Operator Candidate Search Barrier belongs to another Round"
            )
        return decision

    @staticmethod
    def _require_source_binding(start_member: Any, round_member: Any) -> None:
        source = start_member.source_package_ref
        if (
            start_member.ordinal != round_member.ordinal
            or start_member.candidate_id != round_member.candidate_id
            or start_member.round_candidate_id != round_member.round_candidate_id
            or start_member.source_package_store_id
            != round_member.source_package_store_id
            or start_member.source_package_store_hash
            != round_member.source_package_store_hash
            or source.source_package_hash != round_member.source_package_hash
            or source.manifest_schema_version != round_member.source_manifest_version
            or source.manifest_hash != round_member.source_manifest_hash
            or start_member.baseline_source_hash != round_member.baseline_source_hash
            or source.candidate_source_hash != round_member.candidate_source_hash
            or start_member.replacement_point != round_member.replacement_point
            or start_member.candidate_kind != round_member.candidate_kind.value
            or start_member.optimization_intent != round_member.optimization_intent
        ):
            raise OperatorReadModelUnavailable(
                "Operator Candidate source authority bindings do not match"
            )

    @staticmethod
    def _build_evidence(round_member: Any) -> OperatorCandidateBuildEvidence:
        has_artifact = (
            round_member.artifact_id is not None
            and round_member.artifact_hash is not None
        )
        has_failure = (
            round_member.terminal_failure_code is not None
            and round_member.failure_evidence_hash is not None
        )
        if has_artifact:
            return OperatorCandidateBuildEvidence(
                status="available",
                artifact_id=round_member.artifact_id,
                artifact_hash=round_member.artifact_hash,
            )
        if has_failure:
            return OperatorCandidateBuildEvidence(
                status="failed",
                terminal_failure_code=round_member.terminal_failure_code,
                failure_evidence_hash=round_member.failure_evidence_hash,
            )
        if round_member.state not in {
            RoundCandidateState.INTAKE_ACCEPTED,
            RoundCandidateState.BUILDING,
        }:
            raise OperatorReadModelUnavailable(
                "Operator Candidate terminal Build evidence is incomplete"
            )
        return OperatorCandidateBuildEvidence(status="pending")

    @staticmethod
    def _correctness_evidence(
        round_member: Any,
        build: OperatorCandidateBuildEvidence,
        search_barrier: SearchBarrierDecision | None,
        search_member: Any | None,
    ) -> OperatorCandidateCorrectnessEvidence:
        if search_barrier is None:
            if build.status == "pending":
                reason = "build_not_terminal"
            elif build.status == "failed":
                reason = "build_failed"
            else:
                reason = "awaiting_search_barrier"
            return OperatorCandidateCorrectnessEvidence(
                status="not_available",
                authority="not_available",
                reason=reason,
            )
        if search_member is None:
            raise OperatorReadModelUnavailable(
                "Operator Candidate is missing from the Search Barrier"
            )
        if (
            search_member.round_candidate_id != round_member.round_candidate_id
            or search_member.artifact_id != round_member.artifact_id
            or search_member.artifact_hash != round_member.artifact_hash
        ):
            raise OperatorReadModelUnavailable(
                "Operator Candidate Search Barrier identity does not match Build Authority"
            )
        if search_member.candidate_state in {
            RoundCandidateState.BUILD_FAILED,
            RoundCandidateState.INVALID,
        }:
            if search_member.failure_evidence_hash != round_member.failure_evidence_hash:
                raise OperatorReadModelUnavailable(
                    "Operator Candidate Build failure evidence changed in Search Barrier"
                )
            return OperatorCandidateCorrectnessEvidence(
                status="not_available",
                authority="not_available",
                reason="build_failed",
            )
        if search_member.candidate_state is RoundCandidateState.CORRECTNESS_FAILED:
            return OperatorCandidateCorrectnessEvidence(
                status="failed",
                authority="search_barrier",
                reason="correctness_failed",
                search_barrier_id=search_barrier.barrier.barrier_id,
                failure_evidence_hash=search_member.failure_evidence_hash,
            )
        if search_member.correctness_evidence_hash is None:
            raise OperatorReadModelUnavailable(
                "Operator Candidate Search Barrier omits correctness evidence"
            )
        return OperatorCandidateCorrectnessEvidence(
            status="passed",
            authority="search_barrier",
            reason="correctness_passed",
            search_barrier_id=search_barrier.barrier.barrier_id,
            correctness_evidence_hash=search_member.correctness_evidence_hash,
        )
