# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.adapters.business_candidate_family import BusinessCandidateFamilyVerifier
from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileWindowAuthorization,
)
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.contracts.m2_formal_operator_v1 import (
    FormalOperatorAuthoritySnapshot,
    FormalResolvedRoundPlan,
    FormalRoundPlanPreviewRequest,
    FormalRoundPlanPreviewView,
)
from hcuopt.contracts.operator_v1 import (
    MeasurementOperatorProfileRefs,
    OperatorProfileRef,
    OperatorServiceIdentity,
    PreflightCheckResult,
    ResolvedOperatorCandidate,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.domain.enums import SearchRoundRunMode
from hcuopt.domain.errors import Conflict, NotFound, SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator.errors import (
    OperatorFormalAuthorizationInvalid,
    OperatorProfileModeMismatch,
    OperatorServiceIdentityMismatch,
)
from hcuopt.operator.profiles import OperatorProfileCatalog


class FormalOperatorAuthorityRepository(Protocol):
    def resolve_formal_operator_authority(
        self,
        target: TargetOperatorProfileRefs,
        workload: WorkloadOperatorProfileRefs,
        candidate_family: BusinessCandidateFamilyManifest,
    ) -> FormalOperatorAuthoritySnapshot: ...

    def assert_operator_candidate_ids_available(
        self,
        candidate_ids: tuple[UUID, ...],
        *,
        allowed_round_id: UUID | None = None,
    ) -> None: ...


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def formal_operator_preview_request_digest(request: FormalRoundPlanPreviewRequest) -> str:
    return _sha256(request.model_dump(mode="json", exclude={"idempotency_key"}))


def formal_operator_resolved_plan_hash(plan: FormalResolvedRoundPlan) -> str:
    return _sha256(plan)


def _profile_ref(reference: OperatorProfileRef) -> OperatorProfileRef:
    return OperatorProfileRef.model_validate(reference.model_dump(mode="json"))


class FormalOperatorPlanCompiler:
    """Compile a verified Formal Family into a Preview; never create a Round."""

    def __init__(
        self,
        profiles: OperatorProfileCatalog,
        service_identity: OperatorServiceIdentity,
        *,
        authorization: FormalProfileWindowAuthorization,
        candidate_family_verifier: BusinessCandidateFamilyVerifier,
        ttl: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Formal Operator Preview TTL must be positive")
        if profiles.formal_authorization_hash != authorization.authorization_hash:
            raise OperatorFormalAuthorizationInvalid(
                "Formal Profile Catalog and window authorization do not match"
            )
        if authorization.decision != "authorized":
            raise OperatorFormalAuthorizationInvalid(
                "Formal Profile window did not authorize compilation"
            )
        self.profiles = profiles
        self.service_identity = service_identity
        self.authorization = authorization
        self.candidate_family_verifier = candidate_family_verifier
        self.ttl = ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def compile(
        self,
        request: FormalRoundPlanPreviewRequest,
        authority_repository: FormalOperatorAuthorityRepository,
        *,
        allowed_round_id: UUID | None = None,
    ) -> FormalRoundPlanPreviewView:
        if request.expected_service_identity.model_dump(mode="json") != (
            self.service_identity.model_dump(mode="json")
        ):
            raise OperatorServiceIdentityMismatch(
                "Operator service identity changed; create a new Formal Preview"
            )
        if (
            request.expected_formal_authorization_hash
            != self.authorization.authorization_hash
        ):
            raise OperatorFormalAuthorizationInvalid(
                "Formal window authorization changed; create a new Preview"
            )

        target_profile = self.profiles.require(
            request.target_profile, SearchRoundRunMode.FORMAL
        )
        workload_profile = self.profiles.require(
            request.workload_profile, SearchRoundRunMode.FORMAL
        )
        measurement_profile = self.profiles.require(
            request.measurement_profile, SearchRoundRunMode.FORMAL
        )
        actual_profile_refs = (
            _profile_ref(request.target_profile),
            _profile_ref(request.workload_profile),
            _profile_ref(request.measurement_profile),
        )
        authorized_profile_refs = (
            self.authorization.profiles.target_profile,
            self.authorization.profiles.workload_profile,
            self.authorization.profiles.measurement_profile,
        )
        if actual_profile_refs != authorized_profile_refs or any(
            profile.synthetic
            for profile in (target_profile, workload_profile, measurement_profile)
        ):
            raise OperatorProfileModeMismatch(
                "Formal Plan requires the exact authorized non-synthetic Profile Set"
            )

        target = TargetOperatorProfileRefs.model_validate(target_profile.authority_refs)
        workload = WorkloadOperatorProfileRefs.model_validate(workload_profile.authority_refs)
        measurement = MeasurementOperatorProfileRefs.model_validate(
            measurement_profile.authority_refs
        )
        checks = [
            self._pass(
                "formal_operator_service_identity_current",
                "service",
                "Operator service identity matches this deployment.",
            ),
            self._pass(
                "formal_operator_profiles_verified",
                "profiles",
                "Exact authorized Formal Profile versions and Hashes are registered.",
            ),
            self._pass(
                "formal_window_authorization_verified",
                "authorization",
                "The content-addressed project-owner Formal window is active.",
            ),
        ]

        budget_matches = measurement.budget == self.authorization.budget
        if budget_matches:
            checks.append(
                self._pass(
                    "formal_budget_verified",
                    "budget",
                    "Measurement budget matches the signed Formal window.",
                )
            )
        else:
            checks.append(
                self._block(
                    "formal_budget_drift",
                    "budget",
                    "Measurement budget drifted from the signed Formal window.",
                    retryable=False,
                    action_code="publish_new_formal_authorization",
                )
            )

        store_matches = (
            target.candidate_package_store_id
            == self.candidate_family_verifier.store_id
            == request.candidate_family.source_package_store_id
            and target.candidate_package_store_hash
            == self.candidate_family_verifier.store_hash
            == request.candidate_family.source_package_store_hash
        )
        if not store_matches:
            checks.append(
                self._block(
                    "formal_candidate_store_drift",
                    "candidate_family",
                    "Formal Profile, Family Manifest, and deployment Store do not match.",
                    retryable=False,
                    action_code="publish_new_formal_authorization",
                )
            )

        authority_snapshot: FormalOperatorAuthoritySnapshot | None = None
        try:
            authority_snapshot = authority_repository.resolve_formal_operator_authority(
                target,
                workload,
                request.candidate_family,
            )
        except (Conflict, NotFound, ValueError):
            checks.append(
                self._block(
                    "formal_operator_authority_unavailable",
                    "authority",
                    "The exact Formal Target, Stage 0, Baseline, Workload, and "
                    "Hotspot are unavailable.",
                    retryable=True,
                    action_code="refresh_formal_authority",
                )
            )
        else:
            if not self._authority_matches_inputs(
                authority_snapshot,
                request.candidate_family,
                target,
                workload,
            ):
                authority_snapshot = None
                checks.append(
                    self._block(
                        "formal_operator_authority_drift",
                        "authority",
                        "Repository Authority drifted from the frozen Candidate Family.",
                        retryable=False,
                        action_code="freeze_new_candidate_family",
                    )
                )
            else:
                checks.append(
                    self._pass(
                        "formal_operator_authority_resolved",
                        "authority",
                        "Formal Target, Stage 0, Baseline, Workload, and Hotspot were reread.",
                    )
                )

        verified_family = None
        if store_matches:
            try:
                verified_family = self.candidate_family_verifier.verify(
                    request.candidate_family
                )
            except (OSError, SourceArtifactError, ValueError):
                checks.append(
                    self._block(
                        "formal_candidate_family_invalid",
                        "candidate_family",
                        "Business Candidate packages could not be reread and verified.",
                        retryable=False,
                        action_code="freeze_new_candidate_family",
                    )
                )
            else:
                if (
                    verified_family.source_family_hash
                    != self.authorization.source_family_hash
                ):
                    verified_family = None
                    checks.append(
                        self._block(
                            "formal_candidate_family_drift",
                            "candidate_family",
                            "Verified source Family Hash differs from the signed Formal window.",
                            retryable=False,
                            action_code="publish_new_formal_authorization",
                        )
                    )
                elif len(verified_family.manifest.members) != measurement.budget.max_candidates:
                    verified_family = None
                    checks.append(
                        self._block(
                            "formal_candidate_count_drift",
                            "candidate_family",
                            "Verified Family size differs from the authorized Candidate budget.",
                            retryable=False,
                            action_code="freeze_new_candidate_family",
                        )
                    )
                else:
                    checks.append(
                        self._pass(
                            "formal_candidate_family_verified",
                            "candidate_family",
                            "All business Candidate packages and the source Family "
                            "Hash were verified.",
                        )
                    )

        resolved_candidates: tuple[ResolvedOperatorCandidate, ...] = ()
        candidate_input_set_hash: str | None = None
        if (
            budget_matches
            and store_matches
            and authority_snapshot is not None
            and verified_family is not None
        ):
            resolved_candidates = self._resolve_candidates(
                verified_family.manifest,
                target,
                authority_snapshot,
            )
            try:
                authority_repository.assert_operator_candidate_ids_available(
                    tuple(item.candidate_id for item in resolved_candidates),
                    allowed_round_id=allowed_round_id,
                )
            except Conflict:
                resolved_candidates = ()
                checks.append(
                    self._block(
                        "formal_candidate_id_conflict",
                        "candidate_family",
                        "Candidate identities are already bound to another workflow.",
                        retryable=False,
                        action_code="freeze_new_candidate_family",
                    )
                )
            else:
                candidate_input_set_hash = self._candidate_input_set_hash(
                    resolved_candidates
                )

        authority = authority_snapshot.authority if authority_snapshot is not None else None
        hotspot = authority_snapshot.hotspot if authority_snapshot is not None else None
        source_family_hash = (
            verified_family.source_family_hash
            if verified_family is not None and resolved_candidates
            else None
        )
        plan = FormalResolvedRoundPlan(
            target_profile=_profile_ref(request.target_profile),
            workload_profile=_profile_ref(request.workload_profile),
            measurement_profile=_profile_ref(request.measurement_profile),
            hotspot=hotspot,
            authority=authority,
            candidates=resolved_candidates,
            candidate_input_set_hash=candidate_input_set_hash,
            search_protocol_version=measurement.search_protocol_version,
            search_protocol_hash=measurement.search_protocol_hash,
            holdout_protocol_version=measurement.holdout_protocol_version,
            holdout_protocol_hash=measurement.holdout_protocol_hash,
            selection_rule_hash=measurement.selection_rule_hash,
            budget=measurement.budget,
            max_promoted=request.max_promoted,
            formal_authorization_hash=self.authorization.authorization_hash,
            authorized_host_id=self.authorization.host_id,
            authorized_resource_id=self.authorization.resource_id,
            authorization_window_starts_at=self.authorization.window_starts_at,
            authorization_window_expires_at=self.authorization.window_expires_at,
            candidate_family=request.candidate_family,
            source_family_id=request.candidate_family.family_id,
            source_family_hash=source_family_hash,
        )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Formal Operator Preview clock must be timezone-aware")
        expires_at = min(now + self.ttl, self.authorization.window_expires_at)
        if expires_at <= now:
            raise OperatorFormalAuthorizationInvalid(
                "Formal window expired while the Preview was being compiled"
            )
        blocked = any(item.status == "block" for item in checks)
        warning_codes = tuple(sorted(item.code for item in checks if item.status == "warn"))
        return FormalRoundPlanPreviewView(
            preview_id=uuid5(
                NAMESPACE_URL,
                f"hcuopt:formal-operator-preview:{request.idempotency_key}",
            ),
            preview_request_digest=formal_operator_preview_request_digest(request),
            resolved_plan_hash=formal_operator_resolved_plan_hash(plan),
            resolved_plan=plan,
            checks=tuple(checks),
            start_allowed=not blocked,
            required_ack_codes=warning_codes,
            expires_at=expires_at,
            service_identity=self.service_identity,
            formal_authorization=self.authorization,
            created_at=now,
        )

    def revalidate(
        self,
        preview: FormalRoundPlanPreviewView,
        authority_repository: FormalOperatorAuthorityRepository,
        *,
        allowed_round_id: UUID | None = None,
    ) -> FormalRoundPlanPreviewView:
        """Reread every Formal input before a future StartIntent consumes the Plan."""

        request = FormalRoundPlanPreviewRequest(
            name="Formal Operator Preview revalidation",
            target_profile=preview.resolved_plan.target_profile,
            workload_profile=preview.resolved_plan.workload_profile,
            measurement_profile=preview.resolved_plan.measurement_profile,
            candidate_family=preview.resolved_plan.candidate_family,
            max_promoted=preview.resolved_plan.max_promoted,
            idempotency_key=f"revalidate-formal-{preview.preview_id}",
            expected_service_identity=self.service_identity.model_dump(mode="json"),
            expected_formal_authorization_hash=(
                preview.resolved_plan.formal_authorization_hash
            ),
        )
        return self.compile(
            request,
            authority_repository,
            allowed_round_id=allowed_round_id,
        )

    @staticmethod
    def _authority_matches_inputs(
        snapshot: FormalOperatorAuthoritySnapshot,
        family: BusinessCandidateFamilyManifest,
        target: TargetOperatorProfileRefs,
        workload: WorkloadOperatorProfileRefs,
    ) -> bool:
        authority = snapshot.authority
        hotspot = snapshot.hotspot
        return (
            authority.target_snapshot_id,
            authority.stage0_run_id,
            authority.baseline_epoch_id,
            authority.baseline_source_hash,
            authority.hotspot_id,
            authority.replacement_point,
            authority.profiler_evidence_uri,
            authority.profiler_evidence_hash,
            authority.stage0_protocol_hash,
            authority.workload_id,
            authority.workload_hash,
            authority.configuration_hash,
            authority.adapter_profile,
            hotspot.hotspot_id,
            hotspot.replacement_point,
            hotspot.profiler_evidence_uri,
            hotspot.profiler_evidence_hash,
            hotspot.workload_hash,
        ) == (
            family.target_snapshot_id,
            family.stage0_run_id,
            family.baseline_epoch_id,
            family.baseline_source_hash,
            family.hotspot_id,
            family.replacement_point,
            family.profiler_evidence_uri,
            family.profiler_evidence_hash,
            target.required_stage0_protocol_hash,
            workload.workload_id,
            workload.workload_hash,
            workload.configuration_hash,
            target.adapter_profile,
            family.hotspot_id,
            family.replacement_point,
            family.profiler_evidence_uri,
            family.profiler_evidence_hash,
            workload.workload_hash,
        )

    @staticmethod
    def _resolve_candidates(
        family: BusinessCandidateFamilyManifest,
        target: TargetOperatorProfileRefs,
        authority_snapshot: FormalOperatorAuthoritySnapshot,
    ) -> tuple[ResolvedOperatorCandidate, ...]:
        authority = authority_snapshot.authority
        members = sorted(family.members, key=lambda item: str(item.candidate_id))
        return tuple(
            ResolvedOperatorCandidate(
                ordinal=ordinal,
                candidate_id=member.candidate_id,
                source_package_store_id=target.candidate_package_store_id,
                source_package_store_version=target.candidate_package_store_version,
                source_package_store_hash=target.candidate_package_store_hash,
                source_package_ref=member.source_package_ref,
                baseline_source_hash=authority.baseline_source_hash,
                hotspot_id=authority.hotspot_id,
                replacement_point=authority.replacement_point,
                candidate_kind="business",
                optimization_intent=member.optimization_intent,
            )
            for ordinal, member in enumerate(members)
        )

    @staticmethod
    def _candidate_input_set_hash(
        candidates: tuple[ResolvedOperatorCandidate, ...],
    ) -> str:
        return _sha256([item.model_dump(mode="json") for item in candidates])

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
