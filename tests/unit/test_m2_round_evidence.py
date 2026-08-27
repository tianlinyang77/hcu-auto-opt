# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from hcuopt.contracts.m2 import RoundBudget, SearchRound
from hcuopt.domain.enums import RoundCandidateState, RoundTerminalReason, SearchRoundState
from hcuopt.evaluation.m2_authority import SyntheticHoldoutPlanAuthority
from hcuopt.evaluation.m2_finalizer import M2ScriptedRoundFinalizer
from hcuopt.evaluation.m2_models import (
    BarrierMemberResult,
    MultipleComparisonResult,
    RoundBarrierResult,
)
from hcuopt.evaluation.m2_statistics import (
    ScriptedCandidateStatisticsInput,
    SearchBarrierDecision,
    bonferroni_fwer,
    close_scripted_holdout_barrier,
    close_scripted_search_barrier,
)
from hcuopt.evaluation.m2_verifier import (
    M2EvidenceIndex,
    M2EvidenceIndexEntry,
    M2RoundEvidenceError,
    PublishedM2EvidenceIndex,
    SyntheticEvidenceStore,
    build_scripted_round_evidence,
    publish_scripted_evidence_index,
    require_formal_round_signoff,
    scripted_round_evidence_requirements,
)

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)
PLAN = {
    "protocol_version": "m2-scripted-holdout-v1",
    "cases": [{"shape": [1, 2048], "dtype": "float16", "seed": 20260827}],
}


@dataclass(frozen=True, slots=True)
class _Scenario:
    store: SyntheticEvidenceStore
    round_authority: SearchRound
    search_decision: SearchBarrierDecision
    budget_ledger_hash: str
    holdout_barrier: RoundBarrierResult | None = None
    multiple_comparison: MultipleComparisonResult | None = None


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _raw(store: SyntheticEvidenceStore, label: str):
    return store.publish({"label": label, "synthetic": True})


def _round(
    *,
    store: SyntheticEvidenceStore,
    authority: SyntheticHoldoutPlanAuthority,
    commitment: str,
) -> SearchRound:
    return SearchRound(
        round_id=_uuid(1),
        task_id=_uuid(2),
        idempotency_key="m2-round-evidence-idempotency",
        state=SearchRoundState.SEARCH_BARRIER,
        run_mode="scripted",
        project_mode=None,
        target_snapshot_id=_uuid(3),
        stage0_run_id=_uuid(4),
        stage0_protocol_hash=_raw(store, "stage0-protocol").sha256,
        baseline_epoch_id=_uuid(5),
        hotspot_id=_uuid(6),
        replacement_point="sglang.srt.layers.fixture",
        workload_id="scripted-workload",
        workload_hash=_raw(store, "workload").sha256,
        configuration_hash=_raw(store, "configuration").sha256,
        image_digest=_raw(store, "image").sha256,
        adapter_profile="m2-scripted-v1",
        declared_candidate_count=2,
        max_promoted=2,
        family_alpha=0.05,
        search_plan_hash=_raw(store, "search-plan").sha256,
        holdout_plan_commitment=commitment,
        holdout_plan_authority_id=authority.authority_id,
        holdout_plan_authority_hash=authority.authority_hash,
        selection_rule_hash=_raw(store, "selection-rule").sha256,
        budget=RoundBudget(
            max_candidates=2,
            max_build_attempts=4,
            max_correctness_attempts=4,
            max_search_samples=200,
            max_holdout_samples=200,
            max_wall_seconds=600,
            max_exclusive_lease_seconds=300,
        ),
        candidate_family_hash=_raw(store, "candidate-family").sha256,
        artifact_family_hash=_raw(store, "artifact-family").sha256,
        version=4,
        created_at=NOW,
        intake_closed_at=NOW,
    )


def _copy_round(round_authority: SearchRound, **updates: object) -> SearchRound:
    values = round_authority.model_dump(mode="python")
    values.update(updates)
    return SearchRound.model_validate(values)


