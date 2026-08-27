# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hcuopt.contracts.m2 import (
    BudgetUsage,
    RoundBudget,
    RoundBudgetMutationResult,
    RoundBudgetReservationView,
    RoundCandidate,
    SearchRound,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.enums import (
    RoundBudgetEntryType,
    RoundBudgetReservationState,
    RoundCandidateState,
    RoundPhase,
    SearchRoundState,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.measurement.m2_models import (
    M2PhaseBudgetReservationPlan,
    M2PhaseExecutionContext,
    M2PhaseMeasurementRequest,
    M2ScriptedHarnessResult,
    M2ScriptedPhaseEvidence,
)
from hcuopt.measurement.m2_runner import (
    M2PhaseHarnessFailure,
    M2PhaseIsolationRegistry,
    M2PhaseMeasurementRunner,
)
from hcuopt.source_hash import file_uri_to_path

PROFILE = "m2-scripted-measurement-v1"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _round(phase: RoundPhase) -> SearchRound:
    values: dict[str, Any] = {
        "round_id": uuid4(),
        "task_id": uuid4(),
        "idempotency_key": "m2-phase-round-idempotency",
        "state": (
            SearchRoundState.SEARCH_MEASURING
            if phase is RoundPhase.SEARCH
            else SearchRoundState.HOLDOUT_MEASURING
        ),
        "run_mode": "scripted",
        "project_mode": None,
        "target_snapshot_id": uuid4(),
        "stage0_run_id": uuid4(),
        "stage0_protocol_hash": _hash("stage0"),
        "baseline_epoch_id": uuid4(),
        "hotspot_id": uuid4(),
        "replacement_point": "sglang.fixture_kernel",
        "workload_id": "m2-scripted-workload-v1",
        "workload_hash": _hash("workload"),
        "configuration_hash": _hash("configuration"),
        "image_digest": _hash("image"),
        "adapter_profile": PROFILE,
        "declared_candidate_count": 2,
        "max_promoted": 1,
        "family_alpha": 0.05,
        "search_plan_hash": _hash("search-phase-plan"),
        "holdout_plan_commitment": _hash("holdout-commitment"),
        "holdout_plan_authority_id": "synthetic-holdout-authority",
        "holdout_plan_authority_hash": _hash("holdout-authority"),
        "selection_rule_hash": _hash("selection"),
        "budget": RoundBudget(
            max_candidates=2,
            max_build_attempts=2,
            max_correctness_attempts=2,
            max_search_samples=100,
            max_holdout_samples=100,
            max_wall_seconds=100,
            max_exclusive_lease_seconds=100,
        ),
        "candidate_family_hash": _hash("candidate-family"),
        "artifact_family_hash": _hash("artifact-family"),
        "version": 1,
        "created_at": NOW,
    }
    if phase is RoundPhase.HOLDOUT:
        values.update(
            {
                "holdout_family_hash": _hash("holdout-family"),
                "holdout_plan_hash": _hash("holdout-phase-plan"),
                "holdout_reveal_lease_id": uuid4(),
                "holdout_reveal_evidence_hash": _hash("holdout-reveal"),
            }
        )
    return SearchRound.model_validate(values)


def _member(round_authority: SearchRound, phase: RoundPhase) -> RoundCandidate:
    return RoundCandidate(
        round_candidate_id=uuid4(),
        round_id=round_authority.round_id,
        candidate_id=uuid4(),
        ordinal=0,
        source_package_store_id="m2-scripted-package-store",
        source_package_store_hash=_hash("store"),
        source_package_hash=_hash("package"),
        source_manifest_hash=_hash("manifest"),
        baseline_source_hash=_hash("baseline-source"),
        candidate_source_hash=_hash("candidate-source"),
        optimization_intent="scripted phase fixture",
        replacement_point=round_authority.replacement_point,
        candidate_kind="fixture",
        artifact_id=uuid4(),
        artifact_hash=_hash("artifact"),
        state=(
            RoundCandidateState.CORRECTNESS_PASSED
            if phase is RoundPhase.SEARCH
            else RoundCandidateState.SEARCH_MEASURED
        ),
        idempotency_key="m2-phase-candidate-idempotency",
    )


def _request(
    round_authority: SearchRound,
    member: RoundCandidate,
    phase: RoundPhase,
    *,
    suffix: str = "one",
) -> M2PhaseMeasurementRequest:
    samples = 8
    planned = BudgetUsage(
        search_samples=samples if phase is RoundPhase.SEARCH else 0,
        holdout_samples=samples if phase is RoundPhase.HOLDOUT else 0,
        wall_seconds=20,
        exclusive_lease_seconds=20,
    )
    phase_plan_hash = (
        round_authority.search_plan_hash
        if phase is RoundPhase.SEARCH
        else round_authority.holdout_plan_hash
    )
    assert phase_plan_hash is not None
    return M2PhaseMeasurementRequest(
        reservation=M2PhaseBudgetReservationPlan(
            round_id=round_authority.round_id,
            round_candidate_id=member.round_candidate_id,
            candidate_id=member.candidate_id,
            phase=phase,
            phase_plan_hash=phase_plan_hash,
            measurement_plan_hash=_hash(f"measurement-plan-{phase.value}"),
            expected_sample_count=samples,
            reservation_id=uuid4(),
            job_id=uuid4(),
            attempt=1,
            planned=planned,
            idempotency_key=f"m2-phase-budget-{suffix}",
        ),
        execution=M2PhaseExecutionContext(
            lease_id=uuid4(),
            resource_id="hcu-7",
            fencing_token=9,
            lease_acquired_monotonic_ns=1_000_000_000,
            job_started_monotonic_ns=2_000_000_000,
        ),
        holdout_reveal_evidence_hash=(
            round_authority.holdout_reveal_evidence_hash
            if phase is RoundPhase.HOLDOUT
            else None
        ),
        harness_payload={"fixture": suffix},
    )


def _result(
    tmp_path: Path,
    request: M2PhaseMeasurementRequest,
    *,
    suffix: str = "one",
    cleanup_healthy: bool = True,
) -> M2ScriptedHarnessResult:
    measurement_id = uuid4()
    baseline_sample_set_hash = _hash(f"baseline-samples-{suffix}")
    process_identity_set_hash = _hash(f"processes-{suffix}")
    cache_namespace_set_hash = _hash(f"caches-{suffix}")
    cleanup_evidence = {
        "fence": {
            "fenced": True,
            "resource_id": request.execution.resource_id,
            "fencing_token": request.execution.fencing_token,
        },
        "health": {
            "healthy": cleanup_healthy,
            "resource_id": request.execution.resource_id,
        },
    }
    provenance = AdapterProvenance(
        profile=PROFILE,
        capability="measurement_harness",
        adapter_name="ScriptedUniqueHarness",
        adapter_version="1.0.0",
        implementation_kind="fake",
    )
    raw = tmp_path / f"raw-{suffix}.json"
    encoded = canonical_json_bytes(
        M2ScriptedPhaseEvidence(
            round_id=request.reservation.round_id,
            round_candidate_id=request.reservation.round_candidate_id,
            candidate_id=request.reservation.candidate_id,
            phase=request.reservation.phase,
            phase_plan_hash=request.reservation.phase_plan_hash,
            measurement_plan_hash=request.reservation.measurement_plan_hash,
            measurement_id=measurement_id,
            sample_count=request.reservation.expected_sample_count,
            baseline_sample_set_hash=baseline_sample_set_hash,
            process_identity_set_hash=process_identity_set_hash,
            cache_namespace_set_hash=cache_namespace_set_hash,
            cleanup_evidence=cleanup_evidence,
            adapter_provenance=provenance,
        )
    )
    raw.write_bytes(encoded)
    values: dict[str, Any] = {
        "measurement_id": measurement_id,
        "raw_evidence_uri": raw.as_uri(),
        "raw_evidence_hash": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "measurement_plan_hash": request.reservation.measurement_plan_hash,
        "sample_count": request.reservation.expected_sample_count,
        "baseline_sample_set_hash": baseline_sample_set_hash,
        "process_identity_set_hash": process_identity_set_hash,
        "cache_namespace_set_hash": cache_namespace_set_hash,
        "cleanup_evidence": cleanup_evidence,
        "adapter_provenance": provenance,
    }
    return M2ScriptedHarnessResult.model_validate(values)


class RecordingBudgetAuthority:
    def __init__(self, *, reject_reserve: bool = False) -> None:
        self.reject_reserve = reject_reserve
        self.reserve_requests = []
        self.finalize_requests = []
        self.reservations: dict[Any, RoundBudgetReservationView] = {}

    def reserve(self, request):  # type: ignore[no-untyped-def]
        if self.reject_reserve:
            raise MeasurementSafetyError("round budget exhausted")
        self.reserve_requests.append(request)
        view = RoundBudgetReservationView(
            **request.reservation.model_dump(mode="python"),
            created_at=NOW,
            updated_at=NOW,
        )
        self.reservations[view.reservation_id] = view
        return RoundBudgetMutationResult(
            reservation=view, ledger_entry=request.ledger_entry
        )

    def finalize(self, request):  # type: ignore[no-untyped-def]
        self.finalize_requests.append(request)
        entry = request.ledger_entry
        state = (
            RoundBudgetReservationState.SETTLED
            if entry.entry_type is RoundBudgetEntryType.SETTLE
            else RoundBudgetReservationState.RELEASED
        )
        view = self.reservations[entry.reservation_id].model_copy(
            update={"state": state, "updated_at": NOW}
        )
        self.reservations[entry.reservation_id] = view
        return RoundBudgetMutationResult(reservation=view, ledger_entry=entry)


class RecordingHarness:
    def __init__(
        self,
        result: M2ScriptedHarnessResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.payloads: list[dict[str, Any]] = []

    def run_manual_performance(self, payload, output_dir):  # type: ignore[no-untyped-def]
        del output_dir
        self.payloads.append(dict(payload))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class TickClock:
    def __init__(self) -> None:
        self.value = 2_000_000_000

    def __call__(self) -> int:
        self.value += 1_000_000_000
        return self.value


def _runner(
    harness: RecordingHarness,
    budget: RecordingBudgetAuthority,
    *,
    registry: M2PhaseIsolationRegistry | None = None,
    fence_is_live=None,  # type: ignore[no-untyped-def]
) -> M2PhaseMeasurementRunner:
    return M2PhaseMeasurementRunner(
        harness=harness,
        budget_authority=budget,
        isolation_registry=registry,
        fence_is_live=fence_is_live,
        monotonic_ns=TickClock(),
    )


def test_search_phase_reserves_measures_cleans_and_settles(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    result = _result(tmp_path, request)
    harness = RecordingHarness(result)
    budget = RecordingBudgetAuthority()

    outcome = _runner(harness, budget).run(
        round_authority=round_authority,
        member=member,
        request=request,
        output_dir=tmp_path / "evidence",
    )

    assert outcome.reference.phase is RoundPhase.SEARCH
    assert outcome.reference.holdout_family_hash is None
    assert outcome.reference.producer_verdict is None
    assert outcome.reference.synthetic is True
    assert harness.payloads[0]["phase"] == "search"
    assert harness.payloads[0]["artifact_hash"] == member.artifact_hash
    settle = budget.finalize_requests[0].ledger_entry
    assert settle.entry_type is RoundBudgetEntryType.SETTLE
    assert settle.actual.search_samples == 8
    assert settle.actual.holdout_samples == 0
    assert settle.harness_active_seconds == 1
    assert settle.lease_held_seconds == 4
    assert file_uri_to_path(outcome.usage_evidence_uri).is_file()


def test_search_and_holdout_use_distinct_phase_and_evidence_identities(
    tmp_path: Path,
) -> None:
    registry = M2PhaseIsolationRegistry()
    search_round = _round(RoundPhase.SEARCH)
    search_member = _member(search_round, RoundPhase.SEARCH)
    search_request = _request(search_round, search_member, RoundPhase.SEARCH)
    search_result = _result(tmp_path, search_request, suffix="search")
    _runner(
        RecordingHarness(search_result),
        RecordingBudgetAuthority(),
        registry=registry,
    ).run(
        round_authority=search_round,
        member=search_member,
        request=search_request,
        output_dir=tmp_path / "search",
    )

    holdout_round = _round(RoundPhase.HOLDOUT).model_copy(
        update={"round_id": search_round.round_id}
    )
    holdout_member = _member(holdout_round, RoundPhase.HOLDOUT)
    holdout_request = _request(
        holdout_round, holdout_member, RoundPhase.HOLDOUT, suffix="holdout"
    )
    holdout_result = _result(tmp_path, holdout_request, suffix="holdout")
    outcome = _runner(
        RecordingHarness(holdout_result),
        RecordingBudgetAuthority(),
        registry=registry,
    ).run(
        round_authority=holdout_round,
        member=holdout_member,
        request=holdout_request,
        output_dir=tmp_path / "holdout",
    )

    assert outcome.reference.phase is RoundPhase.HOLDOUT
    assert outcome.reference.holdout_family_hash == holdout_round.holdout_family_hash
    assert (
        outcome.reference.holdout_reveal_evidence_hash
        == holdout_round.holdout_reveal_evidence_hash
    )
    assert outcome.settlement.ledger_entry.actual.holdout_samples == 8


@pytest.mark.parametrize(
    "field",
    [
        "measurement_id",
        "raw_evidence_uri",
        "raw_evidence_hash",
        "baseline_sample_set_hash",
        "process_identity_set_hash",
        "cache_namespace_set_hash",
    ],
)
def test_reused_phase_evidence_identity_fails_closed(
    tmp_path: Path, field: str
) -> None:
    registry = M2PhaseIsolationRegistry()
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    first_request = _request(round_authority, member, RoundPhase.SEARCH, suffix="first")
    first_result = _result(tmp_path, first_request, suffix="first")
    first_outcome = _runner(
        RecordingHarness(first_result),
        RecordingBudgetAuthority(),
        registry=registry,
    ).run(
        round_authority=round_authority,
        member=member,
        request=first_request,
        output_dir=tmp_path / "first",
    )

    # Build a second reference with only one reused identity, leaving all earlier
    # uniqueness keys distinct so the stable field-specific rejection is observable.
    second_request = _request(round_authority, member, RoundPhase.SEARCH, suffix="second")
    second_result = _result(tmp_path, second_request, suffix="second")
    second_outcome = _runner(
        RecordingHarness(second_result), RecordingBudgetAuthority()
    ).run(
        round_authority=round_authority,
        member=member,
        request=second_request,
        output_dir=tmp_path / f"candidate-{field}",
    )
    reused_reference = second_outcome.reference.model_copy(
        update={field: getattr(first_outcome.reference, field)}
    )
    with pytest.raises(MeasurementSafetyError, match=f"reused {field}"):
        registry.validate(reused_reference)


def test_partial_sampling_is_settled_but_never_returns_measured_ref(
    tmp_path: Path,
) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    failure = M2PhaseHarnessFailure(
        "scripted process exited after partial sampling",
        error_code="partial_sampling",
        actual_sample_count=3,
        raw_evidence_uri="fixture:///partial.json",
        raw_evidence_hash=_hash("partial"),
        cleanup_evidence={
            "fence": {
                "fenced": True,
                "resource_id": "hcu-7",
                "fencing_token": 9,
            },
            "health": {"healthy": True, "resource_id": "hcu-7"},
        },
    )
    budget = RecordingBudgetAuthority()

    with pytest.raises(M2PhaseHarnessFailure, match="partial sampling"):
        _runner(RecordingHarness(error=failure), budget).run(
            round_authority=round_authority,
            member=member,
            request=request,
            output_dir=tmp_path / "partial",
        )

    settle = budget.finalize_requests[0].ledger_entry
    assert settle.actual.search_samples == 3
    assert settle.entry_type is RoundBudgetEntryType.SETTLE


def test_cleanup_failure_is_charged_and_cannot_be_measured(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    result = _result(tmp_path, request, cleanup_healthy=False)
    budget = RecordingBudgetAuthority()

    with pytest.raises(MeasurementSafetyError, match="lacks healthy cleanup"):
        _runner(RecordingHarness(result), budget).run(
            round_authority=round_authority,
            member=member,
            request=request,
            output_dir=tmp_path / "cleanup-failure",
        )

    assert budget.finalize_requests[0].ledger_entry.actual.search_samples == 8


def test_raw_evidence_tamper_is_charged_and_cannot_create_ref(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    result = _result(tmp_path, request)
    file_uri_to_path(result.raw_evidence_uri).write_text("tampered", encoding="utf-8")
    budget = RecordingBudgetAuthority()

    with pytest.raises(MeasurementSafetyError, match="raw evidence Hash"):
        _runner(RecordingHarness(result), budget).run(
            round_authority=round_authority,
            member=member,
            request=request,
            output_dir=tmp_path / "raw-tamper",
        )

    assert budget.finalize_requests[0].ledger_entry.actual.search_samples == 8


def test_budget_rejection_prevents_harness_execution(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    harness = RecordingHarness(_result(tmp_path, request))
    budget = RecordingBudgetAuthority(reject_reserve=True)

    with pytest.raises(MeasurementSafetyError, match="budget exhausted"):
        _runner(harness, budget).run(
            round_authority=round_authority,
            member=member,
            request=request,
            output_dir=tmp_path / "budget-rejected",
        )

    assert harness.payloads == []
    assert budget.finalize_requests == []


def test_stale_fence_prevents_reservation_and_execution(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    harness = RecordingHarness(_result(tmp_path, request))
    budget = RecordingBudgetAuthority()

    with pytest.raises(MeasurementSafetyError, match="stale Fence"):
        _runner(
            harness,
            budget,
            fence_is_live=lambda _lease, _resource, _token: False,
        ).run(
            round_authority=round_authority,
            member=member,
            request=request,
            output_dir=tmp_path / "stale",
        )

    assert budget.reserve_requests == []
    assert harness.payloads == []


def test_fence_lost_after_harness_is_settled_without_a_ref(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    harness = RecordingHarness(_result(tmp_path, request))
    budget = RecordingBudgetAuthority()
    calls = iter((True, False))

    with pytest.raises(MeasurementSafetyError, match="became stale"):
        _runner(
            harness,
            budget,
            fence_is_live=lambda _lease, _resource, _token: next(calls),
        ).run(
            round_authority=round_authority,
            member=member,
            request=request,
            output_dir=tmp_path / "fence-lost",
        )

    assert budget.finalize_requests[0].ledger_entry.actual.search_samples == 8


def test_unstarted_reservation_uses_release_not_settle(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    harness = RecordingHarness(_result(tmp_path, request))
    budget = RecordingBudgetAuthority()

    reserved, released = _runner(harness, budget).release_unstarted(
        reservation=request.reservation,
        output_dir=tmp_path / "released",
        reason="cancelled_before_execution",
    )

    assert reserved.reservation.state is RoundBudgetReservationState.RESERVED
    assert released.reservation.state is RoundBudgetReservationState.RELEASED
    assert released.ledger_entry.entry_type is RoundBudgetEntryType.RELEASE
    assert released.ledger_entry.actual.is_zero()
    assert harness.payloads == []


def test_phase_plan_rejects_cross_phase_sample_budget() -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    invalid = request.reservation.model_dump(mode="python")
    invalid["planned"] = BudgetUsage(
        holdout_samples=8,
        wall_seconds=20,
        exclusive_lease_seconds=20,
    )

    with pytest.raises(ValidationError, match="sample reservation"):
        M2PhaseBudgetReservationPlan.model_validate(invalid)


def test_holdout_cannot_reuse_search_phase_plan(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.HOLDOUT).model_copy(
        update={"holdout_plan_hash": _hash("search-phase-plan")}
    )
    member = _member(round_authority, RoundPhase.HOLDOUT)
    request = _request(round_authority, member, RoundPhase.HOLDOUT)
    harness = RecordingHarness(_result(tmp_path, request))
    budget = RecordingBudgetAuthority()

    with pytest.raises(MeasurementSafetyError, match="distinct revealed Plan"):
        _runner(harness, budget).run(
            round_authority=round_authority,
            member=member,
            request=request,
            output_dir=tmp_path / "plan-reuse",
        )

    assert budget.reserve_requests == []
    assert harness.payloads == []


def test_scripted_telemetry_retry_count_is_finite(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, member, RoundPhase.SEARCH)
    result = _result(tmp_path, request)
    payload = result.model_dump(mode="python")
    payload["telemetry_attempt_count"] = 4

    with pytest.raises(ValidationError):
        M2ScriptedHarnessResult.model_validate(payload)
