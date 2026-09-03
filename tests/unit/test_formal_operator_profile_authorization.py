# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileGrantVerifierRef,
    FormalProfileSetRef,
    FormalProfileWindowAuthorization,
    FormalProfileWindowAuthorizationContent,
    formal_profile_window_authorization_hash,
    publish_formal_profile_window_authorization,
)
from hcuopt.contracts.formal_readiness_v1 import (
    FormalReadinessEvidenceResult,
    FormalReadinessGateResult,
    FormalReadinessManifest,
    FormalReadinessReport,
)
from hcuopt.contracts.m2 import CandidateSourcePackageRef
from hcuopt.contracts.operator_v1 import (
    MeasurementOperatorProfileRefs,
    OperatorProfileContent,
    OperatorProfileDescriptor,
    OperatorProfileRef,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.domain.enums import SearchRoundRunMode
from hcuopt.operator import (
    OperatorProfileCatalog,
    build_formal_operator_profile_catalog,
    build_scripted_operator_profile_catalog,
    publish_operator_profile,
)
from hcuopt.operator.errors import (
    OperatorFormalAuthorizationInvalid,
    OperatorFormalAuthorizationNotActive,
)
from hcuopt.operator.readiness import (
    formal_readiness_manifest_hash,
    formal_readiness_report_hash,
    load_formal_readiness_manifest,
)

FIXED_TIME = datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc)
ISSUED_AT = FIXED_TIME - timedelta(minutes=30)
WINDOW_STARTS_AT = FIXED_TIME - timedelta(minutes=10)
WINDOW_EXPIRES_AT = FIXED_TIME + timedelta(hours=2)
SHA = "sha256:" + "a" * 64


def _package_ref(seed: str) -> CandidateSourcePackageRef:
    return CandidateSourcePackageRef(
        candidate_source_hash="sha256:" + seed * 64,
        source_package_hash="sha256:" + chr(ord(seed) + 1) * 64,
        manifest_hash="sha256:" + chr(ord(seed) + 2) * 64,
        manifest_schema_version="m1-candidate-source-v1",
    )


def _formal_profiles(manifest: FormalReadinessManifest) -> tuple[OperatorProfileDescriptor, ...]:
    draft = manifest.profile_draft
    scripted = {
        profile.profile_kind: profile
        for profile in build_scripted_operator_profile_catalog().list()
    }
    contents = []
    for kind, profile_id in (
        ("target", draft.target_profile_id),
        ("workload", draft.workload_profile_id),
        ("measurement", draft.measurement_profile_id),
    ):
        source = scripted[kind]
        refs = source.authority_refs
        if isinstance(refs, TargetOperatorProfileRefs):
            refs = refs.model_copy(
                update={
                    "target_id": draft.target_id,
                    "adapter_profile": draft.adapter_profile,
                    "required_stage0_protocol_hash": draft.stage0_protocol_hash,
                }
            )
        elif isinstance(refs, WorkloadOperatorProfileRefs):
            refs = refs.model_copy(update={"workload_id": draft.workload_id})
        elif isinstance(refs, MeasurementOperatorProfileRefs):
            refs = refs.model_copy(
                update={"budget": draft.budget, "conclusion_boundary": "formal"}
            )
        contents.append(
            OperatorProfileContent.model_validate(
                {
                    **source.model_dump(mode="json", exclude={"profile_hash"}),
                    "profile_id": profile_id,
                    "profile_version": draft.profile_version,
                    "allowed_run_modes": ["formal"],
                    "authority_refs": refs,
                    "synthetic": False,
                }
            )
        )
    return tuple(publish_operator_profile(content) for content in contents)


def _ready_manifest() -> FormalReadinessManifest:
    source = load_formal_readiness_manifest(
        Path(__file__).resolve().parents[2]
        / "config"
        / "m2"
        / "nmz36-formal-readiness-v1.yaml"
    )
    raw = source.model_dump(mode="json")
    raw["profile_draft"].update(
        {
            "registration_state": "implementation_ready_unregistered",
            "candidate_family_state": "frozen",
            "budget_state": "accepted",
            "candidate_packages": [
                _package_ref("1").model_dump(mode="json"),
                _package_ref("4").model_dump(mode="json"),
            ],
            "candidate_family_hash": SHA,
        }
    )
    for gate in raw["gates"]:
        gate["status"] = "hold" if gate["code"] == "owner_window_authorization" else "pass"
    for review in raw["reviews"]:
        review["decision"] = "accepted_for_formal_window"
        review["evidence"] = {
            "path": f"docs/review-{review['owner'].lower()}.md",
            "sha256": SHA,
            "digest_mode": "text_lf",
            "evidence_type": "test_formal_acceptance",
        }
    return FormalReadinessManifest.model_validate(raw)


