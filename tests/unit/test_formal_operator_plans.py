# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from hcuopt.adapters.business_candidate_family import (
    BusinessCandidateFamilyVerifier,
    business_candidate_source_family_hash,
)
from hcuopt.adapters.m2_candidate import candidate_source_package_hash
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileGrantVerifierRef,
    FormalProfileSetRef,
    FormalProfileWindowAuthorization,
    FormalProfileWindowAuthorizationContent,
    publish_formal_profile_window_authorization,
)
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.m2 import RoundBudget
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.contracts.m2_formal_operator_v1 import (
    FormalOperatorAuthoritySnapshot,
    FormalRoundPlanPreviewRequest,
)
from hcuopt.contracts.operator_v1 import (
    MeasurementOperatorProfileRefs,
    OperatorProfileContent,
    OperatorProfileDescriptor,
    OperatorProfileRef,
    ProfilerOperatorHotspotRef,
    ResolvedOperatorAuthority,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.domain.errors import Conflict, NotFound
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator import build_operator_service_identity, publish_operator_profile
from hcuopt.operator.errors import OperatorPlanHashMismatch
from hcuopt.operator.formal_plans import FormalOperatorPlanCompiler
from hcuopt.operator.profiles import OperatorProfileCatalog

NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
WINDOW_START = NOW - timedelta(minutes=5)
WINDOW_END = NOW + timedelta(hours=2)
TARGET_SNAPSHOT_ID = UUID("ffb9f94e-2035-5ff6-9b06-f1b5fb163196")
STAGE0_RUN_ID = UUID("dd50c381-75dd-5a64-9211-640c602dc817")
BASELINE_EPOCH_ID = UUID("1ab0480c-6f16-544c-a835-655599eaea6c")
HOTSPOT_ID = UUID("ec941d47-206a-5f18-8538-ae9c18c1e0ec")
FIRST_CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000011")
SECOND_CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000022")
REPLACEMENT_POINT = "sglang.srt.mem_cache.allocator.PagedTokenToKVPoolAllocator.free"
OVERLAY_PATH = "sglang/srt/mem_cache/allocator.py"
MOUNT_TARGET = "/opt/hcuopt/overlay/sglang/srt/mem_cache/allocator.py"
PROFILER_URI = "evidence:///m1/profiler/allocator-prefill.json"
CORRECTNESS_URI = "evidence:///m1/correctness/allocator-prefill.json"
STORE_ID = "nmz36-business-candidate-store-v1"


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _profile_ref(profile: OperatorProfileDescriptor) -> OperatorProfileRef:
    return OperatorProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_kind=profile.profile_kind,
        profile_hash=profile.profile_hash,
    )


def _publish_package(
    root: Path,
    *,
    candidate_id: UUID,
    candidate_source_hash: str,
    baseline_source_hash: str,
    content: bytes,
) -> dict:
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=HOTSPOT_ID,
        baseline_source_hash=baseline_source_hash,
        candidate_source_hash=candidate_source_hash,
        replacement_point=REPLACEMENT_POINT,
        candidate_kind="business",
        overlay_mount_target=MOUNT_TARGET,
        files=[{"path": OVERLAY_PATH, "content_hash": content_hash}],
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=_hash("profiler"),
        reviewed_by="formal-plan-test-reviewer",
        reviewed_at=NOW,
    )
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_hash = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    package_hash = candidate_source_package_hash(manifest_hash, manifest.files)
    digest = candidate_source_hash.removeprefix("sha256:")
    package_root = root / "sha256" / digest[:2] / digest[2:]
    source = package_root / "files" / OVERLAY_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    (package_root / "manifest.json").write_bytes(manifest_bytes)
    return {
        "candidate_id": candidate_id,
        "source_package_ref": {
            "candidate_source_hash": candidate_source_hash,
            "source_package_hash": package_hash,
            "manifest_hash": manifest_hash,
            "manifest_schema_version": manifest.schema_version,
        },
        "optimization_intent": f"evaluate Formal Candidate {candidate_id}",
    }


