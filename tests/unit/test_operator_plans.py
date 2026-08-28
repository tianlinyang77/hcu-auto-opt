# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hcuopt.adapters.m2_candidate import (
    ScriptedCandidateIntake,
    candidate_source_package_hash,
)
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.api.app import create_app
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.operator_v1 import (
    OperatorProfileContent,
    OperatorProfileRef,
    ResolvedOperatorAuthority,
    RoundPlanPreviewRequest,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator import (
    OperatorPlanCompiler,
    build_operator_service_identity,
    build_scripted_operator_profile_catalog,
    publish_operator_profile,
)
from hcuopt.operator.errors import OperatorServiceIdentityMismatch
from hcuopt.operator.profiles import OperatorProfileCatalog

REPLACEMENT_POINT = "sglang.fixture.layer_norm"
OVERLAY_PATH = "sglang/fixture_kernel.py"
MOUNT_TARGET = "/opt/hcuopt/overlay/sglang/fixture_kernel.py"
PROFILER_URI = "fixture:///m2/profiler.json"
CORRECTNESS_URI = "fixture:///m2/correctness.json"
NOW = datetime(2026, 8, 28, tzinfo=timezone.utc)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _profile_ref(profile) -> OperatorProfileRef:  # type: ignore[no-untyped-def]
    return OperatorProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_kind=profile.profile_kind,
        profile_hash=profile.profile_hash,
    )


def _publish_package(
    root: Path,
    *,
    hotspot_id: UUID,
    candidate_id: UUID,
    baseline_source_hash: str,
    candidate_source_hash: str,
    profiler_evidence_hash: str,
) -> dict[str, str]:
    content = f"CANDIDATE = '{candidate_id}'\n".encode()
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    manifest = CandidateSourcePackageManifest(
        candidate_id=candidate_id,
        hotspot_id=hotspot_id,
        baseline_source_hash=baseline_source_hash,
        candidate_source_hash=candidate_source_hash,
        replacement_point=REPLACEMENT_POINT,
        candidate_kind="fixture",
        overlay_mount_target=MOUNT_TARGET,
        files=[{"path": OVERLAY_PATH, "content_hash": content_hash}],
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=profiler_evidence_hash,
        reviewed_by="operator-scripted-fixture",
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
        "candidate_source_hash": candidate_source_hash,
        "source_package_hash": package_hash,
        "manifest_hash": manifest_hash,
        "manifest_schema_version": "m1-candidate-source-v1",
    }


class AuthorityRepository:
    def __init__(self, authority: ResolvedOperatorAuthority) -> None:
        self.authority = authority

    def resolve_scripted_operator_authority(
        self,
        target: TargetOperatorProfileRefs,
        workload: WorkloadOperatorProfileRefs,
        hotspot,  # type: ignore[no-untyped-def]
    ) -> ResolvedOperatorAuthority:
        assert target.target_id == "m2-scripted-target"
        assert workload.workload_hash == self.authority.workload_hash
        assert hotspot.hotspot_id == self.authority.hotspot_id
        return self.authority


class PreviewRepository(AuthorityRepository):
    def __init__(self, authority: ResolvedOperatorAuthority) -> None:
        super().__init__(authority)
        self.preview = None

    def migrate(self) -> None:
        return None

    def get_operator_plan_preview_by_idempotency(self, _key):  # type: ignore[no-untyped-def]
        return self.preview

    def create_operator_plan_preview(self, _key, preview):  # type: ignore[no-untyped-def]
        self.preview = preview
        return preview


