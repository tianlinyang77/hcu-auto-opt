# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from pydantic import ValidationError

from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileGrantVerifierRef,
    FormalProfileSetRef,
    FormalProfileWindowAuthorization,
    FormalProfileWindowAuthorizationContent,
    publish_formal_profile_window_authorization,
)
from hcuopt.contracts.m2 import (
    BudgetUsage,
    CandidateSourcePackageRef,
    RoundBudget,
    RoundBudgetMutationResult,
    RoundBudgetReservationView,
    RoundCandidate,
    SearchRound,
)
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextContent,
    FormalEvidenceStoreRef,
    FormalVerifierRef,
    publish_formal_authority_context,
)
from hcuopt.contracts.m2_formal_execution_v1 import (
    M2FormalExecutionAdapterProfileContent,
    M2FormalExecutionBinding,
    M2FormalExecutionWindow,
    M2FormalPhaseExecutionRequest,
    M2FormalTargetLockRefreshContent,
    m2_formal_phase_execution_request_hash,
    publish_m2_formal_execution_adapter_profile,
    publish_m2_formal_target_lock_refresh,
)
from hcuopt.contracts.m2_formal_operator_v1 import FormalResolvedRoundPlan
from hcuopt.contracts.operator_v1 import (
    OperatorProfileRef,
    ProfilerOperatorHotspotRef,
    ResolvedOperatorAuthority,
    ResolvedOperatorCandidate,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance, MeasurementSeries
from hcuopt.contracts.v1 import ManualPerformanceEvidenceResult
from hcuopt.domain.enums import (
    RoundBudgetEntryType,
    RoundBudgetReservationState,
    RoundCandidateState,
    RoundPhase,
    SearchRoundState,
)
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import write_evidence
from hcuopt.measurement.harness import MeasurementSafetyError
from hcuopt.measurement.m1_failure import M1FailureReport, M1MeasurementFailure
from hcuopt.measurement.m1_models import M1MeasurementBinding
from hcuopt.measurement.m2_formal_isolation import (
    SqliteM2FormalPhaseIsolationAuthority,
)
from hcuopt.measurement.m2_formal_receipt import M2FormalPhaseExecutionReceiptStore
from hcuopt.measurement.m2_formal_runner import (
    M2FormalExecutionFailure,
    M2FormalHarnessFailure,
    M2FormalPhaseExecutionAdapter,
    _FormalLeaseGuard,
)
from hcuopt.measurement.m2_models import M2PhaseBudgetReservationPlan
from hcuopt.operator.formal_plans import formal_operator_resolved_plan_hash
from tests.unit.test_m1_measurement import PortableReader

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
PROFILE = "m2a-formal-measurement-v1"
VERIFIER_REF = FormalProfileGrantVerifierRef(
    verifier_id="test-formal-verifier",
    verifier_version="1",
    verifier_hash="sha256:" + "1" * 64,
    signature_scheme="test-sha256-v1",
    key_id="test-key",
)
AUTHORIZATIONS: dict[object, FormalProfileWindowAuthorization] = {}
RESOLVED_PLANS: dict[str, FormalResolvedRoundPlan] = {}


class AuthorityReader:
    def load_authorization(self, authorization_id):  # type: ignore[no-untyped-def]
        return AUTHORIZATIONS[authorization_id]

    def load_resolved_plan(self, resolved_plan_hash):  # type: ignore[no-untyped-def]
        return RESOLVED_PLANS[resolved_plan_hash]


class AuthorizationVerifier:
    verifier_ref = VERIFIER_REF

    def verify_signature(self, *, authorization_hash, signature):  # type: ignore[no-untyped-def]
        return bool(authorization_hash and signature == "test-signature")


class StaticAuthorityReader:
    def __init__(self, authorization, plan):  # type: ignore[no-untyped-def]
        self.authorization = authorization
        self.plan = plan

    def load_authorization(self, authorization_id):  # type: ignore[no-untyped-def]
        del authorization_id
        return self.authorization

    def load_resolved_plan(self, resolved_plan_hash):  # type: ignore[no-untyped-def]
        del resolved_plan_hash
        return self.plan


class RejectingAuthorizationVerifier(AuthorizationVerifier):
    def verify_signature(self, *, authorization_hash, signature):  # type: ignore[no-untyped-def]
        del authorization_hash, signature
        return False


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _adapter_profile():  # type: ignore[no-untyped-def]
    return publish_m2_formal_execution_adapter_profile(
        M2FormalExecutionAdapterProfileContent(
            profile_id=PROFILE,
            profile_version="1.0.0",
        )
    )


def _round(phase: RoundPhase) -> SearchRound:
    values: dict[str, Any] = {
        "round_id": uuid4(),
        "task_id": uuid4(),
        "idempotency_key": "m2-formal-round-idempotency",
        "state": (
            SearchRoundState.SEARCH_MEASURING
            if phase is RoundPhase.SEARCH
            else SearchRoundState.HOLDOUT_MEASURING
        ),
        "run_mode": "formal",
        "project_mode": "degraded_manual_intake",
        "target_snapshot_id": uuid4(),
        "stage0_run_id": uuid4(),
        "stage0_protocol_hash": _hash("stage0"),
        "baseline_epoch_id": uuid4(),
        "hotspot_id": uuid4(),
        "replacement_point": "sglang.formal_kernel",
        "workload_id": "m2-formal-workload-v1",
        "workload_hash": _hash("workload"),
        "configuration_hash": _hash("configuration"),
        "image_digest": _hash("image"),
        "adapter_profile": PROFILE,
        "declared_candidate_count": 2,
        "max_promoted": 1,
        "family_alpha": 0.05,
        "search_plan_hash": _hash("search-plan"),
        "holdout_plan_commitment": _hash("holdout-commitment"),
        "holdout_plan_authority_id": "d-formal-holdout-authority",
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
                "holdout_plan_hash": _hash("holdout-plan"),
                "holdout_reveal_lease_id": uuid4(),
                "holdout_reveal_evidence_hash": _hash("holdout-reveal"),
            }
        )
    return SearchRound.model_validate(values)


def _authority(round_authority: SearchRound):  # type: ignore[no-untyped-def]
    return publish_formal_authority_context(
        FormalAuthorityContextContent(
            authority_context_id=uuid4(),
            round_id=round_authority.round_id,
            task_id=round_authority.task_id,
            target_snapshot_id=round_authority.target_snapshot_id,
            stage0_run_id=round_authority.stage0_run_id,
            stage0_protocol_hash=round_authority.stage0_protocol_hash,
            baseline_epoch_id=round_authority.baseline_epoch_id,
            hotspot_id=round_authority.hotspot_id,
            target_profile_hash=_hash("target-profile"),
            workload_profile_hash=_hash("workload-profile"),
            measurement_profile_hash=_hash("measurement-profile"),
            candidate_family_hash=round_authority.candidate_family_hash,
            artifact_family_hash=round_authority.artifact_family_hash,
            search_plan_hash=round_authority.search_plan_hash,
            holdout_plan_commitment=round_authority.holdout_plan_commitment,
            holdout_plan_authority_id=round_authority.holdout_plan_authority_id,
            holdout_plan_authority_hash=round_authority.holdout_plan_authority_hash,
            selection_rule_hash=round_authority.selection_rule_hash,
            evidence_store=FormalEvidenceStoreRef(
                store_id="formal-evidence-store",
                store_version=1,
                store_hash=_hash("store"),
                access_policy_hash=_hash("acl"),
            ),
            verifier=FormalVerifierRef(
                verifier_id="formal-verifier-d",
                verifier_version="v1",
                verifier_hash=_hash("verifier"),
            ),
            sealed_by="operator-a",
            sealed_at=NOW,
        )
    )