def _candidate_family(root: Path) -> BusinessCandidateFamilyManifest:
    baseline_source_hash = _hash("baseline-source")
    first = _publish_package(
        root,
        candidate_id=FIRST_CANDIDATE_ID,
        candidate_source_hash=_hash("candidate-one"),
        baseline_source_hash=baseline_source_hash,
        content=b"def free_v1(value):\n    return value\n",
    )
    second = _publish_package(
        root,
        candidate_id=SECOND_CANDIDATE_ID,
        candidate_source_hash=_hash("candidate-two"),
        baseline_source_hash=baseline_source_hash,
        content=b"def free_v2(value):\n    return value + 0\n",
    )
    return BusinessCandidateFamilyManifest(
        family_id="nmz36-allocator-prefill-family-v1",
        source_package_store_id=STORE_ID,
        source_package_store_hash=_hash("candidate-store"),
        target_snapshot_id=TARGET_SNAPSHOT_ID,
        stage0_run_id=STAGE0_RUN_ID,
        baseline_epoch_id=BASELINE_EPOCH_ID,
        baseline_source_hash=baseline_source_hash,
        hotspot_id=HOTSPOT_ID,
        replacement_point=REPLACEMENT_POINT,
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=_hash("profiler"),
        overlay_mount_target=MOUNT_TARGET,
        overlay_file_path=OVERLAY_PATH,
        members=(second, first),
        reviewed_by="formal-plan-test-reviewer",
        reviewed_at=NOW,
    )


def _profiles(
    budget: RoundBudget,
) -> tuple[OperatorProfileDescriptor, OperatorProfileDescriptor, OperatorProfileDescriptor]:
    shared = {
        "profile_version": 1,
        "state": "active",
        "allowed_run_modes": ["formal"],
        "synthetic": False,
        "created_at": NOW,
    }
    target = publish_operator_profile(
        OperatorProfileContent(
            **shared,
            profile_id="m2a-nmz36-target",
            profile_kind="target",
            display_name="nmz36 Formal target",
            summary="Exact non-synthetic target for Formal plan tests.",
            authority_refs=TargetOperatorProfileRefs(
                target_id="nmz36-sglang-0.5.12",
                target_spec_hash=_hash("target"),
                adapter_profile="nmz36-m2a-formal-v1",
                resource_policy_id="nmz36-hcu7-exclusive-v1",
                resource_policy_hash=_hash("resource-policy"),
                candidate_package_store_id=STORE_ID,
                candidate_package_store_version=1,
                candidate_package_store_hash=_hash("candidate-store"),
                required_stage0_protocol_hash=_hash("stage0-protocol"),
            ),
        )
    )
    workload = publish_operator_profile(
        OperatorProfileContent(
            **shared,
            profile_id="m2a-nmz36-allocator-prefill",
            profile_kind="workload",
            display_name="Allocator prefill Formal workload",
            summary="Frozen SGLang prefill workload for Formal plan tests.",
            authority_refs=WorkloadOperatorProfileRefs(
                workload_id="m1-qwen2.5-0.5b-prefill-4090-1-c1",
                workload_hash=_hash("workload"),
                configuration_hash=_hash("configuration"),
                dataset_uri="evidence:///datasets/formal-prompts.jsonl",
                dataset_hash=_hash("dataset"),
                model_uri="model:///qwen2.5-0.5b",
                model_hash=_hash("model"),
                hotspot_scope_id="allocator-prefill",
                hotspot_scope_hash=_hash("hotspot-scope"),
                baseline_selection_policy="latest_frozen_matching",
            ),
        )
    )
    measurement = publish_operator_profile(
        OperatorProfileContent(
            **shared,
            profile_id="m2a-nmz36-formal-standard",
            profile_kind="measurement",
            display_name="Formal measurement",
            summary="Bounded Formal Search and Holdout protocols.",
            authority_refs=MeasurementOperatorProfileRefs(
                search_protocol_version="m2-search-v1",
                search_protocol_hash=_hash("search-protocol"),
                holdout_protocol_version="m2-holdout-v1",
                holdout_protocol_hash=_hash("holdout-protocol"),
                selection_rule_hash=_hash("selection-rule"),
                budget=budget,
                conclusion_boundary="formal",
            ),
        )
    )
    return target, workload, measurement