def _member(
    *,
    store: SyntheticEvidenceStore,
    ordinal: int,
    phase: str,
    state: RoundCandidateState,
    search_member: BarrierMemberResult | None = None,
) -> BarrierMemberResult:
    measured = state in {
        RoundCandidateState.SEARCH_MEASURED,
        RoundCandidateState.HOLDOUT_MEASURED,
    }
    if search_member is None:
        artifact_id = (
            _uuid(300 + ordinal) if state is not RoundCandidateState.BUILD_FAILED else None
        )
        artifact_hash = (
            _raw(store, f"artifact-{ordinal}").sha256 if artifact_id is not None else None
        )
        correctness_hash = _raw(store, f"correctness-{ordinal}").sha256 if measured else None
    else:
        artifact_id = search_member.artifact_id
        artifact_hash = search_member.artifact_hash
        correctness_hash = search_member.correctness_evidence_hash
    return BarrierMemberResult(
        round_candidate_id=_uuid(100 + ordinal),
        candidate_id=_uuid(200 + ordinal),
        candidate_state=state,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        correctness_evidence_hash=correctness_hash,
        scripted_phase_receipt_id=(
            _uuid((400 if phase == "search" else 500) + ordinal) if measured else None
        ),
        failure_evidence_hash=(
            None if measured else _raw(store, f"failure-{phase}-{ordinal}").sha256
        ),
        budget_usage_evidence_hash=_raw(store, f"budget-usage-{phase}-{ordinal}").sha256,
        cleanup_evidence_hash=(
            _raw(store, f"cleanup-{phase}-{ordinal}").sha256 if measured else None
        ),
        synthetic=True,
    )


def _statistics(
    *,
    store: SyntheticEvidenceStore,
    member: BarrierMemberResult,
    phase: str,
    effects: tuple[float, ...],
) -> ScriptedCandidateStatisticsInput:
    return ScriptedCandidateStatisticsInput(
        candidate_id=member.candidate_id,
        scripted_phase_receipt_id=member.scripted_phase_receipt_id,
        correctness_evidence_hash=member.correctness_evidence_hash,
        raw_evidence_hash=_raw(store, f"raw-{phase}-{member.candidate_id}").sha256,
        baseline_sample_set_hash=_raw(store, f"baseline-{phase}-{member.candidate_id}").sha256,
        restart_effects=effects,
        baseline_restart_means_ns=(100.0, 100.0, 100.0, 100.0),
        stage0_mde_ratio=0.03,
    )


def _search(
    round_authority: SearchRound,
    members: tuple[BarrierMemberResult, ...],
    statistics: tuple[ScriptedCandidateStatisticsInput, ...],
) -> SearchBarrierDecision:
    return close_scripted_search_barrier(
        round_authority=round_authority,
        expected_candidate_ids=(item.candidate_id for item in members),
        members=members,
        statistics=statistics,
        closed_by="m2-scripted-evaluation-authority",
        closed_at=NOW,
        idempotency_key="m2-search-evidence-barrier",
    )


def _zero_scenario() -> _Scenario:
    store = SyntheticEvidenceStore()
    authority = SyntheticHoldoutPlanAuthority(
        authority_id="m2-scripted-holdout-store",
        authority_version="1.0.0",
    )
    commitment = authority.commit_plan(
        round_id=_uuid(1), plan=PLAN, nonce=bytes(range(32)), created_at=NOW
    )
    round_authority = _round(
        store=store,
        authority=authority,
        commitment=commitment.commitment,
    )
    measured = _member(
        store=store,
        ordinal=0,
        phase="search",
        state=RoundCandidateState.SEARCH_MEASURED,
    )
    failed = _member(
        store=store,
        ordinal=1,
        phase="search",
        state=RoundCandidateState.BUILD_FAILED,
    )
    decision = _search(
        round_authority,
        (measured, failed),
        (
            _statistics(
                store=store,
                member=measured,
                phase="search",
                effects=(-0.10,) * 4,
            ),
        ),
    )
    return _Scenario(
        store=store,
        round_authority=round_authority,
        search_decision=decision,
        budget_ledger_hash=_raw(store, "budget-ledger-zero").sha256,
    )