def _member(round_authority: SearchRound, phase: RoundPhase) -> RoundCandidate:
    return RoundCandidate(
        round_candidate_id=uuid4(),
        round_id=round_authority.round_id,
        candidate_id=uuid4(),
        ordinal=0,
        source_package_store_id="formal-source-store",
        source_package_store_hash=_hash("source-store"),
        source_package_hash=_hash("source-package"),
        source_manifest_hash=_hash("source-manifest"),
        baseline_source_hash=_hash("baseline-source"),
        candidate_source_hash=_hash("candidate-source"),
        optimization_intent="formal business candidate",
        replacement_point=round_authority.replacement_point,
        candidate_kind="business",
        artifact_id=uuid4(),
        artifact_hash=_hash("artifact"),
        state=(
            RoundCandidateState.CORRECTNESS_PASSED
            if phase is RoundPhase.SEARCH
            else RoundCandidateState.SEARCH_MEASURED
        ),
        idempotency_key="m2-formal-member-idempotency",
    )


def _register_execution_authority(
    round_authority: SearchRound,
    authority,
    member: RoundCandidate,
) -> tuple[FormalProfileWindowAuthorization, str]:  # type: ignore[no-untyped-def]
    profiles = FormalProfileSetRef(
        target_profile=OperatorProfileRef(
            profile_id="test-formal-target",
            profile_version=1,
            profile_kind="target",
            profile_hash=authority.target_profile_hash,
        ),
        workload_profile=OperatorProfileRef(
            profile_id="test-formal-workload",
            profile_version=1,
            profile_kind="workload",
            profile_hash=authority.workload_profile_hash,
        ),
        measurement_profile=OperatorProfileRef(
            profile_id="test-formal-measurement",
            profile_version=1,
            profile_kind="measurement",
            profile_hash=authority.measurement_profile_hash,
        ),
    )
    assert round_authority.candidate_family_hash is not None
    authorization = publish_formal_profile_window_authorization(
        FormalProfileWindowAuthorizationContent(
            authorization_id=uuid4(),
            decision="authorized",
            readiness_audit_id="test-formal-readiness",
            readiness_audit_base_commit="a" * 40,
            readiness_manifest_hash=_hash("readiness-manifest"),
            readiness_report_hash=_hash("readiness-report"),
            profiles=profiles,
            source_family_hash=round_authority.candidate_family_hash,
            budget=round_authority.budget,
            host_id="nmz36",
            resource_id="hcu-7",
            window_starts_at=NOW - timedelta(minutes=1),
            window_expires_at=NOW + timedelta(minutes=10),
            authorized_by="test-formal-owner",
            authorization_evidence_uri="evidence:///formal/window.json",
            authorization_evidence_hash=_hash("window-evidence"),
            issued_at=NOW - timedelta(minutes=2),
            verifier=VERIFIER_REF,
        ),
        signature="test-signature",
    )
    assert member.artifact_id is not None
    assert member.artifact_hash is not None
    candidate = ResolvedOperatorCandidate(
        ordinal=0,
        candidate_id=member.candidate_id,
        source_package_store_id=member.source_package_store_id,
        source_package_store_version=1,
        source_package_store_hash=member.source_package_store_hash,
        source_package_ref=CandidateSourcePackageRef(
            candidate_source_hash=member.candidate_source_hash,
            source_package_hash=member.source_package_hash,
            manifest_hash=member.source_manifest_hash,
            manifest_schema_version=member.source_manifest_version,
        ),
        baseline_source_hash=member.baseline_source_hash,
        hotspot_id=round_authority.hotspot_id,
        replacement_point=round_authority.replacement_point,
        candidate_kind="business",
        optimization_intent=member.optimization_intent,
    )
    second = candidate.model_copy(
        update={
            "ordinal": 1,
            "candidate_id": uuid5(member.candidate_id, "second-formal-candidate"),
            "source_package_ref": candidate.source_package_ref.model_copy(
                update={
                    "candidate_source_hash": _hash(
                        f"second-candidate-{member.candidate_id}"
                    ),
                    "source_package_hash": _hash(f"second-package-{member.candidate_id}"),
                    "manifest_hash": _hash(f"second-manifest-{member.candidate_id}"),
                }
            ),
            "optimization_intent": "second Formal business Candidate",
        }
    )
    hotspot = ProfilerOperatorHotspotRef(
        source="profiler",
        hotspot_id=round_authority.hotspot_id,
        hotspot_intake_hash=_hash("hotspot-intake"),
        profiler_evidence_uri="evidence:///formal/profiler.json",
        profiler_evidence_hash=_hash("profiler-evidence"),
        correctness_evidence_uri="evidence:///formal/correctness.json",
        correctness_evidence_hash=_hash("correctness-evidence"),
        replacement_point=round_authority.replacement_point,
        workload_hash=round_authority.workload_hash,
        shape=(1,),
        dtype="float32",
    )
    plan = FormalResolvedRoundPlan(
        target_profile=profiles.target_profile,
        workload_profile=profiles.workload_profile,
        measurement_profile=profiles.measurement_profile,
        hotspot=hotspot,
        authority=ResolvedOperatorAuthority(
            target_snapshot_id=round_authority.target_snapshot_id,
            stage0_run_id=round_authority.stage0_run_id,
            stage0_protocol_hash=round_authority.stage0_protocol_hash,
            baseline_epoch_id=round_authority.baseline_epoch_id,
            baseline_source_hash=member.baseline_source_hash,
            hotspot_id=round_authority.hotspot_id,
            replacement_point=round_authority.replacement_point,
            workload_id=round_authority.workload_id,
            workload_hash=round_authority.workload_hash,
            configuration_hash=round_authority.configuration_hash,
            image_digest=round_authority.image_digest,
            adapter_profile=round_authority.adapter_profile,
            profiler_evidence_uri=hotspot.profiler_evidence_uri,
            profiler_evidence_hash=hotspot.profiler_evidence_hash,
            synthetic=False,
        ),
        candidates=(candidate, second),
        candidate_input_set_hash=_hash(f"candidate-inputs-{member.candidate_id}"),
        search_protocol_version="m2-search-v1",
        search_protocol_hash=_hash("search-protocol"),
        holdout_protocol_version="m2-holdout-v1",
        holdout_protocol_hash=_hash("holdout-protocol"),
        selection_rule_hash=round_authority.selection_rule_hash,
        budget=round_authority.budget,
        max_promoted=round_authority.max_promoted,
        formal_authorization_hash=authorization.authorization_hash,
        authorized_host_id=authorization.host_id,
        authorized_resource_id=authorization.resource_id,
        authorization_window_starts_at=authorization.window_starts_at,
        authorization_window_expires_at=authorization.window_expires_at,
        authorized_source_family_hash=authorization.source_family_hash,
        source_family_hash=authorization.source_family_hash,
    )
    resolved_plan_hash = formal_operator_resolved_plan_hash(plan)
    AUTHORIZATIONS[authorization.authorization_id] = authorization
    RESOLVED_PLANS[resolved_plan_hash] = plan
    return authorization, resolved_plan_hash