def _authorization(
    profiles: tuple[OperatorProfileDescriptor, ...],
    *,
    source_family_hash: str,
    budget: RoundBudget,
):
    by_kind = {profile.profile_kind: profile for profile in profiles}
    content = FormalProfileWindowAuthorizationContent(
        authorization_id=UUID("00000000-0000-0000-0000-000000000099"),
        decision="authorized",
        readiness_audit_id="formal-plan-test-audit",
        readiness_audit_base_commit="a" * 40,
        readiness_manifest_hash=_hash("readiness-manifest"),
        readiness_report_hash=_hash("readiness-report"),
        profiles=FormalProfileSetRef(
            target_profile=_profile_ref(by_kind["target"]),
            workload_profile=_profile_ref(by_kind["workload"]),
            measurement_profile=_profile_ref(by_kind["measurement"]),
        ),
        source_family_hash=source_family_hash,
        budget=budget,
        host_id="nmz36",
        resource_id="hcu-7",
        window_starts_at=WINDOW_START,
        window_expires_at=WINDOW_END,
        authorized_by="formal-plan-test-owner",
        authorization_evidence_uri="evidence:///formal/window.json",
        authorization_evidence_hash=_hash("window-evidence"),
        issued_at=WINDOW_START - timedelta(minutes=5),
        verifier=FormalProfileGrantVerifierRef(
            verifier_id="formal-plan-test-verifier",
            verifier_version="1.0.0",
            verifier_hash=_hash("verifier"),
            signature_scheme="test-signature-v1",
            key_id="formal-plan-test-key",
        ),
    )
    return publish_formal_profile_window_authorization(content, signature="test-signature")


@dataclass
class _AuthorityRepository:
    snapshot: FormalOperatorAuthoritySnapshot | None
    conflict: bool = False
    resolve_calls: int = 0

    def resolve_formal_operator_authority(
        self,
        _target: TargetOperatorProfileRefs,
        _workload: WorkloadOperatorProfileRefs,
        _candidate_family: BusinessCandidateFamilyManifest,
    ) -> FormalOperatorAuthoritySnapshot:
        self.resolve_calls += 1
        if self.snapshot is None:
            raise NotFound("missing Formal Authority")
        return self.snapshot

    def assert_operator_candidate_ids_available(
        self,
        _candidate_ids: tuple[UUID, ...],
        *,
        allowed_round_id: UUID | None = None,
    ) -> None:
        del allowed_round_id
        if self.conflict:
            raise Conflict("Candidate already bound")


@dataclass
class _FamilyManifestStore:
    manifest: BusinessCandidateFamilyManifest | None
    requested_hashes: list[str]

    def read_manifest(self, *, source_family_hash: str) -> BusinessCandidateFamilyManifest:
        self.requested_hashes.append(source_family_hash)
        if self.manifest is None:
            raise NotFound("missing Candidate Family Manifest")
        return self.manifest


@dataclass(frozen=True)
class _Fixture:
    family: BusinessCandidateFamilyManifest
    profiles: tuple[OperatorProfileDescriptor, ...]
    authorization: FormalProfileWindowAuthorization
    compiler: FormalOperatorPlanCompiler
    request: FormalRoundPlanPreviewRequest
    repository: _AuthorityRepository
    manifest_store: _FamilyManifestStore