def _holdout_scenario() -> _Scenario:
    store = SyntheticEvidenceStore()
    authority = SyntheticHoldoutPlanAuthority(
        authority_id="m2-scripted-holdout-store",
        authority_version="1.0.0",
    )
    commitment = authority.commit_plan(
        round_id=_uuid(1), plan=PLAN, nonce=bytes(range(32)), created_at=NOW
    )
    search_round = _round(
        store=store,
        authority=authority,
        commitment=commitment.commitment,
    )
    search_members = tuple(
        _member(
            store=store,
            ordinal=index,
            phase="search",
            state=RoundCandidateState.SEARCH_MEASURED,
        )
        for index in range(2)
    )
    decision = _search(
        search_round,
        search_members,
        tuple(
            _statistics(
                store=store,
                member=member,
                phase="search",
                effects=(0.10 + index * 0.05,) * 4,
            )
            for index, member in enumerate(search_members)
        ),
    )
    assert decision.holdout_family_hash is not None
    revealable_round = _copy_round(
        search_round,
        holdout_family_hash=decision.holdout_family_hash,
        version=5,
    )
    reveal_lease_id = _uuid(700)
    execution_lease_id = _uuid(701)
    authority.issue_reveal_lease(
        round_authority=revealable_round,
        authorized_worker_id="m2-scripted-measurement-worker",
        execution_lease_id=execution_lease_id,
        resource_id="scripted-resource",
        fencing_token=9,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        reveal_lease_id=reveal_lease_id,
    )
    reveal = authority.reveal(
        reveal_lease_id=reveal_lease_id,
        round_id=search_round.round_id,
        authorized_worker_id="m2-scripted-measurement-worker",
        execution_lease_id=execution_lease_id,
        resource_id="scripted-resource",
        fencing_token=9,
        revealed_at=NOW,
    )
    assert store.publish_bytes(reveal.canonical_plan_json.encode()).sha256 == reveal.plan_hash
    assert store.publish(reveal.evidence_payload()).sha256 == reveal.reveal_evidence_hash
    holdout_round = _copy_round(
        revealable_round,
        state=SearchRoundState.HOLDOUT_BARRIER,
        holdout_plan_hash=reveal.plan_hash,
        holdout_reveal_lease_id=reveal.reveal_lease_id,
        holdout_reveal_evidence_hash=reveal.reveal_evidence_hash,
        version=6,
    )
    holdout_members = tuple(
        _member(
            store=store,
            ordinal=index,
            phase="holdout",
            state=RoundCandidateState.HOLDOUT_MEASURED,
            search_member=search_members[index],
        )
        for index in range(2)
    )
    holdout_barrier = close_scripted_holdout_barrier(
        round_authority=holdout_round,
        expected_candidate_ids=(item.candidate_id for item in holdout_members),
        members=holdout_members,
        closed_by="m2-scripted-evaluation-authority",
        closed_at=NOW,
        idempotency_key="m2-holdout-evidence-barrier",
    )
    multiple = bonferroni_fwer(
        round_authority=holdout_round,
        holdout_barrier=holdout_barrier,
        statistics=tuple(
            _statistics(
                store=store,
                member=member,
                phase="holdout",
                effects=(0.20,) * 4,
            )
            for member in holdout_members
        ),
        created_at=NOW,
    )
    return _Scenario(
        store=store,
        round_authority=holdout_round,
        search_decision=decision,
        budget_ledger_hash=_raw(store, "budget-ledger-holdout").sha256,
        holdout_barrier=holdout_barrier,
        multiple_comparison=multiple,
    )


def _publish_index(scenario: _Scenario) -> PublishedM2EvidenceIndex:
    return publish_scripted_evidence_index(
        store=scenario.store,
        round_id=scenario.round_authority.round_id,
        requirements=scripted_round_evidence_requirements(
            round_authority=scenario.round_authority,
            search_decision=scenario.search_decision,
            budget_ledger_hash=scenario.budget_ledger_hash,
            holdout_barrier=scenario.holdout_barrier,
            multiple_comparison=scenario.multiple_comparison,
        ),
        producer="m2-scripted-evaluation-authority",
        retention_owner="m2-scripted-test",
        created_at=NOW,
    )


