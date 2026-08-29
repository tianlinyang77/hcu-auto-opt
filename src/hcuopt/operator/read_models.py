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
    OperatorEvaluationEvidenceWorkspace,
    OperatorFwerCandidateEvidence,
    OperatorFwerEvidence,
    OperatorHoldoutBarrierEvidence,
    OperatorHoldoutMemberEvidence,
    OperatorHoldoutRevealEvidence,
    OperatorRoundEvidenceBundleView,
    OperatorRoundReport,
    OperatorRoundSummary,
    OperatorSearchBarrierEvidence,
    OperatorSearchMemberEvidence,
    OperatorStartIntentView,
)
from hcuopt.domain.enums import (
    RoundBudgetEntryType,
    RoundCandidateState,
    RoundPhase,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.evaluation.m2_authority import HoldoutRevealResult
from hcuopt.evaluation.m2_models import (
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundEvidenceBundle,
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

    def evaluation_evidence(
        self,
        round_id: UUID,
        repository: OperatorReadModelRepository,
    ) -> OperatorEvaluationEvidenceWorkspace:
        """Expose batch evaluation authorities without recomputing their verdicts."""

        intent, authority, reconciliation = self._authorities(round_id, repository)
        summary = self._reduce(round_id, intent, authority, reconciliation)
        search = self._search_barrier(authority)
        reveal = self._holdout_reveal(authority)
        holdout = self._holdout_barrier(authority)
        fwer = self._multiple_comparison(authority)
        evidence = self._round_evidence(authority)
        self._require_evaluation_bindings(
            authority,
            search=search,
            reveal=reveal,
            holdout=holdout,
            fwer=fwer,
            evidence=evidence,
        )

        candidate_by_id = {item.candidate_id: item for item in authority.candidates}
        search_view = (
            self._search_view(search, candidate_by_id) if search is not None else None
        )
        reveal_view = self._reveal_view(reveal) if reveal is not None else None
        holdout_view = (
            self._holdout_view(holdout, candidate_by_id)
            if holdout is not None
            else None
        )
        fwer_view = (
            self._fwer_view(fwer, candidate_by_id) if fwer is not None else None
        )
        evidence_view = self._evidence_view(evidence) if evidence is not None else None

        promoted = bool(search and search.barrier.promoted_candidate_ids)
        if search is None:
            holdout_status = "pending"
            fwer_status = "pending"
        elif not promoted:
            holdout_status = "not_applicable"
            fwer_status = "not_applicable"
        else:
            holdout_status = (
                "available" if holdout is not None else "revealed" if reveal else "pending"
            )
            fwer_status = "available" if fwer is not None else "pending"

        return OperatorEvaluationEvidenceWorkspace(
            generated_at=self.clock(),
            intent_id=intent.intent_id,
            task_id=summary.task_id,
            round_id=summary.round_id,
            round_version=summary.round_version,
            round_state=summary.state,
            search_status="available" if search is not None else "pending",
            holdout_status=holdout_status,
            fwer_status=fwer_status,
            evidence_status="available" if evidence is not None else "pending",
            search=search_view,
            holdout_reveal=reveal_view,
            holdout=holdout_view,
            fwer=fwer_view,
            evidence_bundle=evidence_view,
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

    @staticmethod
    def _holdout_reveal(authority: SearchRoundSummary) -> HoldoutRevealResult | None:
        if authority.holdout_reveal is None:
            return None
        try:
            return HoldoutRevealResult.model_validate(authority.holdout_reveal)
        except ValueError as error:
            raise OperatorReadModelUnavailable(
                "Operator Holdout Reveal cannot be validated"
            ) from error

    @staticmethod
    def _holdout_barrier(authority: SearchRoundSummary) -> RoundBarrierResult | None:
        payloads = [
            payload for payload in authority.barriers if payload.get("phase") == "holdout"
        ]
        if len(payloads) > 1:
            raise OperatorReadModelUnavailable(
                "Operator Evaluation found multiple Holdout Barriers"
            )
        if not payloads:
            return None
        try:
            return RoundBarrierResult.model_validate(payloads[0])
        except ValueError as error:
            raise OperatorReadModelUnavailable(
                "Operator Holdout Barrier cannot be validated"
            ) from error

    @staticmethod
    def _multiple_comparison(
        authority: SearchRoundSummary,
    ) -> MultipleComparisonResult | None:
        if authority.multiple_comparison is None:
            return None
        try:
            return MultipleComparisonResult.model_validate(
                authority.multiple_comparison
            )
        except ValueError as error:
            raise OperatorReadModelUnavailable(
                "Operator FWER authority cannot be validated"
            ) from error

    @staticmethod
    def _round_evidence(authority: SearchRoundSummary) -> RoundEvidenceBundle | None:
        if authority.evidence_bundle is None:
            return None
        try:
            return RoundEvidenceBundle.model_validate(authority.evidence_bundle)
        except ValueError as error:
            raise OperatorReadModelUnavailable(
                "Operator Round EvidenceBundle cannot be validated"
            ) from error

    @staticmethod
    def _require_evaluation_bindings(
        authority: SearchRoundSummary,
        *,
        search: SearchBarrierDecision | None,
        reveal: HoldoutRevealResult | None,
        holdout: RoundBarrierResult | None,
        fwer: MultipleComparisonResult | None,
        evidence: RoundEvidenceBundle | None,
    ) -> None:
        round_authority = authority.round
        downstream = (reveal, holdout, fwer, evidence)
        if search is None:
            if any(item is not None for item in downstream):
                raise OperatorReadModelUnavailable(
                    "Operator Evaluation downstream authority exists without Search Barrier"
                )
            return

        barrier = search.barrier
        candidate_by_id = {item.candidate_id: item for item in authority.candidates}
        if (
            barrier.round_id != round_authority.round_id
            or barrier.phase is not RoundPhase.SEARCH
            or barrier.input_family_hash != round_authority.artifact_family_hash
            or barrier.rule_hash != round_authority.selection_rule_hash
            or set(candidate_by_id) != {item.candidate_id for item in barrier.members}
            or search.holdout_family_hash != round_authority.holdout_family_hash
        ):
            raise OperatorReadModelUnavailable(
                "Operator Search Barrier does not bind the frozen Round"
            )
        for member in barrier.members:
            stored = candidate_by_id[member.candidate_id]
            if (
                member.round_candidate_id != stored.round_candidate_id
                or member.artifact_id != stored.artifact_id
                or member.artifact_hash != stored.artifact_hash
                or (
                    stored.failure_evidence_hash is not None
                    and member.failure_evidence_hash != stored.failure_evidence_hash
                )
            ):
                raise OperatorReadModelUnavailable(
                    "Operator Search member identity drifted from Round Authority"
                )

        promoted = set(barrier.promoted_candidate_ids)
        if not promoted:
            if any(item is not None for item in (reveal, holdout, fwer)):
                raise OperatorReadModelUnavailable(
                    "zero-promotion Operator Round cannot carry Holdout or FWER"
                )
        else:
            if round_authority.holdout_family_hash is None:
                raise OperatorReadModelUnavailable(
                    "promoted Operator Round omits its Holdout Family"
                )
            round_has_reveal = all(
                value is not None
                for value in (
                    round_authority.holdout_plan_hash,
                    round_authority.holdout_reveal_lease_id,
                    round_authority.holdout_reveal_evidence_hash,
                )
            )
            if round_has_reveal != (reveal is not None):
                raise OperatorReadModelUnavailable(
                    "Operator Holdout Reveal payload and Round Authority disagree"
                )
            if reveal is not None and (
                reveal.round_id != round_authority.round_id
                or reveal.reveal_lease_id != round_authority.holdout_reveal_lease_id
                or reveal.holdout_family_hash != round_authority.holdout_family_hash
                or reveal.commitment != round_authority.holdout_plan_commitment
                or reveal.plan_hash != round_authority.holdout_plan_hash
                or reveal.reveal_evidence_hash
                != round_authority.holdout_reveal_evidence_hash
                or reveal.authority_id != round_authority.holdout_plan_authority_id
                or reveal.authority_hash != round_authority.holdout_plan_authority_hash
            ):
                raise OperatorReadModelUnavailable(
                    "Operator Holdout Reveal does not bind the frozen Round"
                )
            if holdout is not None:
                if reveal is None or (
                    holdout.round_id != round_authority.round_id
                    or holdout.phase is not RoundPhase.HOLDOUT
                    or holdout.input_family_hash != round_authority.holdout_family_hash
                    or promoted != {item.candidate_id for item in holdout.members}
                ):
                    raise OperatorReadModelUnavailable(
                        "Operator Holdout Barrier does not bind Reveal and promoted Family"
                    )
                for member in holdout.members:
                    stored = candidate_by_id[member.candidate_id]
                    if (
                        member.round_candidate_id != stored.round_candidate_id
                        or member.artifact_id != stored.artifact_id
                        or member.artifact_hash != stored.artifact_hash
                        or member.candidate_state != stored.state
                    ):
                        raise OperatorReadModelUnavailable(
                            "Operator Holdout member identity drifted from Round Authority"
                        )
            if fwer is not None and (
                holdout is None
                or fwer.round_id != round_authority.round_id
                or fwer.holdout_barrier_id != holdout.barrier_id
                or fwer.holdout_family_hash != round_authority.holdout_family_hash
                or fwer.family_alpha != round_authority.family_alpha
                or {item.candidate_id for item in fwer.candidate_results}
                != {item.candidate_id for item in holdout.members}
            ):
                raise OperatorReadModelUnavailable(
                    "Operator FWER does not bind the closed Holdout Barrier"
                )
            if fwer is not None and holdout is not None:
                holdout_by_id = {
                    item.candidate_id: item for item in holdout.members
                }
                for result in fwer.candidate_results:
                    member = holdout_by_id[result.candidate_id]
                    if (
                        result.correctness_evidence_hash
                        != member.correctness_evidence_hash
                        or result.scripted_phase_receipt_id
                        != member.scripted_phase_receipt_id
                    ):
                        raise OperatorReadModelUnavailable(
                            "Operator FWER Candidate evidence drifted from Holdout"
                        )

        if evidence is None:
            return
        if (
            evidence.round_id != round_authority.round_id
            or evidence.task_id != round_authority.task_id
            or evidence.candidate_family_hash != round_authority.candidate_family_hash
            or evidence.artifact_family_hash != round_authority.artifact_family_hash
            or evidence.holdout_family_hash != round_authority.holdout_family_hash
            or evidence.search_barrier_id != barrier.barrier_id
            or {item.candidate_id for item in evidence.candidate_evidence}
            != set(candidate_by_id)
        ):
            raise OperatorReadModelUnavailable(
                "Operator EvidenceBundle does not bind the frozen Round"
            )
        if promoted:
            if (
                holdout is None
                or fwer is None
                or evidence.holdout_barrier_id != holdout.barrier_id
                or evidence.multiple_comparison_id != fwer.multiple_comparison_id
            ):
                raise OperatorReadModelUnavailable(
                    "Operator EvidenceBundle omits Holdout or FWER authority"
                )
        elif (
            evidence.holdout_barrier_id is not None
            or evidence.multiple_comparison_id is not None
        ):
            raise OperatorReadModelUnavailable(
                "zero-promotion EvidenceBundle cannot reference Holdout or FWER"
            )
        search_by_id = {item.candidate_id: item for item in barrier.members}
        holdout_by_id = (
            {item.candidate_id: item for item in holdout.members}
            if holdout is not None
            else {}
        )
        for item in evidence.candidate_evidence:
            search_member = search_by_id[item.candidate_id]
            holdout_member = holdout_by_id.get(item.candidate_id)
            if (
                item.round_candidate_id != search_member.round_candidate_id
                or item.search_member_state != search_member.candidate_state
                or item.holdout_member_state
                != (holdout_member.candidate_state if holdout_member else None)
            ):
                raise OperatorReadModelUnavailable(
                    "Operator EvidenceBundle Candidate path drifted from Barriers"
                )

    @staticmethod
    def _search_view(
        decision: SearchBarrierDecision,
        candidate_by_id: dict[UUID, Any],
    ) -> OperatorSearchBarrierEvidence:
        statistics = {
            item.candidate_id: item for item in decision.input_summary.statistics
        }
        promoted = set(decision.barrier.promoted_candidate_ids)
        members = []
        for member in decision.barrier.members:
            stored = candidate_by_id[member.candidate_id]
            item = statistics.get(member.candidate_id)
            members.append(
                OperatorSearchMemberEvidence(
                    ordinal=stored.ordinal,
                    round_candidate_id=member.round_candidate_id,
                    candidate_id=member.candidate_id,
                    state=member.candidate_state,
                    promoted=member.candidate_id in promoted,
                    artifact_id=member.artifact_id,
                    artifact_hash=member.artifact_hash,
                    correctness_evidence_hash=member.correctness_evidence_hash,
                    scripted_phase_receipt_id=(
                        item.scripted_phase_receipt_id if item else None
                    ),
                    raw_evidence_hash=item.raw_evidence_hash if item else None,
                    baseline_sample_set_hash=(
                        item.baseline_sample_set_hash if item else None
                    ),
                    restart_effects=item.restart_effects if item else (),
                    stage0_mde_ratio=item.stage0_mde_ratio if item else None,
                    statistics_valid=(
                        item.bindings_valid
                        and item.correctness_passed
                        and item.cleanup_healthy
                        if item
                        else None
                    ),
                    failure_codes=item.failure_codes if item else (),
                    failure_evidence_hash=member.failure_evidence_hash,
                    budget_usage_evidence_hash=member.budget_usage_evidence_hash,
                    cleanup_evidence_hash=member.cleanup_evidence_hash,
                )
            )
        barrier = decision.barrier
        return OperatorSearchBarrierEvidence(
            barrier_id=barrier.barrier_id,
            outcome=barrier.outcome.value,
            input_family_hash=barrier.input_family_hash,
            input_summary_hash=barrier.input_summary_hash,
            rule_version=barrier.rule_version,
            rule_hash=barrier.rule_hash,
            expected_member_count=barrier.expected_member_count,
            promoted_candidate_ids=barrier.promoted_candidate_ids,
            holdout_family_hash=decision.holdout_family_hash,
            closed_by=barrier.closed_by,
            closed_at=barrier.closed_at,
            members=tuple(sorted(members, key=lambda item: item.ordinal)),
        )

    @staticmethod
    def _reveal_view(reveal: HoldoutRevealResult) -> OperatorHoldoutRevealEvidence:
        return OperatorHoldoutRevealEvidence(
            reveal_lease_id=reveal.reveal_lease_id,
            holdout_family_hash=reveal.holdout_family_hash,
            commitment=reveal.commitment,
            plan_hash=reveal.plan_hash,
            reveal_evidence_hash=reveal.reveal_evidence_hash,
            authority_id=reveal.authority_id,
            authority_hash=reveal.authority_hash,
            resource_id=reveal.resource_id,
            fencing_token=reveal.fencing_token,
            revealed_at=reveal.revealed_at,
        )

    @staticmethod
    def _holdout_view(
        barrier: RoundBarrierResult,
        candidate_by_id: dict[UUID, Any],
    ) -> OperatorHoldoutBarrierEvidence:
        members = []
        for member in barrier.members:
            stored = candidate_by_id[member.candidate_id]
            assert member.artifact_id is not None
            assert member.artifact_hash is not None
            assert member.correctness_evidence_hash is not None
            members.append(
                OperatorHoldoutMemberEvidence(
                    ordinal=stored.ordinal,
                    round_candidate_id=member.round_candidate_id,
                    candidate_id=member.candidate_id,
                    state=member.candidate_state,
                    artifact_id=member.artifact_id,
                    artifact_hash=member.artifact_hash,
                    correctness_evidence_hash=member.correctness_evidence_hash,
                    scripted_phase_receipt_id=member.scripted_phase_receipt_id,
                    failure_evidence_hash=member.failure_evidence_hash,
                    budget_usage_evidence_hash=member.budget_usage_evidence_hash,
                    cleanup_evidence_hash=member.cleanup_evidence_hash,
                )
            )
        return OperatorHoldoutBarrierEvidence(
            barrier_id=barrier.barrier_id,
            input_family_hash=barrier.input_family_hash,
            input_summary_hash=barrier.input_summary_hash,
            rule_version=barrier.rule_version,
            rule_hash=barrier.rule_hash,
            expected_member_count=barrier.expected_member_count,
            closed_by=barrier.closed_by,
            closed_at=barrier.closed_at,
            members=tuple(sorted(members, key=lambda item: item.ordinal)),
        )

    @staticmethod
    def _fwer_view(
        result: MultipleComparisonResult,
        candidate_by_id: dict[UUID, Any],
    ) -> OperatorFwerEvidence:
        candidates = tuple(
            OperatorFwerCandidateEvidence(
                ordinal=candidate_by_id[item.candidate_id].ordinal,
                candidate_id=item.candidate_id,
                scripted_phase_receipt_id=item.scripted_phase_receipt_id,
                correctness_evidence_hash=item.correctness_evidence_hash,
                raw_evidence_hash=item.raw_evidence_hash,
                baseline_sample_set_hash=item.baseline_sample_set_hash,
                verdict=item.verdict.value,
                adjusted_ci_lower=item.adjusted_ci_lower,
                adjusted_ci_upper=item.adjusted_ci_upper,
                stage0_mde_ratio=item.stage0_mde_ratio,
                workload_mde_ratio=item.workload_mde_ratio,
                credible_threshold=item.credible_threshold,
                failure_codes=item.failure_codes,
            )
            for item in sorted(
                result.candidate_results,
                key=lambda candidate: candidate_by_id[candidate.candidate_id].ordinal,
            )
        )
        return OperatorFwerEvidence(
            multiple_comparison_id=result.multiple_comparison_id,
            holdout_barrier_id=result.holdout_barrier_id,
            holdout_family_hash=result.holdout_family_hash,
            protocol_version=result.protocol_version,
            protocol_hash=result.protocol_hash,
            family_alpha=result.family_alpha,
            m=result.m,
            alpha_candidate=result.alpha_candidate,
            result_hash=result.result_hash,
            recommended_candidate_id=result.recommended_candidate_id,
            created_at=result.created_at,
            candidates=candidates,
        )

    @staticmethod
    def _evidence_view(
        evidence: RoundEvidenceBundle,
    ) -> OperatorRoundEvidenceBundleView:
        return OperatorRoundEvidenceBundleView(
            round_evidence_bundle_id=evidence.round_evidence_bundle_id,
            terminal_reason=evidence.terminal_reason.value,
            candidate_family_hash=evidence.candidate_family_hash,
            artifact_family_hash=evidence.artifact_family_hash,
            holdout_family_hash=evidence.holdout_family_hash,
            search_barrier_id=evidence.search_barrier_id,
            holdout_barrier_id=evidence.holdout_barrier_id,
            multiple_comparison_id=evidence.multiple_comparison_id,
            budget_ledger_hash=evidence.budget_ledger_hash,
            evidence_index_uri=evidence.evidence_index_uri,
            evidence_index_hash=evidence.evidence_index_hash,
            summary=evidence.summary,
            created_at=evidence.created_at,
        )
