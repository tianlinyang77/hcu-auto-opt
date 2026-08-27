# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.contracts.m2 import (
    BudgetUsage,
    RoundBudgetFinalizeRequest,
    RoundBudgetLedgerEntry,
    RoundBudgetMutationResult,
    RoundBudgetReservation,
    RoundBudgetReserveRequest,
    RoundCandidate,
    SearchRound,
)
from hcuopt.domain.enums import (
    RoundBudgetEntryType,
    RoundBudgetReservationState,
    RoundCandidateState,
    RoundPhase,
    SearchRoundRunMode,
    SearchRoundState,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.measurement.m2_models import (
    M2PhaseBudgetReservationPlan,
    M2PhaseMeasurementRequest,
    M2PhaseUsageEvidence,
    M2ScriptedHarnessResult,
    M2ScriptedPhaseEvidence,
    RoundMeasurementRef,
    utcnow,
)
from hcuopt.source_hash import file_uri_to_path


@runtime_checkable
class M2RoundBudgetAuthority(Protocol):
    def reserve(
        self, request: RoundBudgetReserveRequest
    ) -> RoundBudgetMutationResult: ...

    def finalize(
        self, request: RoundBudgetFinalizeRequest
    ) -> RoundBudgetMutationResult: ...


@runtime_checkable
class M2UniqueMeasurementHarness(Protocol):
    """The only Harness entrypoint; Scripted implementations must stay synthetic."""

    def run_manual_performance(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> M2ScriptedHarnessResult: ...


class M2PhaseHarnessFailure(MeasurementSafetyError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        actual_sample_count: int = 0,
        raw_evidence_uri: str | None = None,
        raw_evidence_hash: str | None = None,
        cleanup_evidence: Mapping[str, Any] | None = None,
        telemetry_warnings: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.actual_sample_count = actual_sample_count
        self.raw_evidence_uri = raw_evidence_uri
        self.raw_evidence_hash = raw_evidence_hash
        self.cleanup_evidence = (
            dict(cleanup_evidence) if cleanup_evidence is not None else None
        )
        self.telemetry_warnings = telemetry_warnings


class M2PhaseIsolationRegistry:
    """Scripted guard; A's durable Repository will enforce the same uniqueness later."""

    _UNIQUE_FIELDS = (
        "measurement_id",
        "raw_evidence_uri",
        "raw_evidence_hash",
        "baseline_sample_set_hash",
        "process_identity_set_hash",
        "cache_namespace_set_hash",
    )

    def __init__(self) -> None:
        self._seen: dict[str, set[object]] = {
            field: set() for field in self._UNIQUE_FIELDS
        }
        self._phase_plans: dict[tuple[UUID, RoundPhase], str] = {}

    def validate(self, reference: RoundMeasurementRef) -> None:
        for field in self._UNIQUE_FIELDS:
            if getattr(reference, field) in self._seen[field]:
                raise MeasurementSafetyError(
                    f"M2 phase isolation rejected reused {field}"
                )
        other_phase = (
            RoundPhase.HOLDOUT
            if reference.phase is RoundPhase.SEARCH
            else RoundPhase.SEARCH
        )
        if self._phase_plans.get((reference.round_id, other_phase)) == (
            reference.phase_plan_hash
        ):
            raise MeasurementSafetyError("Search and Holdout cannot reuse one Phase Plan")

    def commit(self, reference: RoundMeasurementRef) -> None:
        self.validate(reference)
        for field in self._UNIQUE_FIELDS:
            self._seen[field].add(getattr(reference, field))
        self._phase_plans[(reference.round_id, reference.phase)] = (
            reference.phase_plan_hash
        )


@dataclass(frozen=True, slots=True)
class M2PhaseMeasurementOutcome:
    reference: RoundMeasurementRef
    reservation: RoundBudgetMutationResult
    settlement: RoundBudgetMutationResult
    usage_evidence_uri: str
    usage_evidence_hash: str


class M2PhaseMeasurementRunner:
    """B-line Phase wrapper around the unique Harness; it never emits a verdict."""

    def __init__(
        self,
        *,
        harness: M2UniqueMeasurementHarness,
        budget_authority: M2RoundBudgetAuthority,
        isolation_registry: M2PhaseIsolationRegistry | None = None,
        fence_is_live: Callable[[UUID, str, int], bool] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
    ) -> None:
        self.harness = harness
        self.budget_authority = budget_authority
        self.isolation_registry = isolation_registry or M2PhaseIsolationRegistry()
        self.fence_is_live = fence_is_live or (lambda _lease, _resource, _token: True)
        self.monotonic_ns = monotonic_ns or time.monotonic_ns

    def run(
        self,
        *,
        round_authority: SearchRound,
        member: RoundCandidate,
        request: M2PhaseMeasurementRequest,
        output_dir: Path,
    ) -> M2PhaseMeasurementOutcome:
        self._validate_authority(round_authority, member, request)
        context = request.execution
        if not self.fence_is_live(
            context.lease_id, context.resource_id, context.fencing_token
        ):
            raise MeasurementSafetyError("M2 phase execution rejected a stale Fence")

        reservation_evidence_uri, reservation_evidence_hash = self._write_payload(
            output_dir / "budget" / "reserve",
            request.reservation.reservation_id,
            {
                "schema_version": "m2a-phase-budget-reservation-v1",
                "reservation": request.reservation.model_dump(mode="json"),
                "synthetic": True,
            },
        )
        del reservation_evidence_uri
        reserve_request = self._reserve_request(
            request.reservation, reservation_evidence_hash
        )
        reserved = self.budget_authority.reserve(reserve_request)

        harness_started_ns = self.monotonic_ns()
        result: M2ScriptedHarnessResult | None = None
        failure: BaseException | None = None
        try:
            payload = self._harness_payload(round_authority, member, request)
            result = M2ScriptedHarnessResult.model_validate(
                self.harness.run_manual_performance(payload, output_dir)
            )
        except BaseException as exc:
            failure = exc
        harness_finished_ns = self.monotonic_ns()
        finished_ns = self.monotonic_ns()

        if failure is None and not self.fence_is_live(
            context.lease_id, context.resource_id, context.fencing_token
        ):
            failure = MeasurementSafetyError(
                "M2 phase Fence became stale before evidence finalization"
            )

        try:
            if failure is None:
                assert result is not None
                self._validate_harness_result(round_authority, member, request, result)
        except BaseException as exc:
            failure = exc

        reference: RoundMeasurementRef | None = None
        try:
            if failure is None:
                assert result is not None
                reference = self._reference(round_authority, member, request, result)
                self.isolation_registry.validate(reference)
        except BaseException as exc:
            failure = exc

        actual_samples = self._actual_sample_count(result, failure)
        harness_seconds = self._seconds(harness_finished_ns - harness_started_ns)
        lease_seconds = self._seconds(
            finished_ns - context.lease_acquired_monotonic_ns
        )
        wall_seconds = self._seconds(
            finished_ns - context.job_started_monotonic_ns
        )
        actual = self._actual_usage(
            request.reservation.phase,
            actual_samples,
            wall_seconds,
            lease_seconds,
        )
        usage = self._usage_evidence(
            request,
            result,
            failure,
            actual,
            lease_seconds,
            harness_seconds,
        )
        usage_uri, usage_hash = self._write_payload(
            output_dir / "budget" / "usage",
            request.reservation.reservation_id,
            usage.model_dump(mode="json"),
        )
        settlement = self.budget_authority.finalize(
            RoundBudgetFinalizeRequest(
                ledger_entry=self._terminal_ledger(
                    request.reservation,
                    entry_type=RoundBudgetEntryType.SETTLE,
                    actual=actual,
                    lease_held_seconds=lease_seconds,
                    harness_active_seconds=harness_seconds,
                    raw_usage_evidence_hash=usage_hash,
                )
            )
        )

        if failure is not None:
            raise failure
        assert reference is not None
        self.isolation_registry.commit(reference)
        return M2PhaseMeasurementOutcome(
            reference=reference,
            reservation=reserved,
            settlement=settlement,
            usage_evidence_uri=usage_uri,
            usage_evidence_hash=usage_hash,
        )

    def release_unstarted(
        self,
        *,
        reservation: M2PhaseBudgetReservationPlan,
        output_dir: Path,
        reason: str,
    ) -> tuple[RoundBudgetMutationResult, RoundBudgetMutationResult]:
        if not reason or len(reason) > 200:
            raise ValueError("M2 unstarted release requires a bounded reason")
        _, reserve_hash = self._write_payload(
            output_dir / "budget" / "reserve",
            reservation.reservation_id,
            {
                "schema_version": "m2a-phase-budget-reservation-v1",
                "reservation": reservation.model_dump(mode="json"),
                "synthetic": True,
            },
        )
        reserved = self.budget_authority.reserve(
            self._reserve_request(reservation, reserve_hash)
        )
        usage = M2PhaseUsageEvidence(
            round_id=reservation.round_id,
            candidate_id=reservation.candidate_id,
            phase=reservation.phase,
            reservation_id=reservation.reservation_id,
            status="released",
            planned=reservation.planned,
            actual=BudgetUsage(),
            lease_held_seconds=0,
            harness_active_seconds=0,
            error_code=reason,
        )
        _, usage_hash = self._write_payload(
            output_dir / "budget" / "usage",
            reservation.reservation_id,
            usage.model_dump(mode="json"),
        )
        released = self.budget_authority.finalize(
            RoundBudgetFinalizeRequest(
                ledger_entry=self._terminal_ledger(
                    reservation,
                    entry_type=RoundBudgetEntryType.RELEASE,
                    actual=BudgetUsage(),
                    lease_held_seconds=0,
                    harness_active_seconds=0,
                    raw_usage_evidence_hash=usage_hash,
                )
            )
        )
        return reserved, released

    @staticmethod
    def _validate_authority(
        round_authority: SearchRound,
        member: RoundCandidate,
        request: M2PhaseMeasurementRequest,
    ) -> None:
        plan = request.reservation
        phase = plan.phase
        expected_round_state = (
            SearchRoundState.SEARCH_MEASURING
            if phase is RoundPhase.SEARCH
            else SearchRoundState.HOLDOUT_MEASURING
        )
        expected_candidate_state = (
            RoundCandidateState.CORRECTNESS_PASSED
            if phase is RoundPhase.SEARCH
            else RoundCandidateState.SEARCH_MEASURED
        )
        if (
            round_authority.run_mode is not SearchRoundRunMode.SCRIPTED
            or round_authority.state is not expected_round_state
            or round_authority.candidate_family_hash is None
            or round_authority.artifact_family_hash is None
        ):
            raise MeasurementSafetyError(
                "M2 phase measurement requires a frozen Scripted Round in the expected state"
            )
        if (
            member.round_id != round_authority.round_id
            or member.round_candidate_id != plan.round_candidate_id
            or member.candidate_id != plan.candidate_id
            or member.state is not expected_candidate_state
            or member.artifact_id is None
            or member.artifact_hash is None
        ):
            raise MeasurementSafetyError(
                "M2 phase measurement member is not the expected frozen Artifact terminal"
            )
        if plan.round_id != round_authority.round_id:
            raise MeasurementSafetyError("M2 phase plan belongs to another Round")
        if phase is RoundPhase.SEARCH:
            if (
                plan.phase_plan_hash != round_authority.search_plan_hash
                or round_authority.holdout_plan_hash is not None
                or round_authority.holdout_family_hash is not None
            ):
                raise MeasurementSafetyError(
                    "Search measurement requires the frozen Search Plan before Holdout reveal"
                )
        elif (
            round_authority.holdout_plan_hash is None
            or round_authority.holdout_family_hash is None
            or round_authority.holdout_reveal_evidence_hash is None
            or plan.phase_plan_hash != round_authority.holdout_plan_hash
            or request.holdout_reveal_evidence_hash
            != round_authority.holdout_reveal_evidence_hash
            or round_authority.holdout_plan_hash == round_authority.search_plan_hash
        ):
            raise MeasurementSafetyError(
                "Holdout measurement requires a distinct revealed Plan and frozen Family"
            )

    @staticmethod
    def _harness_payload(
        round_authority: SearchRound,
        member: RoundCandidate,
        request: M2PhaseMeasurementRequest,
    ) -> dict[str, Any]:
        protected = {
            "round_id": str(round_authority.round_id),
            "round_candidate_id": str(member.round_candidate_id),
            "candidate_id": str(member.candidate_id),
            "phase": request.reservation.phase.value,
            "phase_plan_hash": request.reservation.phase_plan_hash,
            "measurement_plan_hash": request.reservation.measurement_plan_hash,
            "candidate_family_hash": round_authority.candidate_family_hash,
            "artifact_family_hash": round_authority.artifact_family_hash,
            "holdout_family_hash": round_authority.holdout_family_hash,
            "artifact_id": str(member.artifact_id),
            "artifact_hash": member.artifact_hash,
            "lease_id": str(request.execution.lease_id),
            "resource_id": request.execution.resource_id,
            "fencing_token": request.execution.fencing_token,
            "expected_sample_count": request.reservation.expected_sample_count,
            "adapter_profile": round_authority.adapter_profile,
            "synthetic": True,
            "producer_verdict": None,
        }
        payload = dict(request.harness_payload)
        for name, value in protected.items():
            if name in payload and payload[name] != value:
                raise MeasurementSafetyError(
                    f"M2 Harness payload attempted to override frozen {name}"
                )
            payload[name] = value
        return payload

    @staticmethod
    def _validate_harness_result(
        round_authority: SearchRound,
        member: RoundCandidate,
        request: M2PhaseMeasurementRequest,
        result: M2ScriptedHarnessResult,
    ) -> None:
        cleanup = result.cleanup_evidence
        fence = cleanup.get("fence")
        health = cleanup.get("health")
        if (
            result.adapter_provenance.profile != round_authority.adapter_profile
            or result.measurement_plan_hash != request.reservation.measurement_plan_hash
            or result.sample_count != request.reservation.expected_sample_count
            or not isinstance(fence, Mapping)
            or not isinstance(health, Mapping)
            or fence.get("fenced") is not True
            or fence.get("resource_id") != request.execution.resource_id
            or fence.get("fencing_token") != request.execution.fencing_token
            or health.get("healthy") is not True
            or health.get("resource_id") != request.execution.resource_id
            or member.artifact_id is None
            or member.artifact_hash is None
        ):
            raise MeasurementSafetyError(
                "M2 Harness result is partial, mismatched, or lacks healthy cleanup"
            )
        raw_path = file_uri_to_path(result.raw_evidence_uri)
        if raw_path.is_symlink() or not raw_path.is_file():
            raise MeasurementSafetyError(
                "M2 Harness raw evidence must be one regular immutable file"
            )
        path = raw_path.resolve(strict=True)
        encoded = path.read_bytes()
        if "sha256:" + hashlib.sha256(encoded).hexdigest() != result.raw_evidence_hash:
            raise MeasurementSafetyError("M2 Harness raw evidence Hash does not match")
        evidence = M2ScriptedPhaseEvidence.model_validate_json(encoded)
        expected = (
            round_authority.round_id,
            member.round_candidate_id,
            member.candidate_id,
            request.reservation.phase,
            request.reservation.phase_plan_hash,
            request.reservation.measurement_plan_hash,
            result.measurement_id,
            result.sample_count,
            result.baseline_sample_set_hash,
            result.process_identity_set_hash,
            result.cache_namespace_set_hash,
            result.cleanup_evidence,
            result.telemetry_attempt_count,
            result.telemetry_warnings,
            result.adapter_provenance.model_dump(mode="python"),
            None,
            "not_available",
            True,
        )
        actual = (
            evidence.round_id,
            evidence.round_candidate_id,
            evidence.candidate_id,
            evidence.phase,
            evidence.phase_plan_hash,
            evidence.measurement_plan_hash,
            evidence.measurement_id,
            evidence.sample_count,
            evidence.baseline_sample_set_hash,
            evidence.process_identity_set_hash,
            evidence.cache_namespace_set_hash,
            evidence.cleanup_evidence,
            evidence.telemetry_attempt_count,
            evidence.telemetry_warnings,
            evidence.adapter_provenance.model_dump(mode="python"),
            evidence.producer_verdict,
            evidence.performance_conclusion,
            evidence.synthetic,
        )
        if actual != expected:
            raise MeasurementSafetyError(
                "M2 Harness raw evidence does not match its frozen Phase binding"
            )

    @staticmethod
    def _reference(
        round_authority: SearchRound,
        member: RoundCandidate,
        request: M2PhaseMeasurementRequest,
        result: M2ScriptedHarnessResult,
    ) -> RoundMeasurementRef:
        assert round_authority.candidate_family_hash is not None
        assert round_authority.artifact_family_hash is not None
        assert member.artifact_id is not None
        assert member.artifact_hash is not None
        return RoundMeasurementRef(
            round_measurement_ref_id=uuid5(
                NAMESPACE_URL,
                "hcuopt:m2-round-measurement:"
                f"{round_authority.round_id}:{member.candidate_id}:"
                f"{request.reservation.phase.value}:{result.measurement_id}",
            ),
            round_id=round_authority.round_id,
            round_candidate_id=member.round_candidate_id,
            candidate_id=member.candidate_id,
            phase=request.reservation.phase,
            candidate_family_hash=round_authority.candidate_family_hash,
            artifact_family_hash=round_authority.artifact_family_hash,
            holdout_family_hash=(
                round_authority.holdout_family_hash
                if request.reservation.phase is RoundPhase.HOLDOUT
                else None
            ),
            artifact_id=member.artifact_id,
            artifact_hash=member.artifact_hash,
            measurement_id=result.measurement_id,
            evidence_schema_version=result.evidence_schema_version,
            raw_evidence_uri=result.raw_evidence_uri,
            raw_evidence_hash=result.raw_evidence_hash,
            measurement_plan_hash=result.measurement_plan_hash,
            phase_plan_hash=request.reservation.phase_plan_hash,
            holdout_reveal_evidence_hash=request.holdout_reveal_evidence_hash,
            baseline_sample_set_hash=result.baseline_sample_set_hash,
            process_identity_set_hash=result.process_identity_set_hash,
            cache_namespace_set_hash=result.cache_namespace_set_hash,
            lease_id=request.execution.lease_id,
            resource_id=request.execution.resource_id,
            fencing_token=request.execution.fencing_token,
        )

    @staticmethod
    def _reserve_request(
        plan: M2PhaseBudgetReservationPlan,
        evidence_hash: str,
    ) -> RoundBudgetReserveRequest:
        reservation = RoundBudgetReservation(
            reservation_id=plan.reservation_id,
            round_id=plan.round_id,
            job_id=plan.job_id,
            attempt=plan.attempt,
            candidate_id=plan.candidate_id,
            phase=plan.phase,
            planned=plan.planned,
            state=RoundBudgetReservationState.RESERVED,
            idempotency_key=f"{plan.idempotency_key}:reservation",
        )
        entry = RoundBudgetLedgerEntry(
            ledger_entry_id=uuid5(
                NAMESPACE_URL, f"hcuopt:m2-budget:{plan.reservation_id}:reserve"
            ),
            reservation_id=plan.reservation_id,
            round_id=plan.round_id,
            entry_type=RoundBudgetEntryType.RESERVE,
            reserved=plan.planned,
            actual=BudgetUsage(),
            lease_held_seconds=0,
            harness_active_seconds=0,
            raw_usage_evidence_hash=evidence_hash,
            idempotency_key=f"{plan.idempotency_key}:reserve",
            created_at=utcnow(),
        )
        return RoundBudgetReserveRequest(reservation=reservation, ledger_entry=entry)

    @staticmethod
    def _terminal_ledger(
        plan: M2PhaseBudgetReservationPlan,
        *,
        entry_type: RoundBudgetEntryType,
        actual: BudgetUsage,
        lease_held_seconds: float,
        harness_active_seconds: float,
        raw_usage_evidence_hash: str,
    ) -> RoundBudgetLedgerEntry:
        return RoundBudgetLedgerEntry(
            ledger_entry_id=uuid5(
                NAMESPACE_URL,
                f"hcuopt:m2-budget:{plan.reservation_id}:{entry_type.value}",
            ),
            reservation_id=plan.reservation_id,
            round_id=plan.round_id,
            entry_type=entry_type,
            reserved=plan.planned,
            actual=actual,
            lease_held_seconds=lease_held_seconds,
            harness_active_seconds=harness_active_seconds,
            raw_usage_evidence_hash=raw_usage_evidence_hash,
            idempotency_key=f"{plan.idempotency_key}:{entry_type.value}",
            created_at=utcnow(),
        )

    @staticmethod
    def _actual_sample_count(
        result: M2ScriptedHarnessResult | None,
        failure: BaseException | None,
    ) -> int:
        if result is not None:
            return result.sample_count
        if isinstance(failure, M2PhaseHarnessFailure):
            return failure.actual_sample_count
        return 0

    @staticmethod
    def _actual_usage(
        phase: RoundPhase,
        samples: int,
        wall_seconds: float,
        lease_seconds: float,
    ) -> BudgetUsage:
        return BudgetUsage(
            search_samples=samples if phase is RoundPhase.SEARCH else 0,
            holdout_samples=samples if phase is RoundPhase.HOLDOUT else 0,
            wall_seconds=wall_seconds,
            exclusive_lease_seconds=lease_seconds,
        )

    @staticmethod
    def _usage_evidence(
        request: M2PhaseMeasurementRequest,
        result: M2ScriptedHarnessResult | None,
        failure: BaseException | None,
        actual: BudgetUsage,
        lease_seconds: float,
        harness_seconds: float,
    ) -> M2PhaseUsageEvidence:
        error_code: str | None = None
        cleanup: Mapping[str, Any] | None = None
        warnings: tuple[str, ...] = ()
        raw_uri: str | None = None
        raw_hash: str | None = None
        measurement_id: UUID | None = None
        if result is not None:
            cleanup = result.cleanup_evidence
            warnings = result.telemetry_warnings
            raw_uri = result.raw_evidence_uri
            raw_hash = result.raw_evidence_hash
            measurement_id = result.measurement_id
        if isinstance(failure, M2PhaseHarnessFailure):
            error_code = failure.error_code
            cleanup = failure.cleanup_evidence
            warnings = failure.telemetry_warnings
            raw_uri = failure.raw_evidence_uri
            raw_hash = failure.raw_evidence_hash
        elif failure is not None:
            error_code = type(failure).__name__
        return M2PhaseUsageEvidence(
            round_id=request.reservation.round_id,
            candidate_id=request.reservation.candidate_id,
            phase=request.reservation.phase,
            reservation_id=request.reservation.reservation_id,
            status="failed" if failure is not None else "measured",
            planned=request.reservation.planned,
            actual=actual,
            lease_held_seconds=lease_seconds,
            harness_active_seconds=harness_seconds,
            measurement_id=measurement_id,
            raw_evidence_uri=raw_uri,
            raw_evidence_hash=raw_hash,
            cleanup_evidence=dict(cleanup) if cleanup is not None else None,
            telemetry_warnings=warnings,
            error_code=error_code,
        )

    @staticmethod
    def _seconds(delta_ns: int) -> float:
        if delta_ns < 0:
            raise MeasurementSafetyError("M2 phase monotonic timestamps are not ordered")
        return delta_ns / 1_000_000_000

    @staticmethod
    def _write_payload(
        root: Path,
        identity: UUID,
        payload: Mapping[str, Any],
    ) -> tuple[str, str]:
        encoded = canonical_json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        evidence_hash = f"sha256:{digest}"
        destination = root.resolve() / identity.hex / f"{digest}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != encoded:
                raise MeasurementSafetyError("M2 usage Evidence path has conflicting bytes")
        else:
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".m2-usage-", dir=destination.parent, delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(encoded)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                try:
                    os.link(temporary_path, destination)
                except FileExistsError:
                    if destination.read_bytes() != encoded:
                        raise MeasurementSafetyError(
                            "M2 usage Evidence Store contains conflicting content"
                        ) from None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        destination.chmod(0o444)
        return destination.as_uri(), evidence_hash