def _build(scenario: _Scenario, published: PublishedM2EvidenceIndex):
    return build_scripted_round_evidence(
        round_authority=scenario.round_authority,
        search_decision=scenario.search_decision,
        budget_ledger_hash=scenario.budget_ledger_hash,
        evidence_index_uri=published.artifact.uri,
        evidence_index_hash=published.artifact.sha256,
        evidence_reader=scenario.store,
        holdout_barrier=scenario.holdout_barrier,
        multiple_comparison=scenario.multiple_comparison,
    )


def test_zero_promotion_bundle_skips_holdout_and_replays_byte_for_byte() -> None:
    scenario = _zero_scenario()
    published = _publish_index(scenario)

    first = _build(scenario, published)
    repeated = _build(scenario, published)

    assert first == repeated
    assert first.terminal_reason is RoundTerminalReason.NO_PROMOTABLE_CANDIDATE
    assert first.holdout_family_hash is None
    assert first.holdout_barrier_id is None
    assert first.multiple_comparison_id is None
    assert first.summary["performance_conclusion"] == "not_measured"
    assert len(first.candidate_evidence) == 2
    with pytest.raises(M2RoundEvidenceError) as signoff:
        require_formal_round_signoff(
            round_authority=scenario.round_authority,
            evidence=first,
        )
    assert signoff.value.code == "synthetic_round_not_signable"


def test_holdout_bundle_binds_every_family_barrier_fwer_budget_and_cleanup() -> None:
    scenario = _holdout_scenario()
    published = _publish_index(scenario)

    bundle = _build(scenario, published)

    assert bundle.terminal_reason is RoundTerminalReason.HOLDOUT_COMPLETED
    assert bundle.holdout_family_hash == scenario.round_authority.holdout_family_hash
    assert bundle.holdout_plan_hash == scenario.round_authority.holdout_plan_hash
    assert bundle.holdout_barrier_id == scenario.holdout_barrier.barrier_id
    assert bundle.multiple_comparison_id == (scenario.multiple_comparison.multiple_comparison_id)
    assert bundle.summary["fixture_verdict_counts"] == {"faster": 2}
    assert all(len(item.budget_evidence_hashes) == 2 for item in bundle.candidate_evidence)
    assert all(len(item.cleanup_evidence_hashes) == 2 for item in bundle.candidate_evidence)


def test_recursive_index_is_verified_and_missing_or_tampered_evidence_fails() -> None:
    scenario = _holdout_scenario()
    flat = _publish_index(scenario)
    by_role = {item.role: item for item in flat.index.entries}
    child_roles = tuple(
        sorted(role for role in by_role if role.startswith("candidate/") and "/search/" in role)
    )
    parent = by_role["search/barrier"]
    nested_parent = M2EvidenceIndexEntry.model_validate(
        {
            **parent.model_dump(mode="python"),
            "children": tuple(by_role[role] for role in child_roles),
        }
    )
    root_entries = [
        item
        for item in flat.index.entries
        if item.role not in child_roles and item.role != parent.role
    ] + [nested_parent]
    nested_index = M2EvidenceIndex(
        round_id=scenario.round_authority.round_id,
        entries=tuple(sorted(root_entries, key=lambda item: item.role)),
        created_at=NOW,
    )
    nested = PublishedM2EvidenceIndex(
        index=nested_index,
        artifact=scenario.store.publish(nested_index),
    )
    assert _build(scenario, nested).evidence_index_hash == nested.artifact.sha256

    missing_index = M2EvidenceIndex(
        round_id=scenario.round_authority.round_id,
        entries=flat.index.entries[:-1],
        created_at=NOW,
    )
    missing = PublishedM2EvidenceIndex(
        index=missing_index,
        artifact=scenario.store.publish(missing_index),
    )
    with pytest.raises(M2RoundEvidenceError) as incomplete:
        _build(scenario, missing)
    assert incomplete.value.code == "evidence_index_incomplete"

    target_uri = flat.index.entries[0].uri

    class _TamperingReader:
        def read_raw_bytes(self, uri: str, expected_hash: str) -> bytes:
            encoded = scenario.store.read_raw_bytes(uri, expected_hash)
            return b"tampered" if uri == target_uri else encoded

    with pytest.raises(M2RoundEvidenceError) as tampered:
        build_scripted_round_evidence(
            round_authority=scenario.round_authority,
            search_decision=scenario.search_decision,
            budget_ledger_hash=scenario.budget_ledger_hash,
            evidence_index_uri=flat.artifact.uri,
            evidence_index_hash=flat.artifact.sha256,
            evidence_reader=_TamperingReader(),
            holdout_barrier=scenario.holdout_barrier,
            multiple_comparison=scenario.multiple_comparison,
        )
    assert tampered.value.code == "evidence_hash_mismatch"