def _request(round_authority, authority, member, phase):  # type: ignore[no-untyped-def]
    phase_plan_hash = (
        round_authority.search_plan_hash
        if phase is RoundPhase.SEARCH
        else round_authority.holdout_plan_hash
    )
    assert phase_plan_hash is not None
    measurement_plan_hash = _hash(f"measurement-{phase.value}")
    job_id = uuid4()
    planned = BudgetUsage(
        search_samples=8 if phase is RoundPhase.SEARCH else 0,
        holdout_samples=8 if phase is RoundPhase.HOLDOUT else 0,
        wall_seconds=20,
        exclusive_lease_seconds=20,
    )
    adapter_profile = _adapter_profile()
    authorization, resolved_plan_hash = _register_execution_authority(
        round_authority, authority, member
    )
    binding = M2FormalExecutionBinding(
        formal_authorization_id=authorization.authorization_id,
        formal_authorization_hash=authorization.authorization_hash,
        resolved_plan_hash=resolved_plan_hash,
        authority_context_id=authority.authority_context_id,
        authority_context_hash=authority.context_hash,
        round_id=round_authority.round_id,
        task_id=round_authority.task_id,
        candidate_family_hash=round_authority.candidate_family_hash,
        artifact_family_hash=round_authority.artifact_family_hash,
        holdout_family_hash=round_authority.holdout_family_hash,
        round_candidate_id=member.round_candidate_id,
        candidate_id=member.candidate_id,
        artifact_id=member.artifact_id,
        artifact_hash=member.artifact_hash,
        target_snapshot_id=round_authority.target_snapshot_id,
        target_profile_hash=authority.target_profile_hash,
        workload_profile_hash=authority.workload_profile_hash,
        measurement_profile_hash=authority.measurement_profile_hash,
        target_lock_hash=_hash("target-lock"),
        stage0_protocol_hash=round_authority.stage0_protocol_hash,
        adapter_profile=round_authority.adapter_profile,
        adapter_profile_version=adapter_profile.profile_version,
        adapter_profile_hash=adapter_profile.profile_hash,
        phase=phase,
        phase_plan_hash=phase_plan_hash,
        measurement_plan_hash=measurement_plan_hash,
        holdout_reveal_evidence_hash=(
            round_authority.holdout_reveal_evidence_hash if phase is RoundPhase.HOLDOUT else None
        ),
        host_id="nmz36",
        execution_host_hash=_hash("nmz36-host"),
        resource_id="hcu-7",
        device_index=7,
        numa_node=7,
        cpu_affinity="112-127",
        topology_lease_hash=_hash("topology-lease"),
        lease_id=uuid4(),
        fencing_token=9,
        lease_authority_hash=_hash("lease-authority"),
        lease_receipt_uri="evidence://formal-lease/test",
        lease_receipt_hash=_hash("lease-receipt"),
        lease_acquired_at=NOW - timedelta(minutes=2),
        lease_last_renewed_at=NOW - timedelta(seconds=30),
        lease_renewal_due_at=NOW + timedelta(minutes=5),
        lease_renewal_sequence=1,
        lease_expires_at=NOW + timedelta(minutes=20),
        window=M2FormalExecutionWindow(
            starts_at=authorization.window_starts_at,
            expires_at=authorization.window_expires_at,
        ),
        job_id=job_id,
        attempt=1,
    )
    return M2FormalPhaseExecutionRequest(
        binding=binding,
        reservation=M2PhaseBudgetReservationPlan(
            round_id=round_authority.round_id,
            round_candidate_id=member.round_candidate_id,
            candidate_id=member.candidate_id,
            phase=phase,
            phase_plan_hash=phase_plan_hash,
            measurement_plan_hash=measurement_plan_hash,
            expected_sample_count=8,
            reservation_id=uuid4(),
            job_id=job_id,
            attempt=1,
            planned=planned,
            idempotency_key=f"formal-budget-{phase.value}",
        ),
    )


class RecordingBudget:
    def __init__(self, reject: bool = False) -> None:
        self.reject = reject
        self.reserves = []
        self.finalizes = []
        self.views = {}

    def reserve(self, request):  # type: ignore[no-untyped-def]
        if self.reject:
            raise MeasurementSafetyError("round budget exhausted")
        self.reserves.append(request)
        view = RoundBudgetReservationView(
            **request.reservation.model_dump(mode="python"),
            created_at=NOW,
            updated_at=NOW,
        )
        self.views[view.reservation_id] = view
        return RoundBudgetMutationResult(reservation=view, ledger_entry=request.ledger_entry)

    def finalize(self, request):  # type: ignore[no-untyped-def]
        self.finalizes.append(request)
        entry = request.ledger_entry
        state = (
            RoundBudgetReservationState.SETTLED
            if entry.entry_type is RoundBudgetEntryType.SETTLE
            else RoundBudgetReservationState.RELEASED
        )
        view = self.views[entry.reservation_id].model_copy(update={"state": state})
        return RoundBudgetMutationResult(reservation=view, ledger_entry=entry)


class Harness:
    def __init__(self, result=None, error=None):  # type: ignore[no-untyped-def]
        self.result = result
        self.error = error
        self.payloads = []

    def run_manual_performance(self, payload, output_dir):  # type: ignore[no-untyped-def]
        del output_dir
        self.payloads.append(dict(payload))
        if self.error is not None:
            raise self.error
        return self.result


class StepClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class TickClock:
    def __init__(self) -> None:
        self.value = 1_000_000_000

    def __call__(self) -> int:
        value = self.value
        self.value += 1_000_000_000
        return value


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self.values = iter(values)

    def __call__(self) -> datetime:
        return next(self.values)


