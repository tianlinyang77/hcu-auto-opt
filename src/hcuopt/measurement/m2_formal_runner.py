# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
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
from hcuopt.contracts.m2_formal_authority_v1 import FormalAuthorityContextDescriptor
from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalExecutionAdapterProfile,
    M2FormalExecutionBinding,
    M2FormalExecutionStatus,
    M2FormalPhaseExecutionReceiptRef,
    M2FormalPhaseExecutionRecord,
    M2FormalPhaseExecutionRequest,
    M2FormalTargetLockRefreshReport,
    m2_formal_execution_id_for,
    m2_formal_phase_execution_request_hash,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.contracts.v1 import ManualPerformanceEvidenceResult
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
from hcuopt.measurement.m2_models import M2PhaseBudgetReservationPlan, utcnow
from hcuopt.source_hash import file_uri_to_path

from .m2_formal_receipt import M2FormalPhaseExecutionReceiptStore
from .m2_runner import M2RoundBudgetAuthority


@runtime_checkable
class M2FormalUniqueMeasurementHarness(Protocol):
    """Existing unique Harness entrypoint; the Formal adapter does not sample."""

    def run_manual_performance(
        self, payload: Mapping[str, Any], output_dir: Path
    ) -> ManualPerformanceEvidenceResult: ...


@runtime_checkable
class M2FormalTargetLockRefreshProbe(Protocol):
    """Deployment probe that re-reads the locked target immediately before reserve."""

    def refresh(
        self,
        binding: M2FormalExecutionBinding,
        observed_at: datetime,
    ) -> M2FormalTargetLockRefreshReport: ...