def test_search_baseline_or_fwer_identity_change_is_invalid() -> None:
    scenario = _holdout_scenario()
    published = _publish_index(scenario)
    changed_hash = _raw(scenario.store, "changed-baseline").sha256
    changed_statistics = scenario.search_decision.input_summary.statistics[0].model_copy(
        update={"baseline_sample_set_hash": changed_hash}
    )
    changed_summary = scenario.search_decision.input_summary.model_copy(
        update={
            "statistics": (
                changed_statistics,
                *scenario.search_decision.input_summary.statistics[1:],
            )
        }
    )
    changed_decision = scenario.search_decision.model_copy(
        update={"input_summary": changed_summary}
    )
    with pytest.raises(M2RoundEvidenceError) as search_mismatch:
        build_scripted_round_evidence(
            round_authority=scenario.round_authority,
            search_decision=changed_decision,
            budget_ledger_hash=scenario.budget_ledger_hash,
            evidence_index_uri=published.artifact.uri,
            evidence_index_hash=published.artifact.sha256,
            evidence_reader=scenario.store,
            holdout_barrier=scenario.holdout_barrier,
            multiple_comparison=scenario.multiple_comparison,
        )
    assert search_mismatch.value.code == "round_evidence_binding_mismatch"

    assert scenario.multiple_comparison is not None
    first = scenario.multiple_comparison.candidate_results[0].model_copy(
        update={"baseline_sample_set_hash": changed_hash}
    )
    changed_multiple = scenario.multiple_comparison.model_copy(
        update={
            "candidate_results": (
                first,
                *scenario.multiple_comparison.candidate_results[1:],
            )
        }
    )
    with pytest.raises(M2RoundEvidenceError) as fwer_mismatch:
        build_scripted_round_evidence(
            round_authority=scenario.round_authority,
            search_decision=scenario.search_decision,
            budget_ledger_hash=scenario.budget_ledger_hash,
            evidence_index_uri=published.artifact.uri,
            evidence_index_hash=published.artifact.sha256,
            evidence_reader=scenario.store,
            holdout_barrier=scenario.holdout_barrier,
            multiple_comparison=changed_multiple,
        )
    assert fwer_mismatch.value.code == "holdout_evidence_binding_mismatch"


def test_scripted_finalizer_rebuilds_bundle_and_rejects_substitution() -> None:
    scenario = _zero_scenario()
    published = _publish_index(scenario)
    bundle = _build(scenario, published)
    finalizer = M2ScriptedRoundFinalizer(scenario.store)

    finalizer.verify(
        round_authority=scenario.round_authority,
        search_decision=scenario.search_decision,
        evidence_bundle=bundle,
        holdout_barrier=None,
        multiple_comparison=None,
    )

    changed = bundle.model_copy(
        update={"summary": {**bundle.summary, "candidate_count": 999}}
    )
    with pytest.raises(M2RoundEvidenceError) as mismatch:
        finalizer.verify(
            round_authority=scenario.round_authority,
            search_decision=scenario.search_decision,
            evidence_bundle=changed,
            holdout_barrier=None,
            multiple_comparison=None,
        )
    assert mismatch.value.code == "round_evidence_bundle_mismatch"