def _fixture(
    tmp_path: Path,
    *,
    authorization_family_hash: str | None = None,
    authorization_budget: RoundBudget | None = None,
) -> _Fixture:
    family = _candidate_family(tmp_path)
    profile_budget = RoundBudget(
        max_candidates=2,
        max_build_attempts=2,
        max_correctness_attempts=2,
        max_search_samples=100,
        max_holdout_samples=100,
        max_wall_seconds=1200,
        max_exclusive_lease_seconds=900,
    )
    profiles = _profiles(profile_budget)
    source_family_hash = business_candidate_source_family_hash(family)
    authorization = _authorization(
        profiles,
        source_family_hash=authorization_family_hash or source_family_hash,
        budget=authorization_budget or profile_budget,
    )
    catalog = OperatorProfileCatalog._from_verified_formal_profiles(
        profiles,
        authorization_hash=authorization.authorization_hash,
        window_starts_at=authorization.window_starts_at,
        window_expires_at=authorization.window_expires_at,
        clock=lambda: NOW,
    )
    service_identity = build_operator_service_identity(
        source_commit="b" * 40,
        server_instance_id=UUID("00000000-0000-0000-0000-000000000087"),
        catalog=catalog,
    )
    store = CandidateSourcePackageStore(
        tmp_path,
        profile="nmz36-m2a-business-v1",
        allowed_overlay_roots=("sglang",),
        approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
    )
    verifier = BusinessCandidateFamilyVerifier(
        store,
        store_id=STORE_ID,
        store_hash=_hash("candidate-store"),
    )
    manifest_store = _FamilyManifestStore(family, [])
    compiler = FormalOperatorPlanCompiler(
        catalog,
        service_identity,
        authorization=authorization,
        candidate_family_manifest_store=manifest_store,
        candidate_family_verifier=verifier,
        clock=lambda: NOW,
    )
    target, workload, _measurement = (
        profile.authority_refs for profile in profiles
    )
    assert isinstance(target, TargetOperatorProfileRefs)
    assert isinstance(workload, WorkloadOperatorProfileRefs)
    authority = ResolvedOperatorAuthority(
        target_snapshot_id=TARGET_SNAPSHOT_ID,
        stage0_run_id=STAGE0_RUN_ID,
        stage0_protocol_hash=target.required_stage0_protocol_hash,
        baseline_epoch_id=BASELINE_EPOCH_ID,
        baseline_source_hash=family.baseline_source_hash,
        hotspot_id=HOTSPOT_ID,
        replacement_point=REPLACEMENT_POINT,
        workload_id=workload.workload_id,
        workload_hash=workload.workload_hash,
        configuration_hash=workload.configuration_hash,
        image_digest=_hash("image"),
        adapter_profile=target.adapter_profile,
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=_hash("profiler"),
        synthetic=False,
    )
    hotspot = ProfilerOperatorHotspotRef(
        source="profiler",
        hotspot_id=HOTSPOT_ID,
        hotspot_intake_hash=_hash("hotspot-intake"),
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=_hash("profiler"),
        correctness_evidence_uri=CORRECTNESS_URI,
        correctness_evidence_hash=_hash("correctness"),
        replacement_point=REPLACEMENT_POINT,
        workload_hash=workload.workload_hash,
        shape=(1, 4096),
        dtype="bfloat16",
    )
    repository = _AuthorityRepository(
        FormalOperatorAuthoritySnapshot(authority=authority, hotspot=hotspot)
    )
    request = FormalRoundPlanPreviewRequest(
        name="Formal allocator preview",
        target_profile=_profile_ref(profiles[0]),
        workload_profile=_profile_ref(profiles[1]),
        measurement_profile=_profile_ref(profiles[2]),
        max_promoted=2,
        idempotency_key="formal-plan-preview-test-v1",
        expected_service_identity=service_identity.model_dump(mode="json"),
        expected_formal_authorization_hash=authorization.authorization_hash,
    )
    return _Fixture(
        family=family,
        profiles=profiles,
        authorization=authorization,
        compiler=compiler,
        request=request,
        repository=repository,
        manifest_store=manifest_store,
    )


def _check_codes(preview) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {item.code: item.status for item in preview.checks}


