# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileGrantVerifierRef,
    FormalProfileSetRef,
    FormalProfileWindowAuthorization,
    FormalProfileWindowAuthorizationContent,
    formal_profile_window_authorization_hash,
)
from hcuopt.contracts.formal_readiness_v1 import (
    FormalReadinessManifest,
    FormalReadinessReport,
)
from hcuopt.contracts.operator_v1 import (
    MeasurementOperatorProfileRefs,
    OperatorProfileDescriptor,
    OperatorProfileRef,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.domain.enums import SearchRoundRunMode
from hcuopt.operator.errors import OperatorFormalAuthorizationInvalid
from hcuopt.operator.profiles import OperatorProfileCatalog
from hcuopt.operator.readiness import (
    formal_readiness_manifest_hash,
    formal_readiness_report_hash,
)


class DeploymentFormalProfileGrantVerifier(Protocol):
    """Deployment-owned signature boundary; implementations must fail closed."""

    @property
    def verifier_ref(self) -> FormalProfileGrantVerifierRef: ...

    def verify_signature(self, *, authorization_hash: str, signature: str) -> bool: ...


def _authorization_content(
    authorization: FormalProfileWindowAuthorization,
) -> FormalProfileWindowAuthorizationContent:
    return FormalProfileWindowAuthorizationContent.model_validate(
        authorization.model_dump(
            mode="json",
            exclude={"authorization_hash", "signature"},
        )
    )


def _profile_ref(profile: OperatorProfileDescriptor) -> OperatorProfileRef:
    return OperatorProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_kind=profile.profile_kind,
        profile_hash=profile.profile_hash,
    )


def _formal_profile_set(
    profiles: tuple[OperatorProfileDescriptor, ...],
) -> FormalProfileSetRef:
    if len(profiles) != 3:
        raise OperatorFormalAuthorizationInvalid(
            "Formal registration requires exactly one target, workload, and measurement Profile"
        )
    by_kind = {profile.profile_kind: profile for profile in profiles}
    if set(by_kind) != {"target", "workload", "measurement"}:
        raise OperatorFormalAuthorizationInvalid(
            "Formal registration requires exactly one Profile of every canonical kind"
        )
    if any(profile.synthetic for profile in profiles):
        raise OperatorFormalAuthorizationInvalid(
            "Formal registration cannot contain synthetic Operator Profiles"
        )
    if any(
        profile.state != "active"
        or profile.allowed_run_modes != (SearchRoundRunMode.FORMAL,)
        for profile in profiles
    ):
        raise OperatorFormalAuthorizationInvalid(
            "Formal registration requires active Profiles that allow only Formal mode"
        )
    return FormalProfileSetRef(
        target_profile=_profile_ref(by_kind["target"]),
        workload_profile=_profile_ref(by_kind["workload"]),
        measurement_profile=_profile_ref(by_kind["measurement"]),
    )


