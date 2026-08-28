# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
from datetime import datetime, timedelta, timezone
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
from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.contracts.operator_v1 import (
    OperatorProfileContent,
    OperatorProfileRef,
    OperatorRoundStartRequest,
    OperatorStartCandidateMember,
    OperatorStartIntentView,
    PreflightCheckResult,
    ResolvedOperatorAuthority,
    RoundPlanPreviewRequest,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator import (
    HmacScriptedPlanAuthority,
    OperatorPlanCompiler,
    OperatorStartCoordinator,
    build_operator_service_identity,
    build_scripted_operator_profile_catalog,
    publish_operator_profile,
)
from hcuopt.operator.errors import (
    OperatorPlanHashMismatch,
    OperatorPreviewBlocked,
    OperatorPreviewExpired,
    OperatorServiceIdentityMismatch,
    OperatorStartFailed,
    OperatorWarningAcknowledgementRequired,
)
from hcuopt.operator.profiles import OperatorProfileCatalog
from hcuopt.operator.start import FrozenScriptedPlans
from hcuopt.orchestrator.search_round import candidate_family_hash

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

    def assert_operator_candidate_ids_available(
        self,
        _candidate_ids,
        *,
        allowed_round_id=None,
    ) -> None:  # type: ignore[no-untyped-def]
        del allowed_round_id


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

    def get_operator_plan_preview(self, preview_id):  # type: ignore[no-untyped-def]
        assert self.preview is not None and self.preview.preview_id == preview_id
        return self.preview


class MemoryStartRepository(PreviewRepository):
    def __init__(self, authority: ResolvedOperatorAuthority) -> None:
        super().__init__(authority)
        self.intent: OperatorStartIntentView | None = None
        self.round: dict | None = None
        self.members: dict[int, dict] = {}
        self.candidate_owner: UUID | None = None

    def assert_operator_candidate_ids_available(
        self,
        _candidate_ids,
        *,
        allowed_round_id=None,
    ) -> None:  # type: ignore[no-untyped-def]
        if self.candidate_owner is not None and self.candidate_owner != allowed_round_id:
            raise Conflict("Operator Candidate identity is already bound")

    @staticmethod
    def _intent_update(intent, **updates):  # type: ignore[no-untyped-def]
        return OperatorStartIntentView.model_validate(
            {
                **intent.model_dump(mode="json"),
                **updates,
                "version": intent.version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def create_operator_start_intent(self, intent):  # type: ignore[no-untyped-def]
        if self.intent is None:
            self.intent = intent
            return intent, True
        assert self.intent.request_digest == intent.request_digest
        return self.intent, False

    def get_operator_start_intent(self, intent_id):  # type: ignore[no-untyped-def]
        assert self.intent is not None and self.intent.intent_id == intent_id
        return self.intent

    def get_operator_start_intent_by_idempotency(
        self, idempotency_key
    ):  # type: ignore[no-untyped-def]
        if self.intent is None or self.intent.idempotency_key != idempotency_key:
            return None
        return self.intent

    def record_operator_start_plans(
        self, intent_id, plans: FrozenScriptedPlans
    ):  # type: ignore[no-untyped-def]
        intent = self.get_operator_start_intent(intent_id)
        if intent.state != "preparing":
            expected = {
                "search_plan_hash": plans.search_plan_hash,
                "holdout_plan_commitment": plans.holdout_plan_commitment,
                "holdout_commitment_scheme": plans.holdout_commitment_scheme,
                "holdout_plan_authority_id": plans.holdout_plan_authority_id,
                "holdout_plan_authority_hash": plans.holdout_plan_authority_hash,
                "family_alpha": plans.family_alpha,
            }
            if any(getattr(intent, name) != value for name, value in expected.items()):
                raise Conflict("Operator Start Plan Authority changed during replay")
            return intent
        self.intent = self._intent_update(
            intent,
            state="plans_frozen",
            search_plan_hash=plans.search_plan_hash,
            holdout_plan_commitment=plans.holdout_plan_commitment,
            holdout_commitment_scheme=plans.holdout_commitment_scheme,
            holdout_plan_authority_id=plans.holdout_plan_authority_id,
            holdout_plan_authority_hash=plans.holdout_plan_authority_hash,
            family_alpha=plans.family_alpha,
        )
        return self.intent

    def record_operator_start_round_created(self, intent_id):  # type: ignore[no-untyped-def]
        intent = self.get_operator_start_intent(intent_id)
        if intent.state == "plans_frozen":
            self.intent = self._intent_update(intent, state="round_created")
        return self.intent

    def record_operator_start_member_bound(
        self, intent_id, ordinal
    ):  # type: ignore[no-untyped-def]
        intent = self.get_operator_start_intent(intent_id)
        members = list(intent.candidate_members)
        members[ordinal] = OperatorStartCandidateMember.model_validate(
            {**members[ordinal].model_dump(mode="json"), "state": "round_member_bound"}
        )
        self.intent = self._intent_update(
            intent,
            candidate_members=[item.model_dump(mode="json") for item in members],
        )
        return self.intent

    def record_operator_start_intake_closed(
        self, intent_id, candidate_family_hash
    ):  # type: ignore[no-untyped-def]
        intent = self.get_operator_start_intent(intent_id)
        self.intent = self._intent_update(
            intent,
            state="intake_closed",
            candidate_family_hash=candidate_family_hash,
        )
        return self.intent

    def finalize_operator_start_intent(self, intent_id):  # type: ignore[no-untyped-def]
        intent = self.get_operator_start_intent(intent_id)
        if intent.state == "finalized":
            return intent
        self.intent = self._intent_update(
            intent,
            state="finalized",
            finalized_at=datetime.now(timezone.utc),
        )
        return self.intent

    def fail_operator_start_intent(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the successful memory fixture must not fail")

    def create_search_round(self, request: SearchRound) -> dict:
        payload = request.model_dump(mode="python")
        payload["updated_at"] = request.created_at
        if self.round is None:
            self.round = payload
        else:
            assert self.round["round_id"] == request.round_id
        return self.round

    def add_round_candidate(self, request: RoundCandidate) -> dict:
        payload = request.model_dump(mode="python")
        existing = self.members.setdefault(request.ordinal, payload)
        assert existing == payload
        return existing

    def close_search_round_intake(self, round_id: UUID) -> dict:
        assert self.round is not None and self.round["round_id"] == round_id
        family_hash = candidate_family_hash(
            self.round,
            [self.members[index] for index in sorted(self.members)],
        )
        self.round.update(
            state="intake_closed",
            candidate_family_hash=family_hash,
        )
        return self.round


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


def _startable_suite(tmp_path: Path):  # type: ignore[no-untyped-def]
    compiler, request, authority_repository = _suite(tmp_path)
    live_compiler = OperatorPlanCompiler(
        compiler.profiles,
        compiler.service_identity,
        candidate_intake=compiler.candidate_intake,
    )
    repository = MemoryStartRepository(authority_repository.authority)
    preview = live_compiler.compile(request, repository)
    repository.create_operator_plan_preview(request.idempotency_key, preview)
    coordinator = OperatorStartCoordinator(
        live_compiler,
        plan_authority=HmacScriptedPlanAuthority(
            b"operator-test-secret-32-bytes!!!!"
        ),
    )
    start_request = OperatorRoundStartRequest(
        preview_id=preview.preview_id,
        resolved_plan_hash=preview.resolved_plan_hash,
        actor="operator-test",
        idempotency_key="operator-start-scripted",
        acknowledged_warning_codes=preview.required_ack_codes,
        expected_service_identity=compiler.service_identity.model_dump(mode="json"),
    )
    return live_compiler, repository, preview, coordinator, start_request


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


def test_operator_start_finalizes_and_replays_one_scripted_intent(
    tmp_path: Path,
) -> None:
    live_compiler, repository, preview, coordinator, start_request = (
        _startable_suite(tmp_path)
    )

    first = coordinator.start(start_request, repository)
    replay = coordinator.start(start_request, repository)

    assert first.state == "finalized"
    assert first.executable is True
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.intent_id == first.intent_id
    assert repository.round is not None
    assert repository.round["state"] == "intake_closed"
    assert len(repository.members) == 2
    serialized = first.model_dump_json()
    assert "nonce_hex" not in serialized
    assert "operator-test-secret" not in serialized

    app = create_app(
        repository=repository,  # type: ignore[arg-type]
        operator_profiles=live_compiler.profiles,
        operator_plan_compiler=live_compiler,
        operator_start_coordinator=coordinator,
    )
    with TestClient(app) as client:
        started = client.post(
            f"/v1/operator/round-plans/{preview.preview_id}:start",
            json=start_request.model_dump(mode="json"),
        )
        status = client.get(f"/v1/operator/start-intents/{first.intent_id}")

    assert started.status_code == 202
    assert started.json()["replayed"] is True
    assert started.headers["location"].endswith(str(first.intent_id))
    assert status.status_code == 200
    assert status.json()["state"] == "finalized"


def test_operator_start_rejects_expired_blocked_and_unacknowledged_preview(
    tmp_path: Path,
) -> None:
    _compiler, repository, preview, coordinator, start_request = _startable_suite(
        tmp_path
    )
    current = datetime.now(timezone.utc)
    repository.preview = type(preview).model_validate(
        {
            **preview.model_dump(mode="json"),
            "created_at": current - timedelta(minutes=2),
            "expires_at": current - timedelta(minutes=1),
        }
    )
    with pytest.raises(OperatorPreviewExpired):
        coordinator.start(start_request, repository)

    blocked = type(preview).model_validate(
        {
            **preview.model_dump(mode="json"),
            "checks": [
                *preview.checks,
                PreflightCheckResult(
                    code="operator_test_block",
                    scope="test",
                    status="block",
                    message="injected blocking Preflight result",
                    retryable=False,
                    action_code="replace_preview",
                ),
            ],
            "start_allowed": False,
        }
    )
    repository.preview = blocked
    with pytest.raises(OperatorPreviewBlocked):
        coordinator.start(start_request, repository)

    warning_code = "operator_test_warning"
    warned = type(preview).model_validate(
        {
            **preview.model_dump(mode="json"),
            "checks": [
                *preview.checks,
                PreflightCheckResult(
                    code=warning_code,
                    scope="test",
                    status="warn",
                    message="injected warning",
                    retryable=True,
                    action_code="acknowledge_warning",
                ),
            ],
            "required_ack_codes": [warning_code],
        }
    )
    repository.preview = warned
    with pytest.raises(OperatorWarningAcknowledgementRequired):
        coordinator.start(start_request, repository)


def test_operator_start_rejects_stale_identity_and_missing_plan_authority(
    tmp_path: Path,
) -> None:
    compiler, repository, preview, _coordinator, start_request = _startable_suite(
        tmp_path
    )
    stale = start_request.model_copy(
        update={
            "expected_service_identity": start_request.expected_service_identity.model_copy(
                update={"source_commit": "b" * 40}
            )
        }
    )
    without_authority = OperatorStartCoordinator(compiler)

    with pytest.raises(OperatorServiceIdentityMismatch):
        without_authority.start(stale, repository)
    with pytest.raises(OperatorStartFailed, match="not configured"):
        without_authority.start(start_request, repository)
    assert repository.get_operator_plan_preview(preview.preview_id) == preview


def test_operator_start_revalidates_candidate_identity_and_idempotency_inputs(
    tmp_path: Path,
) -> None:
    _compiler, repository, _preview, coordinator, start_request = _startable_suite(
        tmp_path
    )
    repository.candidate_owner = UUID("00000000-0000-0000-0000-000000000099")
    with pytest.raises(OperatorPreviewBlocked, match="Preflight"):
        coordinator.start(start_request, repository)

    repository.candidate_owner = None
    first = coordinator.start(start_request, repository)
    changed_actor = start_request.model_copy(update={"actor": "another-operator"})
    with pytest.raises(OperatorPlanHashMismatch, match="different inputs"):
        coordinator.start(changed_actor, repository)
    assert first.state == "finalized"


def test_existing_start_replays_after_preview_expiry(tmp_path: Path) -> None:
    compiler, repository, preview, coordinator, start_request = _startable_suite(
        tmp_path
    )
    first = coordinator.start(start_request, repository)
    current = datetime.now(timezone.utc)
    repository.preview = type(preview).model_validate(
        {
            **preview.model_dump(mode="json"),
            "created_at": current - timedelta(minutes=2),
            "expires_at": current - timedelta(minutes=1),
        }
    )

    replay = OperatorStartCoordinator(compiler).start(start_request, repository)

    assert replay.intent_id == first.intent_id
    assert replay.state == "finalized"
    assert replay.replayed is True


def test_plan_authority_key_rotation_stops_nonterminal_reconcile(
    tmp_path: Path,
) -> None:
    compiler, repository, _preview, coordinator, start_request = _startable_suite(
        tmp_path
    )

    class FailAfterPlansFrozen:
        def __init__(self, wrapped):  # type: ignore[no-untyped-def]
            self.wrapped = wrapped
            self.failed = False

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self.wrapped, name)

        def record_operator_start_plans(
            self, intent_id, plans
        ):  # type: ignore[no-untyped-def]
            result = self.wrapped.record_operator_start_plans(intent_id, plans)
            if not self.failed:
                self.failed = True
                raise RuntimeError("injected crash after frozen Plans")
            return result

    with pytest.raises(RuntimeError, match="injected crash"):
        coordinator.start(start_request, FailAfterPlansFrozen(repository))
    assert repository.intent is not None
    assert repository.intent.state == "plans_frozen"

    rotated_authority = HmacScriptedPlanAuthority(
        b"rotated-operator-secret-32-bytes!!"
    )
    assert isinstance(coordinator.plan_authority, HmacScriptedPlanAuthority)
    assert rotated_authority.authority_hash != coordinator.plan_authority.authority_hash
    rotated = OperatorStartCoordinator(compiler, plan_authority=rotated_authority)
    with pytest.raises(Conflict, match="Authority changed"):
        rotated.reconcile(repository.intent.intent_id, repository)