def test_formal_compiler_freezes_verified_family_deterministically(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    first = fixture.compiler.compile(fixture.request, fixture.repository)
    second = fixture.compiler.compile(fixture.request, fixture.repository)
    revalidated = fixture.compiler.revalidate(first, fixture.repository)

    assert first.start_allowed is False
    assert first.synthetic is False
    assert first.automatic_release_allowed is False
    assert first.preview_id == second.preview_id
    assert first.resolved_plan_hash == second.resolved_plan_hash
    assert revalidated.resolved_plan_hash == first.resolved_plan_hash
    assert "candidate_family" not in fixture.request.model_dump(mode="json")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FormalRoundPlanPreviewRequest.model_validate(
            {
                **fixture.request.model_dump(mode="json"),
                "candidate_family": fixture.family.model_dump(mode="json"),
            }
        )
    assert first.expires_at == NOW + timedelta(minutes=30)
    assert first.resolved_plan.source_family_hash == business_candidate_source_family_hash(
        fixture.family
    )
    assert first.resolved_plan.formal_authorization_hash == (
        fixture.authorization.authorization_hash
    )
    assert first.resolved_plan.authorized_host_id == "nmz36"
    assert first.resolved_plan.authorized_resource_id == "hcu-7"
    assert [item.ordinal for item in first.resolved_plan.candidates] == [0, 1]
    assert [item.candidate_id for item in first.resolved_plan.candidates] == [
        FIRST_CANDIDATE_ID,
        SECOND_CANDIDATE_ID,
    ]
    assert fixture.repository.resolve_calls == 3
    assert fixture.manifest_store.requested_hashes == [
        fixture.authorization.source_family_hash,
        fixture.authorization.source_family_hash,
        fixture.authorization.source_family_hash,
    ]
    statuses = _check_codes(first)
    assert statuses.pop("formal_start_authority_not_bound") == "block"
    assert all(status == "pass" for status in statuses.values())


def test_formal_compiler_blocks_family_authority_and_budget_drift(tmp_path: Path) -> None:
    family_drift = _fixture(
        tmp_path / "family",
        authorization_family_hash=_hash("different-family"),
    )
    family_preview = family_drift.compiler.compile(
        family_drift.request, family_drift.repository
    )
    assert family_preview.start_allowed is False
    assert family_preview.resolved_plan.candidates == ()
    assert _check_codes(family_preview)["formal_candidate_family_drift"] == "block"

    budget = RoundBudget(
        max_candidates=2,
        max_build_attempts=2,
        max_correctness_attempts=2,
        max_search_samples=100,
        max_holdout_samples=100,
        max_wall_seconds=1201,
        max_exclusive_lease_seconds=900,
    )
    budget_drift = _fixture(tmp_path / "budget", authorization_budget=budget)
    budget_preview = budget_drift.compiler.compile(
        budget_drift.request, budget_drift.repository
    )
    assert budget_preview.start_allowed is False
    assert budget_preview.resolved_plan.candidates == ()
    assert _check_codes(budget_preview)["formal_budget_drift"] == "block"

    missing = _fixture(tmp_path / "authority")
    missing.repository.snapshot = None
    missing_preview = missing.compiler.compile(missing.request, missing.repository)
    assert missing_preview.start_allowed is False
    assert missing_preview.resolved_plan.authority is None
    assert (
        _check_codes(missing_preview)["formal_operator_authority_unavailable"] == "block"
    )

    missing_family = _fixture(tmp_path / "family-store")
    missing_family.manifest_store.manifest = None
    missing_family_preview = missing_family.compiler.compile(
        missing_family.request, missing_family.repository
    )
    assert missing_family_preview.start_allowed is False
    assert missing_family_preview.resolved_plan.candidate_family is None
    assert missing_family_preview.resolved_plan.authority is None
    assert (
        _check_codes(missing_family_preview)["formal_candidate_family_unavailable"]
        == "block"
    )


def test_formal_compiler_blocks_repository_and_candidate_identity_drift(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "authority")
    assert fixture.repository.snapshot is not None
    fixture.repository.snapshot = fixture.repository.snapshot.model_copy(
        update={
            "authority": fixture.repository.snapshot.authority.model_copy(
                update={"baseline_source_hash": _hash("wrong-baseline")}
            )
        }
    )
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    assert preview.start_allowed is False
    assert _check_codes(preview)["formal_operator_authority_drift"] == "block"

    conflict = _fixture(tmp_path / "identity")
    conflict.repository.conflict = True
    preview = conflict.compiler.compile(conflict.request, conflict.repository)
    assert preview.start_allowed is False
    assert preview.resolved_plan.candidates == ()
    assert _check_codes(preview)["formal_candidate_id_conflict"] == "block"


def test_formal_compiler_rejects_profile_or_window_identity_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    changed_profile = fixture.request.model_copy(
        update={
            "target_profile": fixture.request.target_profile.model_copy(
                update={"profile_hash": _hash("changed-profile")}
            )
        }
    )
    with pytest.raises(OperatorPlanHashMismatch, match="Profile Hash changed"):
        fixture.compiler.compile(changed_profile, fixture.repository)

    changed_window = fixture.request.model_copy(
        update={"expected_formal_authorization_hash": _hash("changed-window")}
    )
    with pytest.raises(Conflict, match="authorization changed"):
        fixture.compiler.compile(changed_window, fixture.repository)