def _result(tmp_path: Path, request) -> ManualPerformanceEvidenceResult:  # type: ignore[no-untyped-def]
    raw = tmp_path / f"raw-{request.binding.phase.value}.json"
    raw.write_text(
        '{"schema_version":"m1-kernel-performance-evidence-v1",'
        f'"phase_fixture":"{request.binding.phase.value}"}}\n',
        encoding="utf-8",
    )
    digest = "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest()
    provenance = AdapterProvenance(
        profile=PROFILE,
        capability="measurement_harness",
        adapter_name="NoHCUFormalHarnessFixture",
        adapter_version="1",
        implementation_kind="real",
    )
    measurement_id = uuid4()
    return ManualPerformanceEvidenceResult(
        candidate_id=request.binding.candidate_id,
        measurement=MeasurementSeries(
            measurement_id=measurement_id,
            status="measured",
            metric_name="kernel_elapsed",
            unit="ns",
            protocol_version="m1-kernel-performance-v1",
            sample_count=8,
            raw_samples_uri=raw.resolve().as_uri(),
            raw_samples_hash=digest,
            environment_fingerprint=_hash("environment"),
            summary={
                "plan_hash": request.binding.measurement_plan_hash,
                "baseline_sample_set_hash": _hash(f"baseline-{measurement_id}"),
                "process_identity_set_hash": _hash(f"process-{measurement_id}"),
                "cache_namespace_set_hash": _hash(f"cache-{measurement_id}"),
            },
            adapter_provenance=provenance,
        ),
        cleanup_evidence=_cleanup(request.binding),
    )


def _cleanup(binding):  # type: ignore[no-untyped-def]
    return {
        "fence": {
            "fenced": True,
            "resource_id": binding.resource_id,
            "fencing_token": binding.fencing_token,
            "failures": [],
            "clocks_restored": True,
        },
        "health": {
            "healthy": True,
            "resource_id": binding.resource_id,
            "residual_owned_containers": [],
            "memory_release_check": "no_adapter_owned_container_remains",
            "clocks_restored": True,
        },
    }


def _directory_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink is unavailable: {error}")


def _refresh(binding, mode="matched"):  # type: ignore[no-untyped-def]
    synthetic = mode == "synthetic"
    status = "mismatched" if mode == "mismatched" else "matched"
    return publish_m2_formal_target_lock_refresh(
        M2FormalTargetLockRefreshContent(
            refresh_id=uuid5(
                NAMESPACE_URL,
                f"hcuopt:test-target-lock-refresh:{binding.lease_id}:{binding.fencing_token}",
            ),
            formal_authorization_hash=(
                _hash("wrong-formal-authorization")
                if mode == "wrong_binding"
                else binding.formal_authorization_hash
            ),
            authority_context_hash=binding.authority_context_hash,
            target_snapshot_id=binding.target_snapshot_id,
            target_lock_hash=binding.target_lock_hash,
            host_id=binding.host_id,
            resource_id=binding.resource_id,
            device_index=binding.device_index,
            numa_node=binding.numa_node,
            cpu_affinity=binding.cpu_affinity,
            lease_id=binding.lease_id,
            fencing_token=binding.fencing_token,
            observed_at=NOW - timedelta(seconds=10),
            valid_until=NOW if mode == "stale" else binding.window.expires_at,
            status=status,
            mismatch_codes=("topology_changed",) if status == "mismatched" else (),
            evidence_uri="evidence://target-lock-refresh/test",
            evidence_hash=_hash(f"target-lock-refresh-evidence-{mode}"),
            adapter_provenance=AdapterProvenance(
                profile="m2a-target-lock-refresh-v1",
                capability="target_lock_refresh",
                adapter_name="NoHCUTargetLockRefreshFixture",
                adapter_version="1",
                implementation_kind="fake" if synthetic else "real",
            ),
            synthetic=synthetic,
        )
    )


class TargetLockProbe:
    def __init__(self, mode="matched") -> None:  # type: ignore[no-untyped-def]
        self.mode = mode
        self.bindings = []

    def refresh(self, binding, observed_at):  # type: ignore[no-untyped-def]
        del observed_at
        self.bindings.append(binding)
        return _refresh(binding, self.mode)


def _adapter(
    tmp_path,
    harness,
    budget,
    live=lambda _binding, _now: True,
    registry=None,
    target_lock=lambda _binding, _now: True,
    recover_expired=None,
    probe=None,
    authority_reader=None,
    authorization_verifier=None,
    clock=None,
    failure_reader=None,
):  # type: ignore[no-untyped-def]
    return M2FormalPhaseExecutionAdapter(
        harness=harness,
        adapter_profile=_adapter_profile(),
        target_lock_probe=probe or TargetLockProbe(),
        budget_authority=budget,
        receipt_store=M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts"),
        authority_reader=authority_reader or AuthorityReader(),
        authorization_verifier=authorization_verifier or AuthorizationVerifier(),
        isolation_authority=(
            registry
            or SqliteM2FormalPhaseIsolationAuthority(tmp_path / "formal-isolation.sqlite3")
        ),
        lease_is_live=live,
        target_lock_is_live=target_lock,
        recover_resource=_cleanup,
        recover_expired_lease=recover_expired,
        clock=clock or StepClock(),
        monotonic_ns=TickClock(),
        failure_reader=failure_reader,
    )


def test_formal_binding_rejects_missing_lease_and_non_hcu_resource() -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    values = request.binding.model_dump(mode="python")
    values.pop("lease_id")
    with pytest.raises(ValidationError):
        M2FormalExecutionBinding.model_validate(values)
    values = request.binding.model_dump(mode="python")
    values["resource_id"] = "gpu-7"
    with pytest.raises(ValidationError, match="hcu-<index>"):
        M2FormalExecutionBinding.model_validate(values)
    values = request.binding.model_dump(mode="python")
    values["cpu_affinity"] = "127-112"
    with pytest.raises(ValidationError, match="CPU affinity"):
        M2FormalExecutionBinding.model_validate(values)


def test_execution_authority_hash_binds_fence_and_topology() -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)

    assert m2_formal_phase_execution_request_hash(request) == (
        m2_formal_phase_execution_request_hash(request)
    )
    changed = request.model_copy(
        update={
            "binding": request.binding.model_copy(
                update={"fencing_token": request.binding.fencing_token + 1}
            )
        }
    )
    assert m2_formal_phase_execution_request_hash(changed) != (
        m2_formal_phase_execution_request_hash(request)
    )