def _ready_report(manifest: FormalReadinessManifest) -> FormalReadinessReport:
    results = []
    for gate in manifest.gates:
        evidence = tuple(
            FormalReadinessEvidenceResult(
                **reference.model_dump(mode="json"),
                status="verified",
            )
            for reference in gate.evidence
        )
        results.append(
            FormalReadinessGateResult(
                code=gate.code,
                owner=gate.owner,
                declared_status=gate.status,
                effective_status=gate.status,
                evidence_status="verified",
                summary=gate.summary,
                required_action=gate.required_action,
                evidence=evidence,
            )
        )
    return FormalReadinessReport(
        audit_id=manifest.audit_id,
        audit_base_commit=manifest.audit_base_commit,
        manifest_hash=formal_readiness_manifest_hash(manifest),
        generated_at=ISSUED_AT - timedelta(minutes=10),
        decision="ready_for_window_authorization",
        gate_results=tuple(results),
        reviews=manifest.reviews,
        blocker_codes=(),
        verified_evidence_count=32,
    )


def _profile_set(profiles: tuple[OperatorProfileDescriptor, ...]) -> FormalProfileSetRef:
    by_kind = {profile.profile_kind: profile for profile in profiles}

    def reference(kind: str) -> OperatorProfileRef:
        profile = by_kind[kind]
        return OperatorProfileRef(
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            profile_kind=profile.profile_kind,
            profile_hash=profile.profile_hash,
        )

    return FormalProfileSetRef(
        target_profile=reference("target"),
        workload_profile=reference("workload"),
        measurement_profile=reference("measurement"),
    )


@dataclass(frozen=True)
class _TestVerifier:
    verifier_ref: FormalProfileGrantVerifierRef
    reject: bool = False

    def verify_signature(self, *, authorization_hash: str, signature: str) -> bool:
        return not self.reject and signature == f"test-signature:{authorization_hash}"


def _authorization_fixture(
    *,
    decision: Literal["authorized", "rejected"] = "authorized",
) -> tuple[
    tuple[OperatorProfileDescriptor, ...],
    FormalReadinessManifest,
    FormalReadinessReport,
    FormalProfileWindowAuthorization,
    _TestVerifier,
]:
    manifest = _ready_manifest()
    report = _ready_report(manifest)
    profiles = _formal_profiles(manifest)
    verifier_ref = FormalProfileGrantVerifierRef(
        verifier_id="deployment-formal-profile-verifier",
        verifier_version="1.0.0",
        verifier_hash="sha256:" + "b" * 64,
        signature_scheme="test-signature-v1",
        key_id="test-owner-key",
    )
    content = FormalProfileWindowAuthorizationContent(
        authorization_id=uuid4(),
        decision=decision,
        readiness_audit_id=manifest.audit_id,
        readiness_audit_base_commit=manifest.audit_base_commit,
        readiness_manifest_hash=formal_readiness_manifest_hash(manifest),
        readiness_report_hash=formal_readiness_report_hash(report),
        profiles=_profile_set(profiles),
        source_family_hash=SHA,
        budget=manifest.profile_draft.budget,
        host_id="nmz36",
        resource_id=manifest.profile_draft.resource_id,
        window_starts_at=WINDOW_STARTS_AT,
        window_expires_at=WINDOW_EXPIRES_AT,
        authorized_by="project-owner",
        authorization_evidence_uri="evidence://formal-window/test",
        authorization_evidence_hash="sha256:" + "c" * 64,
        issued_at=ISSUED_AT,
        verifier=verifier_ref,
    )
    authorization_hash = formal_profile_window_authorization_hash(content)
    authorization = publish_formal_profile_window_authorization(
        content,
        signature=f"test-signature:{authorization_hash}",
    )
    return profiles, manifest, report, authorization, _TestVerifier(verifier_ref)


def test_signed_exact_window_registers_real_profiles_and_expires_at_use_time() -> None:
    profiles, manifest, report, authorization, verifier = _authorization_fixture()
    observed = {"now": FIXED_TIME}
    catalog = build_formal_operator_profile_catalog(
        profiles,
        authorization=authorization,
        readiness_manifest=manifest,
        readiness_report=report,
        verifier=verifier,
        clock=lambda: observed["now"],
    )
    reference = authorization.profiles.target_profile

    assert catalog.formal_authorization_hash == authorization.authorization_hash
    assert catalog.require(reference, SearchRoundRunMode.FORMAL).synthetic is False

    observed["now"] = WINDOW_EXPIRES_AT
    with pytest.raises(OperatorFormalAuthorizationNotActive, match="not active"):
        catalog.require(reference, SearchRoundRunMode.FORMAL)


