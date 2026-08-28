# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from hcuopt.adapters.m2_candidate import (
    ScriptedCandidateIntake,
    candidate_source_package_hash,
)
from hcuopt.contracts.m2 import RoundBudget
from hcuopt.contracts.operator_v1 import (
    MeasurementOperatorProfileRefs,
    OperatorCandidateInput,
    OperatorHotspotRef,
    OperatorProfileRef,
    OperatorServiceIdentity,
    PreflightCheckResult,
    ResolvedOperatorAuthority,
    ResolvedOperatorCandidate,
    ResolvedRoundPlan,
    RoundPlanPreviewRequest,
    RoundPlanPreviewView,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.domain.enums import ManualCandidateKind, SearchRoundRunMode
from hcuopt.domain.errors import Conflict, NotFound, SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator.errors import (
    OperatorProfileModeMismatch,
    OperatorServiceIdentityMismatch,
)
from hcuopt.operator.profiles import OperatorProfileCatalog


class OperatorAuthorityRepository(Protocol):
    def resolve_scripted_operator_authority(
        self,
        target: TargetOperatorProfileRefs,
        workload: WorkloadOperatorProfileRefs,
        hotspot: OperatorHotspotRef,
    ) -> ResolvedOperatorAuthority: ...


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def operator_preview_request_digest(request: RoundPlanPreviewRequest) -> str:
    return _sha256(request.model_dump(mode="json", exclude={"idempotency_key"}))


def operator_resolved_plan_hash(plan: ResolvedRoundPlan) -> str:
    return _sha256(plan)


def _profile_ref(reference: OperatorProfileRef) -> OperatorProfileRef:
    return OperatorProfileRef.model_validate(reference.model_dump(mode="json"))


class OperatorPlanCompiler:
    """Compile one Scripted Preview without creating a Task or SearchRound."""

    def __init__(
        self,
        profiles: OperatorProfileCatalog,
        service_identity: OperatorServiceIdentity,
        *,
        candidate_intake: ScriptedCandidateIntake | None = None,
        ttl: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Operator Preview TTL must be positive")
        self.profiles = profiles
        self.service_identity = service_identity
        self.candidate_intake = candidate_intake
        self.ttl = ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def compile(
        self,
        request: RoundPlanPreviewRequest,
        authority_repository: OperatorAuthorityRepository,
    ) -> RoundPlanPreviewView:
        if request.expected_service_identity.model_dump(mode="json") != (
            self.service_identity.model_dump(mode="json")
        ):
            raise OperatorServiceIdentityMismatch(
                "Operator service identity changed; create a new Preview"
            )

        target_profile = self.profiles.require(request.target_profile, request.run_mode)
        workload_profile = self.profiles.require(request.workload_profile, request.run_mode)
        measurement_profile = self.profiles.require(
            request.measurement_profile, request.run_mode
        )
        target = TargetOperatorProfileRefs.model_validate(target_profile.authority_refs)
        workload = WorkloadOperatorProfileRefs.model_validate(
            workload_profile.authority_refs
        )
        measurement = MeasurementOperatorProfileRefs.model_validate(
            measurement_profile.authority_refs
        )
        if request.run_mode is not SearchRoundRunMode.SCRIPTED or not all(
            item.synthetic for item in (target_profile, workload_profile, measurement_profile)
        ):
            raise OperatorProfileModeMismatch(
                "Only synthetic Scripted Operator Plans are authorized"
            )

        budget = RoundBudget.model_validate(
            {
                **measurement.budget.model_dump(mode="json"),
                "max_candidates": len(request.candidates),
            }
        )
        checks = [
            self._pass(
                "operator_service_identity_current",
                "service",
                "Operator service identity matches this deployment.",
            ),
            self._pass(
                "operator_profiles_verified",
                "profiles",
                "Exact synthetic Scripted Profile versions and Hashes are registered.",
            ),
        ]
        deprecated_profiles = tuple(
            profile.profile_id
            for profile in (target_profile, workload_profile, measurement_profile)
            if profile.state == "deprecated"
        )
        if deprecated_profiles:
            checks.append(
                PreflightCheckResult(
                    code="operator_profile_deprecated",
                    scope="profiles",
                    status="warn",
                    message=(
                        "Deprecated Profiles require explicit acknowledgement: "
                        + ", ".join(deprecated_profiles)
                    ),
                    retryable=True,
                    action_code="acknowledge_deprecated_profiles",
                )
            )
        authority: ResolvedOperatorAuthority | None = None
        resolved_candidates: tuple[ResolvedOperatorCandidate, ...] = ()
        candidate_input_set_hash: str | None = None
        try:
            authority = authority_repository.resolve_scripted_operator_authority(
                target, workload, request.hotspot
            )
            if not authority.synthetic:
                raise Conflict("Scripted Authority must remain synthetic")
        except (Conflict, NotFound):
            authority = None
            checks.append(
                self._block(
                    "operator_authority_unavailable",
                    "authority",
                    "A matching frozen Scripted Authority is not available.",
                    retryable=True,
                    action_code="refresh_scripted_authority",
                )
            )
        else:
            checks.append(
                self._pass(
                    "operator_authority_resolved",
                    "authority",
                    "Target, Stage 0, Baseline, Workload, and Hotspot Authority resolved.",
                )
            )
            try:
                resolved_candidates = self._resolve_candidates(
                    request.candidates, target, authority
                )
                candidate_input_set_hash = self._candidate_input_set_hash(
                    resolved_candidates
                )
            except (OSError, SourceArtifactError, ValueError):
                resolved_candidates = ()
                checks.append(
                    self._block(
                        "operator_candidate_package_invalid",
                        "candidates",
                        "Candidate Packages could not be verified by the bound Store.",
                        retryable=False,
                        action_code="replace_candidate_packages",
                    )
                )
            else:
                checks.append(
                    self._pass(
                        "operator_candidate_packages_verified",
                        "candidates",
                        "All Candidate Packages and Manifest bindings were verified.",
                    )
                )

        plan = ResolvedRoundPlan(
            run_mode=request.run_mode,
            target_profile=_profile_ref(request.target_profile),
            workload_profile=_profile_ref(request.workload_profile),
            measurement_profile=_profile_ref(request.measurement_profile),
            hotspot=request.hotspot,
            authority=authority,
            candidates=resolved_candidates,
            candidate_input_set_hash=candidate_input_set_hash,
            search_protocol_version=measurement.search_protocol_version,
            search_protocol_hash=measurement.search_protocol_hash,
            holdout_protocol_version=measurement.holdout_protocol_version,
            holdout_protocol_hash=measurement.holdout_protocol_hash,
            selection_rule_hash=measurement.selection_rule_hash,
            budget=budget,
            max_promoted=request.max_promoted,
            conclusion_boundary=measurement.conclusion_boundary,
            synthetic=True,
        )
        request_digest = operator_preview_request_digest(request)
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Operator Preview clock must return a timezone-aware datetime")
        blocked = any(item.status == "block" for item in checks)
        warning_codes = tuple(sorted(item.code for item in checks if item.status == "warn"))
        return RoundPlanPreviewView(
            preview_id=uuid5(
                NAMESPACE_URL, f"hcuopt:operator-preview:{request.idempotency_key}"
            ),
            preview_request_digest=request_digest,
            resolved_plan_hash=operator_resolved_plan_hash(plan),
            resolved_plan=plan,
            checks=tuple(checks),
            start_allowed=not blocked,
            required_ack_codes=warning_codes,
            expires_at=now + self.ttl,
            service_identity=self.service_identity,
            synthetic=True,
            created_at=now,
        )

    def _resolve_candidates(
        self,
        candidates: tuple[OperatorCandidateInput, ...],
        target: TargetOperatorProfileRefs,
        authority: ResolvedOperatorAuthority,
    ) -> tuple[ResolvedOperatorCandidate, ...]:
        intake = self.candidate_intake
        if intake is None or (
            intake.store_id != target.candidate_package_store_id
            or intake.store_hash != target.candidate_package_store_hash
        ):
            raise SourceArtifactError("Operator Candidate Store Authority is unavailable")
        resolved: list[ResolvedOperatorCandidate] = []
        candidate_ids = set()
        for item in candidates:
            reference = item.source_package_ref
            package = intake.source_packages.read(
                candidate_source_hash=reference.candidate_source_hash
            )
            manifest = package.manifest
            package_hash = candidate_source_package_hash(
                package.manifest_hash, manifest.files
            )
            expected = (
                reference.manifest_schema_version,
                reference.manifest_hash,
                reference.source_package_hash,
                authority.hotspot_id,
                authority.baseline_source_hash,
                reference.candidate_source_hash,
                authority.profiler_evidence_uri,
                authority.profiler_evidence_hash,
                authority.replacement_point,
                ManualCandidateKind.FIXTURE,
            )
            actual = (
                manifest.schema_version,
                package.manifest_hash,
                package_hash,
                manifest.hotspot_id,
                manifest.baseline_source_hash,
                manifest.candidate_source_hash,
                manifest.profiler_evidence_uri,
                manifest.profiler_evidence_hash,
                manifest.replacement_point,
                manifest.candidate_kind,
            )
            if expected != actual or manifest.candidate_id in candidate_ids:
                raise SourceArtifactError(
                    "Candidate Package does not match the resolved Operator Authority"
                )
            candidate_ids.add(manifest.candidate_id)
            resolved.append(
                ResolvedOperatorCandidate(
                    ordinal=item.ordinal,
                    candidate_id=manifest.candidate_id,
                    source_package_store_id=target.candidate_package_store_id,
                    source_package_store_version=target.candidate_package_store_version,
                    source_package_store_hash=target.candidate_package_store_hash,
                    source_package_ref=reference,
                    baseline_source_hash=manifest.baseline_source_hash,
                    hotspot_id=manifest.hotspot_id,
                    replacement_point=authority.replacement_point,
                    candidate_kind=manifest.candidate_kind.value,
                    optimization_intent=item.optimization_intent,
                )
            )
        return tuple(resolved)

    @staticmethod
    def _candidate_input_set_hash(
        candidates: tuple[ResolvedOperatorCandidate, ...],
    ) -> str:
        return _sha256(
            [item.model_dump(mode="json") for item in sorted(candidates, key=lambda x: x.ordinal)]
        )

    @staticmethod
    def _pass(code: str, scope: str, message: str) -> PreflightCheckResult:
        return PreflightCheckResult(
            code=code,
            scope=scope,
            status="pass",
            message=message,
            retryable=False,
            action_code="none_required",
        )

    @staticmethod
    def _block(
        code: str,
        scope: str,
        message: str,
        *,
        retryable: bool,
        action_code: str,
    ) -> PreflightCheckResult:
        return PreflightCheckResult(
            code=code,
            scope=scope,
            status="block",
            message=message,
            retryable=retryable,
            action_code=action_code,
        )