def test_formal_search_executes_through_unique_harness_and_settles(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    harness = Harness(_result(tmp_path, request))
    budget = RecordingBudget()

    outcome = _adapter(tmp_path, harness, budget).run(
        round_authority=round_authority,
        formal_authority=authority,
        member=member,
        request=request,
        output_dir=tmp_path / "execution",
    )

    receipt = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts").load(outcome.receipt_ref)
    assert receipt.execution.status == "succeeded"
    assert receipt.execution.binding.phase is RoundPhase.SEARCH
    assert receipt.execution.measurement_ref is not None
    assert receipt.execution.measurement_ref.baseline_sample_set_hash == (
        harness.result.measurement.summary["baseline_sample_set_hash"]
    )
    assert receipt.execution.measurement_ref.process_identity_set_hash == (
        harness.result.measurement.summary["process_identity_set_hash"]
    )
    assert receipt.execution.measurement_ref.cache_namespace_set_hash == (
        harness.result.measurement.summary["cache_namespace_set_hash"]
    )
    assert receipt.performance_conclusion == "not_measured"
    assert receipt.synthetic is False
    assert harness.payloads[0]["host_id"] == "nmz36"
    assert harness.payloads[0]["resource_id"] == "hcu-7"
    assert harness.payloads[0]["numa_node"] == 7
    assert harness.payloads[0]["cpu_affinity"] == "112-127"
    assert harness.payloads[0]["target_lock_refresh_hash"] == _refresh(
        request.binding
    ).report_hash
    assert budget.finalizes[0].ledger_entry.entry_type is RoundBudgetEntryType.SETTLE
    assert budget.finalizes[0].ledger_entry.actual.search_samples == 8
    payload = harness.payloads[0]
    assert payload["budget"] == {"max_samples": 8, "max_wall_seconds": 20}
    assert payload["task_id"] == str(request.binding.task_id)
    assert payload["workload_hash"] == round_authority.workload_hash
    assert payload["_job_context"]["lease_id"] == str(request.binding.lease_id)
    assert payload["_job_context"]["fencing_token"] == request.binding.fencing_token
    assert isinstance(payload["_job_context"]["lease_lost_event"], _FormalLeaseGuard)
    assert payload["_job_context"]["lease_lost_event"].is_set() is False


@pytest.mark.parametrize(
    "conflict",
    [
        {"_job_context": {"resource_id": "hcu-0"}},
        {"_job_context": {"lease_lost_event": False}},
        {"budget": {"max_samples": 800, "max_wall_seconds": 20}},
        {"budget": {"max_samples": 8, "max_wall_seconds": 200}},
        {"budget": {"max_samples": 8.0, "max_wall_seconds": 20}},
        {"task_id": str(uuid4())},
        {"workload_hash": "sha256:" + "f" * 64},
        {"candidate_id": str(uuid4())},
    ],
)
def test_formal_payload_conflicts_rejected_before_budget_or_harness(tmp_path, conflict):
    round_ = _round(RoundPhase.SEARCH)
    authority = _authority(round_)
    member = _member(round_, RoundPhase.SEARCH)
    request = _request(round_, authority, member, RoundPhase.SEARCH)
    request = request.model_copy(update={"harness_payload": conflict})
    budget, harness = RecordingBudget(), Harness()
    with pytest.raises(MeasurementSafetyError, match="override frozen"):
        _adapter(tmp_path, harness, budget).run(
            round_authority=round_,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / "execution",
        )
    assert budget.reserves == budget.finalizes == harness.payloads == []
    assert not (tmp_path / "execution").exists()


@pytest.mark.parametrize("mode", ["expired", "renewal", "window", "false", "truthy", "error"])
def test_formal_runtime_guard_fails_closed(mode):
    round_ = _round(RoundPhase.SEARCH)
    authority = _authority(round_)
    member = _member(round_, RoundPhase.SEARCH)
    binding = _request(round_, authority, member, RoundPhase.SEARCH).binding
    observed = {
        "expired": binding.lease_expires_at,
        "renewal": binding.lease_renewal_due_at,
        "window": binding.window.expires_at,
    }.get(mode, NOW)

    def live(_binding, _now):
        if mode == "error":
            raise RuntimeError("authority unavailable")
        return {"false": False, "truthy": 1}.get(mode, True)

    assert _FormalLeaseGuard(binding, lambda: observed, live).is_set() is True


def test_formal_m1_wall_budget_rounds_down_not_up():
    round_ = _round(RoundPhase.SEARCH)
    authority = _authority(round_)
    member = _member(round_, RoundPhase.SEARCH)
    request = _request(round_, authority, member, RoundPhase.SEARCH)
    for seconds, expected in ((1.9, 1), (0.9, None)):
        changed = request.model_copy(
            update={
                "reservation": request.reservation.model_copy(
                    update={
                        "planned": request.reservation.planned.model_copy(
                            update={"wall_seconds": seconds}
                        )
                    }
                )
            }
        )

        def build(changed=changed):
            return M2FormalPhaseExecutionAdapter._harness_payload(
                round_,
                member,
                changed,
                _refresh(changed.binding),
                m2_formal_phase_execution_request_hash(changed),
            )

        if expected is None:
            with pytest.raises(MeasurementSafetyError, match="whole M1 second"):
                build()
        else:
            assert build()["budget"]["max_wall_seconds"] == expected


def test_search_and_holdout_use_independent_execution_receipts(tmp_path: Path) -> None:
    search_round = _round(RoundPhase.SEARCH)
    authority = _authority(search_round)
    search_member = _member(search_round, RoundPhase.SEARCH)
    search_request = _request(search_round, authority, search_member, RoundPhase.SEARCH)
    registry = SqliteM2FormalPhaseIsolationAuthority(tmp_path / "phase-isolation.sqlite3")
    search = _adapter(
        tmp_path,
        Harness(_result(tmp_path, search_request)),
        RecordingBudget(),
        registry=registry,
    ).run(
        round_authority=search_round,
        formal_authority=authority,
        member=search_member,
        request=search_request,
        output_dir=tmp_path / "search",
    )

    holdout_round = SearchRound.model_validate(
        {
            **search_round.model_dump(mode="python"),
            "state": SearchRoundState.HOLDOUT_MEASURING,
            "holdout_family_hash": _hash("holdout-family"),
            "holdout_plan_hash": _hash("holdout-plan"),
            "holdout_reveal_lease_id": uuid4(),
            "holdout_reveal_evidence_hash": _hash("holdout-reveal"),
        }
    )
    holdout_member = search_member.model_copy(update={"state": RoundCandidateState.SEARCH_MEASURED})
    holdout_request = _request(holdout_round, authority, holdout_member, RoundPhase.HOLDOUT)
    holdout = _adapter(
        tmp_path,
        Harness(_result(tmp_path, holdout_request)),
        RecordingBudget(),
        registry=registry,
    ).run(
        round_authority=holdout_round,
        formal_authority=authority,
        member=holdout_member,
        request=holdout_request,
        output_dir=tmp_path / "holdout",
    )

    assert search.receipt_ref.phase is RoundPhase.SEARCH
    assert holdout.receipt_ref.phase is RoundPhase.HOLDOUT
    assert search.receipt_ref.receipt_id != holdout.receipt_ref.receipt_id
    assert search.receipt_ref.content_hash != holdout.receipt_ref.content_hash


def test_holdout_cannot_reuse_search_measurement_identity(tmp_path: Path) -> None:
    search_round = _round(RoundPhase.SEARCH)
    authority = _authority(search_round)
    member = _member(search_round, RoundPhase.SEARCH)
    search_request = _request(search_round, authority, member, RoundPhase.SEARCH)
    search_result = _result(tmp_path, search_request)
    registry = SqliteM2FormalPhaseIsolationAuthority(tmp_path / "phase-isolation.sqlite3")
    _adapter(
        tmp_path,
        Harness(search_result),
        RecordingBudget(),
        registry=registry,
    ).run(
        round_authority=search_round,
        formal_authority=authority,
        member=member,
        request=search_request,
        output_dir=tmp_path / "search-original",
    )

    holdout_round = SearchRound.model_validate(
        {
            **search_round.model_dump(mode="python"),
            "state": SearchRoundState.HOLDOUT_MEASURING,
            "holdout_family_hash": _hash("holdout-family"),
            "holdout_plan_hash": _hash("holdout-plan"),
            "holdout_reveal_lease_id": uuid4(),
            "holdout_reveal_evidence_hash": _hash("holdout-reveal"),
        }
    )
    holdout_member = member.model_copy(update={"state": RoundCandidateState.SEARCH_MEASURED})
    holdout_request = _request(holdout_round, authority, holdout_member, RoundPhase.HOLDOUT)
    holdout_result = _result(tmp_path, holdout_request)
    reused = holdout_result.model_copy(
        update={
            "measurement": holdout_result.measurement.model_copy(
                update={
                    "measurement_id": search_result.measurement.measurement_id,
                    "raw_samples_uri": search_result.measurement.raw_samples_uri,
                    "raw_samples_hash": search_result.measurement.raw_samples_hash,
                }
            )
        }
    )

    with pytest.raises(M2FormalExecutionFailure) as captured:
        _adapter(
            tmp_path,
            Harness(reused),
            RecordingBudget(),
            registry=SqliteM2FormalPhaseIsolationAuthority(
                tmp_path / "phase-isolation.sqlite3"
            ),
        ).run(
            round_authority=holdout_round,
            formal_authority=authority,
            member=holdout_member,
            request=holdout_request,
            output_dir=tmp_path / "holdout-reused",
        )

    receipt = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts").load(
        captured.value.receipt_ref
    )
    assert receipt.execution.status == "evidence_invalid"
    assert receipt.execution.error_code == "phase_identity_reused"