def test_real_profiles_still_reject_missing_authorization_or_verifier() -> None:
    profiles, manifest, report, authorization, _verifier = _authorization_fixture()

    with pytest.raises(ValueError, match="separate authorization"):
        OperatorProfileCatalog(profiles)
    with pytest.raises(OperatorFormalAuthorizationInvalid, match="deployment-owned"):
        build_formal_operator_profile_catalog(
            profiles,
            authorization=authorization,
            readiness_manifest=manifest,
            readiness_report=report,
            verifier=None,  # type: ignore[arg-type]
            clock=lambda: FIXED_TIME,
        )


def test_rejected_or_invalidly_signed_window_fails_closed() -> None:
    profiles, manifest, report, rejected, verifier = _authorization_fixture(
        decision="rejected"
    )
    with pytest.raises(OperatorFormalAuthorizationInvalid, match="did not authorize"):
        build_formal_operator_profile_catalog(
            profiles,
            authorization=rejected,
            readiness_manifest=manifest,
            readiness_report=report,
            verifier=verifier,
            clock=lambda: FIXED_TIME,
        )

    profiles, manifest, report, authorization, verifier = _authorization_fixture()
    tampered_signature = authorization.model_copy(update={"signature": "invalid"})
    with pytest.raises(OperatorFormalAuthorizationInvalid, match="signature was rejected"):
        build_formal_operator_profile_catalog(
            profiles,
            authorization=tampered_signature,
            readiness_manifest=manifest,
            readiness_report=report,
            verifier=verifier,
            clock=lambda: FIXED_TIME,
        )


def test_tampered_expired_or_drifted_authority_fails_closed() -> None:
    profiles, manifest, report, authorization, verifier = _authorization_fixture()
    tampered = authorization.model_copy(
        update={"authorization_hash": "sha256:" + "0" * 64}
    )
    with pytest.raises(OperatorFormalAuthorizationInvalid, match="Hash drifted"):
        build_formal_operator_profile_catalog(
            profiles,
            authorization=tampered,
            readiness_manifest=manifest,
            readiness_report=report,
            verifier=verifier,
            clock=lambda: FIXED_TIME,
        )

    with pytest.raises(OperatorFormalAuthorizationInvalid, match="window is not active"):
        build_formal_operator_profile_catalog(
            profiles,
            authorization=authorization,
            readiness_manifest=manifest,
            readiness_report=report,
            verifier=verifier,
            clock=lambda: WINDOW_EXPIRES_AT,
        )

    changed = profiles[0].model_copy(update={"profile_hash": "sha256:" + "9" * 64})
    with pytest.raises(OperatorFormalAuthorizationInvalid, match="Profile content drifted"):
        build_formal_operator_profile_catalog(
            (changed, *profiles[1:]),
            authorization=authorization,
            readiness_manifest=manifest,
            readiness_report=report,
            verifier=verifier,
            clock=lambda: FIXED_TIME,
        )


def test_readiness_or_budget_drift_cannot_reuse_a_signed_window() -> None:
    profiles, manifest, report, authorization, verifier = _authorization_fixture()
    held_report = report.model_copy(
        update={"decision": "hold", "blocker_codes": ("formal_plan_compiler",)}
    )
    with pytest.raises(OperatorFormalAuthorizationInvalid, match="Manifest or Report"):
        build_formal_operator_profile_catalog(
            profiles,
            authorization=authorization,
            readiness_manifest=manifest,
            readiness_report=held_report,
            verifier=verifier,
            clock=lambda: FIXED_TIME,
        )

    changed_budget = authorization.budget.model_copy(
        update={"max_wall_seconds": authorization.budget.max_wall_seconds + 1}
    )
    changed_content = FormalProfileWindowAuthorizationContent.model_validate(
        {
            **authorization.model_dump(
                mode="json", exclude={"authorization_hash", "signature"}
            ),
            "budget": changed_budget,
        }
    )
    changed_hash = formal_profile_window_authorization_hash(changed_content)
    changed_authorization = publish_formal_profile_window_authorization(
        changed_content,
        signature=f"test-signature:{changed_hash}",
    )
    with pytest.raises(OperatorFormalAuthorizationInvalid, match="Budget"):
        build_formal_operator_profile_catalog(
            profiles,
            authorization=changed_authorization,
            readiness_manifest=manifest,
            readiness_report=report,
            verifier=verifier,
            clock=lambda: FIXED_TIME,
        )