def build_formal_operator_profile_catalog(
    profiles: tuple[OperatorProfileDescriptor, ...],
    *,
    authorization: FormalProfileWindowAuthorization,
    readiness_manifest: FormalReadinessManifest,
    readiness_report: FormalReadinessReport,
    verifier: DeploymentFormalProfileGrantVerifier,
    clock: Callable[[], datetime] | None = None,
) -> OperatorProfileCatalog:
    """Register one exact Real Profile set only inside its signed Formal window."""

    current_time = (clock or (lambda: datetime.now(timezone.utc)))()
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise OperatorFormalAuthorizationInvalid(
            "Formal authorization clock must be timezone-aware"
        )

    content = _authorization_content(authorization)
    actual_authorization_hash = formal_profile_window_authorization_hash(content)
    if actual_authorization_hash != authorization.authorization_hash:
        raise OperatorFormalAuthorizationInvalid("Formal authorization Hash drifted")
    if verifier is None:
        raise OperatorFormalAuthorizationInvalid(
            "Formal registration requires a deployment-owned signature verifier"
        )
    if verifier.verifier_ref != authorization.verifier:
        raise OperatorFormalAuthorizationInvalid(
            "Formal authorization verifier identity drifted"
        )
    try:
        signature_valid = verifier.verify_signature(
            authorization_hash=authorization.authorization_hash,
            signature=authorization.signature,
        )
    except Exception as error:
        raise OperatorFormalAuthorizationInvalid(
            "Formal authorization signature verifier failed closed"
        ) from error
    if signature_valid is not True:
        raise OperatorFormalAuthorizationInvalid(
            "Formal authorization signature was rejected"
        )
    if authorization.decision != "authorized":
        raise OperatorFormalAuthorizationInvalid(
            "Project owner did not authorize this Formal window"
        )
    if not authorization.window_starts_at <= current_time < authorization.window_expires_at:
        raise OperatorFormalAuthorizationInvalid(
            "Formal authorization window is not active"
        )

    manifest_hash = formal_readiness_manifest_hash(readiness_manifest)
    report_hash = formal_readiness_report_hash(readiness_report)
    expected_readiness = (
        readiness_manifest.audit_id,
        readiness_manifest.audit_base_commit,
        manifest_hash,
        report_hash,
    )
    authorized_readiness = (
        authorization.readiness_audit_id,
        authorization.readiness_audit_base_commit,
        authorization.readiness_manifest_hash,
        authorization.readiness_report_hash,
    )
    if expected_readiness != authorized_readiness:
        raise OperatorFormalAuthorizationInvalid(
            "Formal readiness Manifest or Report authority drifted"
        )
    if (
        readiness_report.audit_id != readiness_manifest.audit_id
        or readiness_report.audit_base_commit != readiness_manifest.audit_base_commit
        or readiness_report.manifest_hash != manifest_hash
        or readiness_report.reviews != readiness_manifest.reviews
    ):
        raise OperatorFormalAuthorizationInvalid(
            "Formal readiness Report does not match its Manifest"
        )
    if readiness_report.generated_at > authorization.issued_at:
        raise OperatorFormalAuthorizationInvalid(
            "Formal authorization predates its readiness Report"
        )
    if (
        readiness_report.decision != "ready_for_window_authorization"
        or readiness_report.blocker_codes
        or any(
            review.decision != "accepted_for_formal_window"
            for review in readiness_report.reviews
        )
        or any(review.evidence is None for review in readiness_report.reviews)
    ):
        raise OperatorFormalAuthorizationInvalid(
            "Formal readiness and A/B/C/D reviews do not authorize a window request"
        )
    owner_gate = None
    for gate in readiness_report.gate_results:
        if gate.evidence_status != "verified":
            raise OperatorFormalAuthorizationInvalid(
                "Formal readiness contains unverified gate evidence"
            )
        if gate.code == "owner_window_authorization":
            owner_gate = gate
        elif gate.effective_status != "pass":
            raise OperatorFormalAuthorizationInvalid(
                "Formal readiness contains an implementation gate that did not pass"
            )
    if owner_gate is None or owner_gate.effective_status != "hold":
        raise OperatorFormalAuthorizationInvalid(
            "Formal readiness must leave the separate owner window decision on hold"
        )

    actual_profiles = _formal_profile_set(profiles)
    if actual_profiles != authorization.profiles:
        raise OperatorFormalAuthorizationInvalid(
            "Formal Operator Profile content drifted from the authorized Profile Set"
        )
    draft = readiness_manifest.profile_draft
    if (
        draft.registration_state != "implementation_ready_unregistered"
        or draft.candidate_family_state != "frozen"
        or draft.budget_state != "accepted"
        or draft.candidate_family_hash != authorization.source_family_hash
        or draft.budget != authorization.budget
        or draft.resource_id != authorization.resource_id
    ):
        raise OperatorFormalAuthorizationInvalid(
            "Formal Profile draft, Candidate Family, Budget, or Resource is not ready"
        )
    if (
        actual_profiles.target_profile.profile_id != draft.target_profile_id
        or actual_profiles.target_profile.profile_version != draft.profile_version
        or actual_profiles.workload_profile.profile_id != draft.workload_profile_id
        or actual_profiles.workload_profile.profile_version != draft.profile_version
        or actual_profiles.measurement_profile.profile_id != draft.measurement_profile_id
        or actual_profiles.measurement_profile.profile_version != draft.profile_version
    ):
        raise OperatorFormalAuthorizationInvalid(
            "Formal Profile identities drifted from the readiness draft"
        )

    by_kind = {profile.profile_kind: profile for profile in profiles}
    target_refs = by_kind["target"].authority_refs
    workload_refs = by_kind["workload"].authority_refs
    measurement_refs = by_kind["measurement"].authority_refs
    if not isinstance(target_refs, TargetOperatorProfileRefs):
        raise OperatorFormalAuthorizationInvalid("Formal target Profile authority is invalid")
    if not isinstance(workload_refs, WorkloadOperatorProfileRefs):
        raise OperatorFormalAuthorizationInvalid("Formal workload Profile authority is invalid")
    if not isinstance(measurement_refs, MeasurementOperatorProfileRefs):
        raise OperatorFormalAuthorizationInvalid(
            "Formal measurement Profile authority is invalid"
        )
    if (
        target_refs.target_id != draft.target_id
        or target_refs.adapter_profile != draft.adapter_profile
        or target_refs.required_stage0_protocol_hash != draft.stage0_protocol_hash
        or workload_refs.workload_id != draft.workload_id
        or measurement_refs.budget != authorization.budget
        or measurement_refs.conclusion_boundary != "formal"
    ):
        raise OperatorFormalAuthorizationInvalid(
            "Formal Profile Authority refs drifted from readiness or authorization"
        )

    active_clock = clock or (lambda: datetime.now(timezone.utc))
    return OperatorProfileCatalog._from_verified_formal_profiles(
        profiles,
        authorization_hash=authorization.authorization_hash,
        window_starts_at=authorization.window_starts_at,
        window_expires_at=authorization.window_expires_at,
        clock=active_clock,
    )