def _suite(tmp_path: Path):  # type: ignore[no-untyped-def]
    catalog = build_scripted_operator_profile_catalog()
    profiles = {item.profile_kind: item for item in catalog.list()}
    target = TargetOperatorProfileRefs.model_validate(profiles["target"].authority_refs)
    workload = WorkloadOperatorProfileRefs.model_validate(
        profiles["workload"].authority_refs
    )
    identity = build_operator_service_identity(
        source_commit="a" * 40,
        server_instance_id=UUID("00000000-0000-0000-0000-000000000087"),
        catalog=catalog,
    )
    hotspot_id = UUID("00000000-0000-0000-0000-000000000064")
    baseline_source_hash = _hash("baseline")
    profiler_hash = _hash("profiler")
    package_refs = [
        _publish_package(
            tmp_path,
            hotspot_id=hotspot_id,
            candidate_id=uuid5(hotspot_id, f"candidate-{ordinal}"),
            baseline_source_hash=baseline_source_hash,
            candidate_source_hash=_hash(f"candidate-{ordinal}"),
            profiler_evidence_hash=profiler_hash,
        )
        for ordinal in range(2)
    ]
    store = CandidateSourcePackageStore(
        tmp_path,
        profile="m2-scripted-v1",
        allowed_overlay_roots=("sglang",),
        approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
    )
    intake = ScriptedCandidateIntake(
        store,
        store_id=target.candidate_package_store_id,
        store_hash=target.candidate_package_store_hash,
    )
    authority = ResolvedOperatorAuthority(
        target_snapshot_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage0_run_id=UUID("00000000-0000-0000-0000-000000000002"),
        stage0_protocol_hash=target.required_stage0_protocol_hash,
        baseline_epoch_id=UUID("00000000-0000-0000-0000-000000000003"),
        baseline_source_hash=baseline_source_hash,
        hotspot_id=hotspot_id,
        replacement_point=REPLACEMENT_POINT,
        workload_id=workload.workload_id,
        workload_hash=workload.workload_hash,
        configuration_hash=workload.configuration_hash,
        image_digest=_hash("image"),
        adapter_profile=target.adapter_profile,
        profiler_evidence_uri=PROFILER_URI,
        profiler_evidence_hash=profiler_hash,
        synthetic=True,
    )
    request = RoundPlanPreviewRequest(
        name="Scripted Operator Preview",
        run_mode="scripted",
        target_profile=_profile_ref(profiles["target"]),
        workload_profile=_profile_ref(profiles["workload"]),
        measurement_profile=_profile_ref(profiles["measurement"]),
        hotspot={
            "source": "profiler",
            "hotspot_id": hotspot_id,
            "hotspot_intake_hash": _hash("hotspot"),
            "profiler_evidence_uri": PROFILER_URI,
            "profiler_evidence_hash": profiler_hash,
            "correctness_evidence_uri": CORRECTNESS_URI,
            "correctness_evidence_hash": _hash("correctness"),
            "replacement_point": REPLACEMENT_POINT,
            "workload_hash": workload.workload_hash,
            "shape": [1, 128],
            "dtype": "float16",
        },
        candidates=tuple(
            {
                "ordinal": ordinal,
                "source_package_ref": reference,
                "optimization_intent": f"exercise candidate {ordinal}",
            }
            for ordinal, reference in enumerate(package_refs)
        ),
        max_promoted=1,
        idempotency_key="operator-preview-scripted",
        expected_service_identity=identity.model_dump(mode="json"),
    )
    compiler = OperatorPlanCompiler(
        catalog,
        identity,
        candidate_intake=intake,
        clock=lambda: NOW,
    )
    return compiler, request, AuthorityRepository(authority)


def test_scripted_plan_compiler_resolves_a_startable_immutable_preview(
    tmp_path: Path,
) -> None:
    compiler, request, repository = _suite(tmp_path)

    first = compiler.compile(request, repository)
    replay = compiler.compile(request, repository)

    assert first == replay
    assert first.start_allowed is True
    assert first.synthetic is True
    assert first.resolved_plan.authority is not None
    assert len(first.resolved_plan.candidates) == 2
    assert first.resolved_plan.candidate_input_set_hash is not None
    assert first.resolved_plan.search_protocol_hash == _hash("m2-scripted-search-v1")
    assert first.resolved_plan.holdout_protocol_hash == _hash("m2-scripted-holdout-v1")
    assert first.resolved_plan.holdout_commitment_scheme == "sha256-nonce-v1"
    assert first.resolved_plan.selection_rule_hash == _hash("m2-scripted-selection-v1")
    assert all(item.status == "pass" for item in first.checks)