@pytest.mark.parametrize(
    "field_name",
    [
        "baseline_sample_set_hash",
        "process_identity_set_hash",
        "cache_namespace_set_hash",
    ],
)
def test_durable_isolation_rejects_cross_process_evidence_reuse(
    tmp_path: Path,
    field_name: str,
) -> None:
    search_round = _round(RoundPhase.SEARCH)
    authority = _authority(search_round)
    member = _member(search_round, RoundPhase.SEARCH)
    search_request = _request(search_round, authority, member, RoundPhase.SEARCH)
    search_result = _result(tmp_path, search_request)
    database = tmp_path / "durable-isolation.sqlite3"
    _adapter(
        tmp_path,
        Harness(search_result),
        RecordingBudget(),
        registry=SqliteM2FormalPhaseIsolationAuthority(database),
    ).run(
        round_authority=search_round,
        formal_authority=authority,
        member=member,
        request=search_request,
        output_dir=tmp_path / f"search-{field_name}",
    )

    holdout_round = SearchRound.model_validate(
        {
            **search_round.model_dump(mode="python"),
            "state": SearchRoundState.HOLDOUT_MEASURING,
            "holdout_family_hash": _hash("holdout-family"),
            "holdout_plan_hash": _hash("holdout-plan"),
            "holdout_reveal_lease_id": uuid4(),
            "holdout_reveal_evidence_hash": _hash("holdout-reveal"),
        }
    )
    holdout_member = member.model_copy(update={"state": RoundCandidateState.SEARCH_MEASURED})
    holdout_request = _request(holdout_round, authority, holdout_member, RoundPhase.HOLDOUT)
    holdout_result = _result(tmp_path, holdout_request)
    summary = dict(holdout_result.measurement.summary)
    summary[field_name] = search_result.measurement.summary[field_name]
    reused = holdout_result.model_copy(
        update={"measurement": holdout_result.measurement.model_copy(update={"summary": summary})}
    )

    with pytest.raises(M2FormalExecutionFailure) as captured:
        _adapter(
            tmp_path,
            Harness(reused),
            RecordingBudget(),
            registry=SqliteM2FormalPhaseIsolationAuthority(database),
        ).run(
            round_authority=holdout_round,
            formal_authority=authority,
            member=holdout_member,
            request=holdout_request,
            output_dir=tmp_path / f"holdout-{field_name}",
        )

    receipt = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts").load(
        captured.value.receipt_ref
    )
    assert receipt.execution.status == "evidence_invalid"
    assert receipt.execution.measurement_ref is None


def test_execution_rereads_plan_and_rejects_content_drift(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    authorization = AUTHORIZATIONS[request.binding.formal_authorization_id]
    plan = RESOLVED_PLANS[request.binding.resolved_plan_hash].model_copy(
        update={"selection_rule_hash": _hash("drifted-selection-rule")}
    )
    harness = Harness(_result(tmp_path, request))

    with pytest.raises(MeasurementSafetyError, match="Plan content Hash drifted"):
        _adapter(
            tmp_path,
            harness,
            RecordingBudget(),
            authority_reader=StaticAuthorityReader(authorization, plan),
        ).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / "plan-drift",
        )

    assert harness.payloads == []


def test_execution_rejects_owner_signature_before_harness(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    harness = Harness(_result(tmp_path, request))

    with pytest.raises(MeasurementSafetyError, match="signature was rejected"):
        _adapter(
            tmp_path,
            harness,
            RecordingBudget(),
            authorization_verifier=RejectingAuthorizationVerifier(),
        ).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / "signature-rejected",
        )

    assert harness.payloads == []


@pytest.mark.parametrize(
    "mode",
    ["expired", "renewal_overdue", "stale", "target_lock", "budget"],
)
def test_formal_preflight_and_budget_fail_before_harness(tmp_path: Path, mode: str) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    if mode == "expired":
        binding = request.binding.model_copy(
            update={
                "window": M2FormalExecutionWindow(
                    starts_at=NOW - timedelta(minutes=20),
                    expires_at=NOW - timedelta(minutes=10),
                ),
                "lease_expires_at": NOW + timedelta(minutes=20),
            }
        )
        request = request.model_copy(update={"binding": binding})
    if mode == "renewal_overdue":
        binding = request.binding.model_copy(update={"lease_renewal_due_at": NOW})
        request = request.model_copy(update={"binding": binding})
    harness = Harness(_result(tmp_path, request))
    budget = RecordingBudget(reject=mode == "budget")
    live = (lambda _binding, _now: False) if mode == "stale" else (lambda _b, _n: True)
    target_lock = (
        (lambda _binding, _now: False) if mode == "target_lock" else (lambda _binding, _now: True)
    )
    probe = TargetLockProbe()

    with pytest.raises(MeasurementSafetyError):
        _adapter(
            tmp_path,
            harness,
            budget,
            live,
            target_lock=target_lock,
            probe=probe,
        ).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / mode,
        )
    assert harness.payloads == []
    if mode != "budget":
        assert probe.bindings == []