class M2FormalHarnessFailure(MeasurementSafetyError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        actual_sample_count: int = 0,
        cleanup_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.actual_sample_count = actual_sample_count
        self.cleanup_evidence = dict(cleanup_evidence) if cleanup_evidence is not None else None


class M2FormalExecutionFailure(MeasurementSafetyError):
    def __init__(
        self,
        message: str,
        *,
        receipt_ref: M2FormalPhaseExecutionReceiptRef,
    ) -> None:
        super().__init__(message)
        self.receipt_ref = receipt_ref


class M2FormalPhaseIsolationRegistry:
    _UNIQUE_FIELDS = ("measurement_id", "raw_evidence_uri", "raw_evidence_hash")

    def __init__(self) -> None:
        self._seen: dict[str, set[object]] = {field: set() for field in self._UNIQUE_FIELDS}
        self._phase_plans: dict[tuple[UUID, RoundPhase], tuple[str, str]] = {}

    def validate(self, record: M2FormalPhaseExecutionRecord) -> None:
        if record.status != "succeeded":
            return
        for field in self._UNIQUE_FIELDS:
            if getattr(record, field) in self._seen[field]:
                raise MeasurementSafetyError(f"Formal phase isolation rejected reused {field}")
        other = (
            RoundPhase.HOLDOUT if record.binding.phase is RoundPhase.SEARCH else RoundPhase.SEARCH
        )
        other_plans = self._phase_plans.get((record.binding.round_id, other))
        if other_plans is not None and (
            record.binding.phase_plan_hash in other_plans
            or record.binding.measurement_plan_hash in other_plans
        ):
            raise MeasurementSafetyError("Search and Holdout require distinct execution plans")

    def commit(self, record: M2FormalPhaseExecutionRecord) -> None:
        self.validate(record)
        if record.status != "succeeded":
            return
        for field in self._UNIQUE_FIELDS:
            self._seen[field].add(getattr(record, field))
        self._phase_plans[(record.binding.round_id, record.binding.phase)] = (
            record.binding.phase_plan_hash,
            record.binding.measurement_plan_hash,
        )


@dataclass(frozen=True, slots=True)
class M2FormalPhaseExecutionOutcome:
    receipt_ref: M2FormalPhaseExecutionReceiptRef
    reservation: RoundBudgetMutationResult
    settlement: RoundBudgetMutationResult
    usage_evidence_uri: str
    usage_evidence_hash: str


class M2FormalPhaseExecutionAdapter:
    """Formal B-line lifecycle wrapper; sampling and verdicts remain external."""

    def __init__(
        self,
        *,
        harness: M2FormalUniqueMeasurementHarness,
        adapter_profile: M2FormalExecutionAdapterProfile,
        target_lock_probe: M2FormalTargetLockRefreshProbe,
        budget_authority: M2RoundBudgetAuthority,
        receipt_store: M2FormalPhaseExecutionReceiptStore,
        lease_is_live: Callable[[M2FormalExecutionBinding, datetime], bool],
        target_lock_is_live: Callable[[M2FormalExecutionBinding, datetime], bool],
        recover_resource: Callable[[M2FormalExecutionBinding], Mapping[str, Any]],
        recover_expired_lease: (
            Callable[[M2FormalExecutionBinding], Mapping[str, Any]] | None
        ) = None,
        isolation_registry: M2FormalPhaseIsolationRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
        provenance: AdapterProvenance | None = None,
    ) -> None:
        self.harness = harness
        self.adapter_profile = adapter_profile
        self.target_lock_probe = target_lock_probe
        self.budget_authority = budget_authority
        self.receipt_store = receipt_store
        self.lease_is_live = lease_is_live
        self.target_lock_is_live = target_lock_is_live
        self.recover_resource = recover_resource
        self.recover_expired_lease = recover_expired_lease or recover_resource
        self.isolation_registry = isolation_registry or M2FormalPhaseIsolationRegistry()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_ns = monotonic_ns or time.monotonic_ns
        self.provenance = provenance or AdapterProvenance(
            profile=adapter_profile.profile_id,
            capability="formal_execution_adapter",
            adapter_name="M2FormalPhaseExecutionAdapter",
            adapter_version=adapter_profile.profile_version,
            implementation_kind="real",
        )

    def run(
        self,
        *,
        round_authority: SearchRound,
        formal_authority: FormalAuthorityContextDescriptor,
        member: RoundCandidate,
        request: M2FormalPhaseExecutionRequest,
        output_dir: Path,
    ) -> M2FormalPhaseExecutionOutcome:
        now = self.clock()
        request_hash = m2_formal_phase_execution_request_hash(request)
        if (
            self.adapter_profile.profile_id != request.binding.adapter_profile
            or self.adapter_profile.profile_version
            != request.binding.adapter_profile_version
            or self.adapter_profile.profile_hash != request.binding.adapter_profile_hash
            or self.provenance.profile != self.adapter_profile.profile_id
            or self.provenance.adapter_version != self.adapter_profile.profile_version
        ):
            raise MeasurementSafetyError(
                "Formal execution Adapter Profile identity differs from its binding"
            )
        self._validate_authority(round_authority, formal_authority, member, request)
        target_lock_refresh = self._validate_live_binding(request, now)

        _, reserve_hash = self._write_payload(
            output_dir / "budget" / "reserve",
            request.reservation.reservation_id,
            {
                "schema_version": "m2a-formal-phase-budget-reservation-v1",
                "reservation": request.reservation.model_dump(mode="json"),
                "binding": request.binding.model_dump(mode="json"),
                "synthetic": False,
                "performance_conclusion": "not_measured",
            },
        )
        reserved = self.budget_authority.reserve(
            self._reserve_request(request.reservation, reserve_hash)
        )

        started_at = self.clock()
        started_ns = self.monotonic_ns()
        result: ManualPerformanceEvidenceResult | None = None
        failure: Exception | None = None
        cleanup_evidence: dict[str, Any] | None = None
        actual_sample_count = 0
        try:
            result = ManualPerformanceEvidenceResult.model_validate(
                self.harness.run_manual_performance(
                    self._harness_payload(
                        round_authority,
                        member,
                        request,
                        target_lock_refresh,
                        request_hash,
                    ),
                    output_dir,
                )
            )
            cleanup_evidence = dict(result.cleanup_evidence)
            actual_sample_count = result.measurement.sample_count
            self._validate_harness_result(round_authority, member, request, result)
        except Exception as exc:
            failure = exc
            if isinstance(exc, M2FormalHarnessFailure):
                cleanup_evidence = exc.cleanup_evidence
                actual_sample_count = exc.actual_sample_count
            if cleanup_evidence is None:
                try:
                    cleanup_evidence = dict(self.recover_resource(request.binding))
                except Exception as cleanup_error:
                    cleanup_evidence = {
                        "fence": {
                            "fenced": False,
                            "resource_id": request.binding.resource_id,
                            "fencing_token": request.binding.fencing_token,
                            "failures": [type(cleanup_error).__name__],
                            "clocks_restored": False,
                        },
                        "health": {
                            "healthy": False,
                            "resource_id": request.binding.resource_id,
                            "residual_owned_containers": ["cleanup_unverified"],
                            "memory_release_check": "unverified",
                            "clocks_restored": False,
                        },
                    }
        finished_ns = self.monotonic_ns()
        finished_at = self.clock()

        if failure is None:
            if finished_at >= request.binding.window.expires_at:
                failure = TimeoutError("Formal execution exceeded its approved window")
            elif finished_at >= request.binding.lease_expires_at or not self.lease_is_live(
                request.binding, finished_at
            ):
                failure = MeasurementSafetyError(
                    "Formal execution Fence became stale before finalization"
                )

        elapsed = max(0.0, (finished_ns - started_ns) / 1_000_000_000)
        actual = self._actual_usage(
            request.binding.phase,
            actual_sample_count,
            elapsed,
        )
        cleanup_healthy = self._cleanup_is_healthy(cleanup_evidence, request.binding)
        status, error_code = self._terminal_status(
            failure,
            cleanup_evidence,
            request.binding,
        )
        measurement = result.measurement if result is not None and failure is None else None
        record = M2FormalPhaseExecutionRecord(
            execution_id=m2_formal_execution_id_for(request.binding),
            request_hash=request_hash,
            binding=request.binding,
            target_lock_refresh=target_lock_refresh,
            adapter_provenance=self.provenance,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            actual=actual,
            lease_held_seconds=elapsed,
            harness_active_seconds=elapsed,
            measurement_id=measurement.measurement_id if measurement is not None else None,
            raw_evidence_uri=(measurement.raw_samples_uri if measurement is not None else None),
            raw_evidence_hash=(measurement.raw_samples_hash if measurement is not None else None),
            sample_count=actual_sample_count,
            cleanup_evidence=cleanup_evidence,
            cleanup_status="verified" if cleanup_healthy else "failed",
            termination_reason=(
                None if failure is None else f"{type(failure).__name__}: {str(failure)[:400]}"
            ),
            error_code=error_code,
        )
        try:
            self.isolation_registry.validate(record)
        except Exception as exc:
            failure = exc
            record = M2FormalPhaseExecutionRecord.model_validate(
                {
                    **record.model_dump(mode="python"),
                    "status": "evidence_invalid",
                    "measurement_id": None,
                    "raw_evidence_uri": None,
                    "raw_evidence_hash": None,
                    "error_code": "phase_identity_reused",
                    "termination_reason": f"{type(exc).__name__}: {str(exc)[:400]}",
                }
            )

        usage_uri, usage_hash = self._write_payload(
            output_dir / "budget" / "usage",
            request.reservation.reservation_id,
            {
                "schema_version": "m2a-formal-phase-budget-usage-v1",
                "round_id": str(request.binding.round_id),
                "candidate_id": str(request.binding.candidate_id),
                "phase": request.binding.phase.value,
                "reservation_id": str(request.reservation.reservation_id),
                "status": record.status,
                "planned": request.reservation.planned.model_dump(mode="json"),
                "actual": actual.model_dump(mode="json"),
                "lease_held_seconds": elapsed,
                "harness_active_seconds": elapsed,
                "error_code": record.error_code,
                "synthetic": False,
                "producer_verdict": None,
                "performance_conclusion": "not_measured",
            },
        )
        settled = self.budget_authority.finalize(
            RoundBudgetFinalizeRequest(
                ledger_entry=self._terminal_ledger(
                    request.reservation,
                    entry_type=RoundBudgetEntryType.SETTLE,
                    actual=actual,
                    lease_held_seconds=elapsed,
                    harness_active_seconds=elapsed,
                    raw_usage_evidence_hash=usage_hash,
                )
            )
        )
        receipt_ref = self.receipt_store.publish(record)
        if failure is not None:
            raise M2FormalExecutionFailure(str(failure), receipt_ref=receipt_ref) from failure
        self.isolation_registry.commit(record)
        return M2FormalPhaseExecutionOutcome(
            receipt_ref=receipt_ref,
            reservation=reserved,
            settlement=settled,
            usage_evidence_uri=usage_uri,
            usage_evidence_hash=usage_hash,
        )

    def release_unstarted(
        self,
        *,
        request: M2FormalPhaseExecutionRequest,
        output_dir: Path,
        reason: str,
    ) -> tuple[RoundBudgetMutationResult, RoundBudgetMutationResult]:
        if not reason or len(reason) > 200:
            raise ValueError("Formal unstarted release requires a bounded reason")
        self._validate_live_binding(request, self.clock())
        _, reserve_hash = self._write_payload(
            output_dir / "budget" / "reserve",
            request.reservation.reservation_id,
            {
                "schema_version": "m2a-formal-phase-budget-reservation-v1",
                "reservation": request.reservation.model_dump(mode="json"),
                "binding": request.binding.model_dump(mode="json"),
                "synthetic": False,
            },
        )
        reserved = self.budget_authority.reserve(
            self._reserve_request(request.reservation, reserve_hash)
        )
        _, usage_hash = self._write_payload(
            output_dir / "budget" / "usage",
            request.reservation.reservation_id,
            {
                "schema_version": "m2a-formal-phase-budget-usage-v1",
                "status": "released",
                "reason": reason,
                "actual": BudgetUsage().model_dump(mode="json"),
                "synthetic": False,
                "performance_conclusion": "not_measured",
            },
        )
        released = self.budget_authority.finalize(
            RoundBudgetFinalizeRequest(
                ledger_entry=self._terminal_ledger(
                    request.reservation,
                    entry_type=RoundBudgetEntryType.RELEASE,
                    actual=BudgetUsage(),
                    lease_held_seconds=0,
                    harness_active_seconds=0,
                    raw_usage_evidence_hash=usage_hash,
                )
            )
        )
        return reserved, released

    def _validate_live_binding(
        self,
        request: M2FormalPhaseExecutionRequest,
        now: datetime,
    ) -> M2FormalTargetLockRefreshReport:
        binding = request.binding
        if now.tzinfo is None or now.utcoffset() is None:
            raise MeasurementSafetyError("Formal execution clock must be timezone-aware")
        if now >= binding.lease_expires_at:
            try:
                recovery = dict(self.recover_expired_lease(binding))
            except Exception as error:
                raise MeasurementSafetyError(
                    "expired Formal Lease recovery failed closed"
                ) from error
            if not self._cleanup_is_healthy(recovery, binding):
                raise MeasurementSafetyError(
                    "expired Formal Lease did not recover its resource"
                )
            raise MeasurementSafetyError("Formal execution Lease expired and was recovered")
        if not (binding.window.starts_at <= now < binding.window.expires_at):
            raise MeasurementSafetyError("Formal execution is outside its approved window")
        if now >= binding.lease_renewal_due_at:
            raise MeasurementSafetyError("Formal execution Lease renewal is overdue")
        remaining_window = (binding.window.expires_at - now).total_seconds()
        remaining_lease = (binding.lease_expires_at - now).total_seconds()
        if (
            request.reservation.planned.wall_seconds > remaining_window
            or request.reservation.planned.exclusive_lease_seconds > remaining_lease
        ):
            raise MeasurementSafetyError(
                "Formal execution Budget does not fit its live window and Lease"
            )
        if not self.lease_is_live(binding, now):
            raise MeasurementSafetyError("Formal execution rejected a stale Fencing Token")
        if not self.target_lock_is_live(binding, now):
            raise MeasurementSafetyError("Formal execution Target Lock is not current")
        try:
            refresh = M2FormalTargetLockRefreshReport.model_validate(
                self.target_lock_probe.refresh(binding, now)
            )
        except Exception as error:
            raise MeasurementSafetyError(
                "Formal Target Lock refresh probe failed closed"
            ) from error
        if (
            refresh.formal_authorization_hash != binding.formal_authorization_hash
            or refresh.authority_context_hash != binding.authority_context_hash
            or refresh.target_snapshot_id != binding.target_snapshot_id
            or refresh.target_lock_hash != binding.target_lock_hash
            or refresh.host_id != binding.host_id
            or refresh.resource_id != binding.resource_id
            or refresh.device_index != binding.device_index
            or refresh.numa_node != binding.numa_node
            or refresh.cpu_affinity != binding.cpu_affinity
            or refresh.lease_id != binding.lease_id
            or refresh.fencing_token != binding.fencing_token
        ):
            raise MeasurementSafetyError(
                "Target Lock refresh and Formal execution bindings differ"
            )
        if (
            refresh.synthetic
            or refresh.status != "matched"
            or not (refresh.observed_at <= now < refresh.valid_until)
            or refresh.valid_until < binding.window.expires_at
        ):
            raise MeasurementSafetyError(
                "Formal execution requires a current real Target Lock refresh"
            )
        return refresh

    @staticmethod
    def _validate_authority(
        round_authority: SearchRound,
        formal_authority: FormalAuthorityContextDescriptor,
        member: RoundCandidate,
        request: M2FormalPhaseExecutionRequest,
    ) -> None:
        binding = request.binding
        phase = binding.phase
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
            round_authority.run_mode is not SearchRoundRunMode.FORMAL
            or round_authority.state is not expected_round_state
            or member.state is not expected_candidate_state
            or member.round_id != round_authority.round_id
            or member.round_candidate_id != binding.round_candidate_id
            or member.candidate_id != binding.candidate_id
            or member.artifact_id != binding.artifact_id
            or member.artifact_hash != binding.artifact_hash
        ):
            raise MeasurementSafetyError("Formal execution authority/member state differs")
        if (
            formal_authority.authority_context_id != binding.authority_context_id
            or formal_authority.context_hash != binding.authority_context_hash
            or formal_authority.round_id != binding.round_id
            or formal_authority.task_id != binding.task_id
            or formal_authority.target_snapshot_id != binding.target_snapshot_id
            or formal_authority.target_profile_hash != binding.target_profile_hash
            or formal_authority.stage0_protocol_hash != binding.stage0_protocol_hash
            or formal_authority.candidate_family_hash != round_authority.candidate_family_hash
            or formal_authority.artifact_family_hash != round_authority.artifact_family_hash
            or round_authority.adapter_profile != binding.adapter_profile
        ):
            raise MeasurementSafetyError("Formal Authority Context binding differs")
        if phase is RoundPhase.SEARCH:
            if binding.phase_plan_hash != formal_authority.search_plan_hash:
                raise MeasurementSafetyError("Formal Search Plan binding differs")
        elif (
            round_authority.holdout_plan_hash is None
            or round_authority.holdout_reveal_evidence_hash != binding.holdout_reveal_evidence_hash
            or binding.phase_plan_hash != round_authority.holdout_plan_hash
            or binding.phase_plan_hash == formal_authority.search_plan_hash
        ):
            raise MeasurementSafetyError("Formal Holdout requires a distinct revealed Plan")

    @staticmethod
    def _harness_payload(
        round_authority: SearchRound,
        member: RoundCandidate,
        request: M2FormalPhaseExecutionRequest,
        target_lock_refresh: M2FormalTargetLockRefreshReport,
        request_hash: str,
    ) -> dict[str, Any]:
        binding = request.binding
        protected = {
            "formal_execution_request_hash": request_hash,
            "formal_authorization_id": str(binding.formal_authorization_id),
            "formal_authorization_hash": binding.formal_authorization_hash,
            "resolved_plan_hash": binding.resolved_plan_hash,
            "authority_context_hash": binding.authority_context_hash,
            "round_id": str(binding.round_id),
            "candidate_id": str(binding.candidate_id),
            "artifact_id": str(binding.artifact_id),
            "artifact_hash": binding.artifact_hash,
            "phase": binding.phase.value,
            "phase_plan_hash": binding.phase_plan_hash,
            "measurement_plan_hash": binding.measurement_plan_hash,
            "target_snapshot_id": str(binding.target_snapshot_id),
            "target_lock_hash": binding.target_lock_hash,
            "target_lock_refresh_hash": target_lock_refresh.report_hash,
            "host_id": binding.host_id,
            "execution_host_hash": binding.execution_host_hash,
            "lease_id": str(binding.lease_id),
            "lease_scope": "exclusive",
            "resource_id": binding.resource_id,
            "device_index": binding.device_index,
            "numa_node": binding.numa_node,
            "cpu_affinity": binding.cpu_affinity,
            "topology_lease_hash": binding.topology_lease_hash,
            "fencing_token": binding.fencing_token,
            "lease_authority_hash": binding.lease_authority_hash,
            "lease_receipt_uri": binding.lease_receipt_uri,
            "lease_receipt_hash": binding.lease_receipt_hash,
            "lease_renewal_sequence": binding.lease_renewal_sequence,
            "adapter_profile": binding.adapter_profile,
            "adapter_profile_version": binding.adapter_profile_version,
            "adapter_profile_hash": binding.adapter_profile_hash,
            "stage0_protocol_hash": binding.stage0_protocol_hash,
            "expected_sample_count": request.reservation.expected_sample_count,
            "wall_budget_seconds": request.reservation.planned.wall_seconds,
            "mode": "formal",
            "synthetic": False,
            "producer_verdict": None,
            "performance_conclusion": "not_measured",
        }
        payload = dict(request.harness_payload)
        for name, value in protected.items():
            if name in payload and payload[name] != value:
                raise MeasurementSafetyError(
                    f"Formal Harness payload attempted to override frozen {name}"
                )
            payload[name] = value
        return payload

    @staticmethod
    def _validate_harness_result(
        round_authority: SearchRound,
        member: RoundCandidate,
        request: M2FormalPhaseExecutionRequest,
        result: ManualPerformanceEvidenceResult,
    ) -> None:
        measurement = result.measurement
        cleanup = result.cleanup_evidence
        fence = cleanup.get("fence")
        health = cleanup.get("health")
        if (
            result.candidate_id != member.candidate_id
            or measurement.adapter_provenance.profile != round_authority.adapter_profile
            or measurement.adapter_provenance.implementation_kind != "real"
            or measurement.sample_count != request.reservation.expected_sample_count
            or measurement.summary.get("plan_hash") != request.binding.measurement_plan_hash
            or not isinstance(fence, Mapping)
            or not isinstance(health, Mapping)
            or fence.get("fenced") is not True
            or fence.get("resource_id") != request.binding.resource_id
            or fence.get("fencing_token") != request.binding.fencing_token
            or health.get("healthy") is not True
            or health.get("resource_id") != request.binding.resource_id
            or not M2FormalPhaseExecutionAdapter._cleanup_is_healthy(
                cleanup,
                request.binding,
            )
        ):
            raise MeasurementSafetyError(
                "Formal Harness result is mismatched or lacks healthy cleanup"
            )
        assert measurement.raw_samples_uri is not None
        assert measurement.raw_samples_hash is not None
        raw_path = file_uri_to_path(measurement.raw_samples_uri)
        if raw_path.is_symlink() or not raw_path.is_file():
            raise MeasurementSafetyError("Formal raw evidence must be one regular file")
        encoded = raw_path.resolve(strict=True).read_bytes()
        actual_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if actual_hash != measurement.raw_samples_hash:
            raise MeasurementSafetyError("Formal raw evidence Hash does not match")

    @staticmethod
    def _terminal_status(
        failure: Exception | None,
        cleanup: Mapping[str, Any] | None,
        binding: M2FormalExecutionBinding,
    ) -> tuple[M2FormalExecutionStatus, str | None]:
        if failure is None:
            return "succeeded", None
        if not M2FormalPhaseExecutionAdapter._cleanup_is_healthy(cleanup, binding):
            return "cleanup_failed", "cleanup_failed"
        if isinstance(failure, TimeoutError):
            return "timed_out", "execution_timed_out"
        if isinstance(failure, M2FormalHarnessFailure):
            return "failed", failure.error_code
        if "Fence became stale" in str(failure):
            return "fence_lost", "fencing_token_lost"
        return "evidence_invalid", "formal_evidence_invalid"

    @staticmethod
    def _cleanup_is_healthy(
        cleanup: Mapping[str, Any] | None,
        binding: M2FormalExecutionBinding,
    ) -> bool:
        if cleanup is None:
            return False
        fence = cleanup.get("fence")
        health = cleanup.get("health")
        return (
            isinstance(fence, Mapping)
            and isinstance(health, Mapping)
            and fence.get("fenced") is True
            and health.get("healthy") is True
            and fence.get("resource_id") == binding.resource_id
            and fence.get("fencing_token") == binding.fencing_token
            and health.get("resource_id") == binding.resource_id
            and fence.get("failures") in ([], ())
            and fence.get("clocks_restored") is True
            and health.get("residual_owned_containers") in ([], ())
            and health.get("memory_release_check") == "no_adapter_owned_container_remains"
            and health.get("clocks_restored") is True
        )

    @staticmethod
    def _actual_usage(phase: RoundPhase, sample_count: int, elapsed: float) -> BudgetUsage:
        return BudgetUsage(
            search_samples=sample_count if phase is RoundPhase.SEARCH else 0,
            holdout_samples=sample_count if phase is RoundPhase.HOLDOUT else 0,
            wall_seconds=elapsed,
            exclusive_lease_seconds=elapsed,
        )

    @staticmethod
    def _reserve_request(
        plan: M2PhaseBudgetReservationPlan, evidence_hash: str
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
                NAMESPACE_URL, f"hcuopt:m2-formal-budget:{plan.reservation_id}:reserve"
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
                f"hcuopt:m2-formal-budget:{plan.reservation_id}:{entry_type.value}",
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
    def _write_payload(root: Path, identity: UUID, value: Mapping[str, Any]) -> tuple[str, str]:
        encoded = canonical_json_bytes(value)
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        path = root / f"{identity}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != encoded:
            raise MeasurementSafetyError("Formal evidence identity already has other bytes")
        if not path.exists():
            path.write_bytes(encoded)
        return path.resolve(strict=True).as_uri(), digest


__all__ = [
    "M2FormalExecutionFailure",
    "M2FormalHarnessFailure",
    "M2FormalPhaseExecutionAdapter",
    "M2FormalPhaseExecutionOutcome",
    "M2FormalPhaseIsolationRegistry",
    "M2FormalTargetLockRefreshProbe",
    "M2FormalUniqueMeasurementHarness",
]