def test_plan_compiler_persists_safe_block_when_candidate_store_is_unavailable(
    tmp_path: Path,
) -> None:
    compiler, request, repository = _suite(tmp_path)
    blocked_compiler = OperatorPlanCompiler(
        compiler.profiles,
        compiler.service_identity,
        clock=lambda: NOW,
    )

    preview = blocked_compiler.compile(request, repository)

    assert preview.start_allowed is False
    assert preview.resolved_plan.authority is not None
    assert preview.resolved_plan.candidates == ()
    assert preview.resolved_plan.candidate_input_set_hash is None
    assert preview.checks[-1].code == "operator_candidate_package_invalid"

    invalid = preview.model_dump(mode="json")
    invalid["start_allowed"] = True
    invalid["checks"] = [
        item for item in invalid["checks"] if item["status"] != "block"
    ]
    with pytest.raises(ValidationError, match="fully resolved"):
        type(preview).model_validate(invalid)


def test_plan_request_rejects_extra_fields_duplicate_packages_and_stale_identity(
    tmp_path: Path,
) -> None:
    compiler, request, repository = _suite(tmp_path)
    payload = request.model_dump(mode="json")

    with pytest.raises(ValidationError, match="extra_forbidden"):
        RoundPlanPreviewRequest.model_validate({**payload, "unapproved_default": True})
    duplicate = request.candidates[1].model_copy(
        update={"source_package_ref": request.candidates[0].source_package_ref}
    )
    with pytest.raises(ValidationError, match="must be unique"):
        RoundPlanPreviewRequest.model_validate(
            {**payload, "candidates": [request.candidates[0], duplicate]}
        )
    stale = request.model_copy(
        update={
            "expected_service_identity": request.expected_service_identity.model_copy(
                update={"source_commit": "b" * 40}
            )
        }
    )
    with pytest.raises(OperatorServiceIdentityMismatch):
        compiler.compile(stale, repository)


def test_plan_compiler_requires_acknowledgement_for_deprecated_profile(
    tmp_path: Path,
) -> None:
    compiler, request, repository = _suite(tmp_path)
    deprecated = tuple(
        publish_operator_profile(
            OperatorProfileContent.model_validate(
                {
                    **profile.model_dump(mode="json", exclude={"profile_hash"}),
                    "state": "deprecated",
                }
            )
        )
        if profile.profile_kind == "measurement"
        else profile
        for profile in compiler.profiles.list()
    )
    catalog = OperatorProfileCatalog(deprecated)
    identity = build_operator_service_identity(
        source_commit="a" * 40,
        server_instance_id=UUID("00000000-0000-0000-0000-000000000087"),
        catalog=catalog,
    )
    profiles = {item.profile_kind: item for item in catalog.list()}
    deprecated_request = request.model_copy(
        update={
            "measurement_profile": _profile_ref(profiles["measurement"]),
            "expected_service_identity": identity,
        }
    )

    preview = OperatorPlanCompiler(
        catalog,
        identity,
        candidate_intake=compiler.candidate_intake,
        clock=lambda: NOW,
    ).compile(deprecated_request, repository)

    assert preview.start_allowed is True
    assert preview.required_ack_codes == ("operator_profile_deprecated",)
    assert any(
        item.code == "operator_profile_deprecated" and item.status == "warn"
        for item in preview.checks
    )


def test_operator_preview_api_exposes_identity_and_idempotent_plan_creation(
    tmp_path: Path,
) -> None:
    compiler, request, authority_repository = _suite(tmp_path)
    repository = PreviewRepository(authority_repository.authority)
    app = create_app(
        repository=repository,  # type: ignore[arg-type]
        operator_profiles=compiler.profiles,
        operator_plan_compiler=compiler,
    )

    with TestClient(app) as client:
        identity = client.get("/v1/operator/identity")
        created = client.post(
            "/v1/operator/round-plans:preview",
            json=request.model_dump(mode="json"),
        )
        replayed = client.post(
            "/v1/operator/round-plans:preview",
            json=request.model_dump(mode="json"),
        )
        changed = client.post(
            "/v1/operator/round-plans:preview",
            json={**request.model_dump(mode="json"), "name": "changed name"},
        )

    assert identity.status_code == 200
    assert identity.json()["profile_catalog_hash"] == compiler.profiles.catalog_hash
    assert created.status_code == 201
    assert created.json()["start_allowed"] is True
    assert replayed.status_code == 200
    assert replayed.json()["preview_id"] == created.json()["preview_id"]
    assert changed.status_code == 409
    assert changed.json()["code"] == "operator_plan_hash_mismatch"