def test_execution_crossing_renewal_deadline_fails_closed(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    binding = request.binding.model_copy(
        update={"lease_renewal_due_at": NOW + timedelta(seconds=2)}
    )
    reservation = request.reservation.model_copy(
        update={
            "planned": BudgetUsage(
                search_samples=8,
                wall_seconds=1,
                exclusive_lease_seconds=1,
            )
        }
    )
    request = request.model_copy(
        update={"binding": binding, "reservation": reservation}
    )
    budget = RecordingBudget()

    with pytest.raises(M2FormalExecutionFailure) as captured:
        _adapter(
            tmp_path,
            Harness(_result(tmp_path, request)),
            budget,
            clock=SequenceClock(NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)),
        ).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / "renewal-crossed",
        )

    receipt = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts").load(
        captured.value.receipt_ref
    )
    assert receipt.execution.status == "fence_lost"
    assert receipt.execution.error_code == "lease_renewal_overdue"
    assert budget.finalizes[0].ledger_entry.entry_type is RoundBudgetEntryType.SETTLE


def test_expired_lease_recovers_before_rejecting_execution(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    binding = request.binding.model_copy(
        update={
            "window": M2FormalExecutionWindow(
                starts_at=NOW - timedelta(minutes=20),
                expires_at=NOW - timedelta(minutes=10),
            ),
            "lease_expires_at": NOW - timedelta(minutes=5),
        }
    )
    request = request.model_copy(update={"binding": binding})
    recovered = []

    with pytest.raises(MeasurementSafetyError, match="expired and was recovered"):
        _adapter(
            tmp_path,
            Harness(_result(tmp_path, request)),
            RecordingBudget(),
            recover_expired=lambda expired: recovered.append(expired) or _cleanup(expired),
        ).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / "expired-lease",
        )

    assert recovered == [binding]


@pytest.mark.parametrize("mode", ["mismatched", "synthetic", "stale", "wrong_binding"])
def test_target_lock_refresh_must_be_real_matched_and_current(
    tmp_path: Path,
    mode: str,
) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    harness = Harness(_result(tmp_path, request))

    with pytest.raises(MeasurementSafetyError, match="Target Lock refresh"):
        _adapter(
            tmp_path,
            harness,
            RecordingBudget(),
            probe=TargetLockProbe(mode),
        ).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / f"refresh-{mode}",
        )

    assert harness.payloads == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (M2FormalHarnessFailure("timed out", error_code="execution_timed_out"), "timed_out"),
        (
            M2FormalHarnessFailure(
                "cleanup failed",
                error_code="cleanup_failed",
                actual_sample_count=3,
                cleanup_evidence={
                    "fence": {"fenced": True},
                    "health": {"healthy": False},
                },
            ),
            "cleanup_failed",
        ),
    ],
)
def test_started_failures_settle_and_publish_receipt(
    tmp_path: Path, error: Exception, status: str
) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    budget = RecordingBudget()

    with pytest.raises(M2FormalExecutionFailure) as captured:
        _adapter(tmp_path, Harness(error=error), budget).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / status,
        )
    receipt = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts").load(
        captured.value.receipt_ref
    )
    assert receipt.execution.status == status
    assert receipt.execution.cleanup_status == (
        "failed" if status == "cleanup_failed" else "verified"
    )
    assert budget.finalizes[0].ledger_entry.entry_type is RoundBudgetEntryType.SETTLE


def test_fence_lost_after_harness_settles_and_publishes_receipt(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    budget = RecordingBudget()
    checks = iter((True, False))

    with pytest.raises(M2FormalExecutionFailure) as captured:
        _adapter(
            tmp_path,
            Harness(_result(tmp_path, request)),
            budget,
            live=lambda _binding, _now: next(checks),
        ).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=tmp_path / "fence-lost",
        )

    receipt = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts").load(
        captured.value.receipt_ref
    )
    assert receipt.execution.status == "fence_lost"
    assert receipt.execution.error_code == "fencing_token_lost"
    assert budget.finalizes[0].ledger_entry.entry_type is RoundBudgetEntryType.SETTLE


@pytest.mark.parametrize("mode", [
    "valid", "native", "tampered", "wrong_candidate", "wrong_request", "unpublished", "generic"
])
def test_formal_failure_accounting_rereads_evidence_or_retains_reservation(tmp_path, mode):
    if mode == "native" and os.name != "posix":
        pytest.skip("native no-follow evidence reader requires POSIX")
    round_ = _round(RoundPhase.SEARCH)
    authority = _authority(round_)
    member = _member(round_, RoundPhase.SEARCH)
    request = _request(round_, authority, member, RoundPhase.SEARCH)
    b = request.binding
    binding = M1MeasurementBinding.model_validate_json(json.dumps({
        "task_id": str(b.task_id), "candidate_id": str(b.candidate_id),
        "round_id": str(b.round_id), "baseline_epoch_id": str(round_.baseline_epoch_id),
        "stage0_run_id": str(round_.stage0_run_id), "target_snapshot_id": str(b.target_snapshot_id),
        "target_id": "fixture", "target_fingerprint": _hash("target"),
        "environment_fingerprint": _hash("environment"), "workload_id": round_.workload_id,
        "workload_hash": round_.workload_hash, "configuration_hash": round_.configuration_hash,
        "image_digest": round_.image_digest, "baseline_source_hash": _hash("baseline"),
        "candidate_source_hash": _hash("candidate"), "artifact_id": str(b.artifact_id),
        "artifact_content_hash": b.artifact_hash, "measurement_id": str(uuid4()),
        "adapter_profile": b.adapter_profile,
        "lease": {"lease_id": str(b.lease_id), "lease_scope": "exclusive",
                  "resource_id": b.resource_id, "fencing_token": b.fencing_token},
    }))
    if mode == "wrong_candidate":
        binding = binding.model_copy(update={"candidate_id": uuid4()})
    report = M1FailureReport(
        binding=binding, plan_hash=b.measurement_plan_hash, expected_sample_count=8,
        formal_execution_request_hash=(
            _hash("other-attempt") if mode == "wrong_request"
            else m2_formal_phase_execution_request_hash(request)
        ),
        attempted_sample_count=1, verified_samples=(), completed_acquisitions=(),
        calibration=None, cleanup_evidence=_cleanup(b), error_type="TimeoutError", error="fixture",
    )
    raw_path = tmp_path / ("execution/failure.json" if mode == "native" else "failure.json")
    ref = write_evidence(raw_path, report)
    error = M1MeasurementFailure("fixture failure", ref)
    if mode == "tampered":
        (tmp_path / "failure.json").chmod(0o600)  # Corrupt this test-owned immutable fixture only.
        (tmp_path / "failure.json").write_text("{}\n", encoding="utf-8")
    elif mode == "unpublished":
        error = M1MeasurementFailure("fixture unavailable", None)
    elif mode == "generic":
        error = TimeoutError("no accounting proof")
    budget = RecordingBudget()
    adapter = _adapter(
        tmp_path, Harness(error=error), budget,
        failure_reader=None if mode == "native" else PortableReader(tmp_path)
    )
    with pytest.raises(MeasurementSafetyError) as captured:
        adapter.run(round_authority=round_, formal_authority=authority, member=member,
                    request=request, output_dir=tmp_path / "execution")
    assert len(budget.reserves) == 1
    if mode in {"valid", "native"}:
        assert isinstance(captured.value, M2FormalExecutionFailure)
        receipt = adapter.receipt_store.load(captured.value.receipt_ref)
        assert receipt.execution.measurement_ref is None
        assert receipt.execution.sample_count == 1
        assert len(budget.finalizes) == 1
        assert budget.finalizes[0].ledger_entry.actual.search_samples == 1
        usage = json.loads(next((tmp_path / "execution/budget/usage").glob("*.json")).read_bytes())
        assert usage["failure_accounting"]["verified_sample_count"] == 0
        assert usage["failure_accounting"]["attempted_sample_count"] == 1
        assert usage["failure_accounting"]["failure_evidence_hash"] == ref.sha256
    else:
        assert "reservation retained" in str(captured.value)
        assert budget.finalizes == []
        blocked = json.loads(next(
            (tmp_path / "execution/budget/accounting-blocked").glob("*.json")
        ).read_bytes())
        assert blocked["usage"] == "unknown"
        assert blocked["reservation_action"] == "retained"
        assert blocked["cleanup_evidence"]["health"]["healthy"] is True


def test_unstarted_execution_releases_budget(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    budget = RecordingBudget()
    harness = Harness(_result(tmp_path, request))

    _, released = _adapter(tmp_path, harness, budget).release_unstarted(
        request=request,
        output_dir=tmp_path / "released",
        reason="cancelled_before_harness",
    )
    assert released.ledger_entry.entry_type is RoundBudgetEntryType.RELEASE
    assert released.ledger_entry.actual.is_zero()
    assert harness.payloads == []


def test_receipt_id_cannot_be_rebound(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    store = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts")
    adapter = M2FormalPhaseExecutionAdapter(
        harness=Harness(_result(tmp_path, request)),
        adapter_profile=_adapter_profile(),
        target_lock_probe=TargetLockProbe(),
        budget_authority=RecordingBudget(),
        receipt_store=store,
        authority_reader=AuthorityReader(),
        authorization_verifier=AuthorizationVerifier(),
        isolation_authority=SqliteM2FormalPhaseIsolationAuthority(
            tmp_path / "phase-isolation.sqlite3"
        ),
        lease_is_live=lambda _binding, _now: True,
        target_lock_is_live=lambda _binding, _now: True,
        recover_resource=_cleanup,
        clock=StepClock(),
        monotonic_ns=TickClock(),
    )
    outcome = adapter.run(
        round_authority=round_authority,
        formal_authority=authority,
        member=member,
        request=request,
        output_dir=tmp_path / "success",
    )
    original = store.load(outcome.receipt_ref).execution

    with pytest.raises(SourceArtifactError, match="immutable identity"):
        store.publish(
            original.model_copy(update={"finished_at": original.finished_at + timedelta(seconds=1)})
        )


def test_identical_execution_receipt_replay_is_idempotent(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    store = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts")
    outcome = _adapter(
        tmp_path,
        Harness(_result(tmp_path, request)),
        RecordingBudget(),
    ).run(
        round_authority=round_authority,
        formal_authority=authority,
        member=member,
        request=request,
        output_dir=tmp_path / "replay",
    )
    receipt = store.load(outcome.receipt_ref)

    assert store.publish(receipt.execution) == outcome.receipt_ref


def test_concurrent_identical_receipt_publication_is_atomic(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    source_store = M2FormalPhaseExecutionReceiptStore(tmp_path / "source-receipts")
    outcome = M2FormalPhaseExecutionAdapter(
        harness=Harness(_result(tmp_path, request)),
        adapter_profile=_adapter_profile(),
        target_lock_probe=TargetLockProbe(),
        budget_authority=RecordingBudget(),
        receipt_store=source_store,
        authority_reader=AuthorityReader(),
        authorization_verifier=AuthorizationVerifier(),
        isolation_authority=SqliteM2FormalPhaseIsolationAuthority(
            tmp_path / "source-isolation.sqlite3"
        ),
        lease_is_live=lambda _binding, _now: True,
        target_lock_is_live=lambda _binding, _now: True,
        recover_resource=_cleanup,
        clock=StepClock(),
        monotonic_ns=TickClock(),
    ).run(
        round_authority=round_authority,
        formal_authority=authority,
        member=member,
        request=request,
        output_dir=tmp_path / "source-execution",
    )
    record = source_store.load(outcome.receipt_ref).execution
    store = M2FormalPhaseExecutionReceiptStore(tmp_path / "concurrent-receipts")

    with ThreadPoolExecutor(max_workers=8) as pool:
        references = tuple(pool.map(lambda _index: store.publish(record), range(16)))

    assert len(set(references)) == 1


def test_receipt_store_rejects_parent_symlink_escape(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    trusted_store = M2FormalPhaseExecutionReceiptStore(tmp_path / "receipts")
    outcome = _adapter(
        tmp_path,
        Harness(_result(tmp_path, request)),
        RecordingBudget(),
    ).run(
        round_authority=round_authority,
        formal_authority=authority,
        member=member,
        request=request,
        output_dir=tmp_path / "trusted-execution",
    )
    record = trusted_store.load(outcome.receipt_ref).execution
    store_root = tmp_path / "symlinked-store"
    outside = tmp_path / "outside-store"
    store_root.mkdir()
    outside.mkdir()
    _directory_symlink_or_skip(store_root / "receipts", outside)
    store = M2FormalPhaseExecutionReceiptStore(store_root)

    with pytest.raises(SourceArtifactError, match="link|trusted"):
        store.publish(record)

    assert tuple(outside.rglob("*")) == ()


def test_budget_evidence_rejects_parent_symlink_escape(tmp_path: Path) -> None:
    round_authority = _round(RoundPhase.SEARCH)
    authority = _authority(round_authority)
    member = _member(round_authority, RoundPhase.SEARCH)
    request = _request(round_authority, authority, member, RoundPhase.SEARCH)
    output_dir = tmp_path / "unsafe-execution"
    outside = tmp_path / "outside-budget"
    output_dir.mkdir()
    outside.mkdir()
    _directory_symlink_or_skip(output_dir / "budget", outside)
    harness = Harness(_result(tmp_path, request))

    with pytest.raises(SourceArtifactError, match="link|trusted"):
        _adapter(tmp_path, harness, RecordingBudget()).run(
            round_authority=round_authority,
            formal_authority=authority,
            member=member,
            request=request,
            output_dir=output_dir,
        )

    assert harness.payloads == []
    assert tuple(outside.rglob("*")) == ()
