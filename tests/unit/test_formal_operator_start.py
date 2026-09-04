# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hcuopt.api.app import create_app
from hcuopt.cli import build_parser
from hcuopt.contracts.formal_evidence_acceptance_v1 import (
    FormalEvidenceVerifierIdentityContent,
    ProductionEvidenceRootContent,
    publish_formal_evidence_verifier_identity,
    publish_production_evidence_root,
)
from hcuopt.contracts.m2_formal_start_v1 import (
    FormalEvaluationStartAuthority,
    FormalEvaluationStartAuthorityContent,
    FormalExecutionStartAuthority,
    FormalExecutionStartAuthorityContent,
    FormalStartActorAssertionContent,
    FormalStartIntentActionRequest,
    FormalStartIntentRequest,
    FormalStartIntentView,
    FormalStartSignerRef,
    derive_formal_start_ids,
    formal_start_action_subject_digest,
    formal_start_content_hash,
    publish_formal_evaluation_start_authority,
    publish_formal_execution_start_authority,
    publish_formal_start_actor_assertion,
)
from hcuopt.domain.errors import Conflict, NotFound
from hcuopt.operator.cli import OperatorHttpClient
from hcuopt.operator.errors import (
    OperatorFormalStartAuthenticationInvalid,
    OperatorFormalStartAuthorityInvalid,
    OperatorPlanHashMismatch,
    OperatorProfileNotFound,
    OperatorProfileRevoked,
)
from hcuopt.operator.formal_start import FormalStartCoordinator
from tests.unit.test_formal_operator_plans import NOW, _fixture, _hash

ACTOR_SIGNER = FormalStartSignerRef(
    signer_id="formal-operator-authenticator",
    signer_version="1.0.0",
    signer_hash=_hash("actor-signer"),
    signature_scheme="test-signature-v1",
    key_id="actor-test-key",
)
B_SIGNER = FormalStartSignerRef(
    signer_id="formal-execution-authority",
    signer_version="1.0.0",
    signer_hash=_hash("execution-signer"),
    signature_scheme="test-signature-v1",
    key_id="execution-test-key",
)
D_SIGNER = FormalStartSignerRef(
    signer_id="formal-evaluation-authority",
    signer_version="1.0.0",
    signer_hash=_hash("evaluation-signer"),
    signature_scheme="test-signature-v1",
    key_id="evaluation-test-key",
)
START_KEY = "formal-start-intent-test-v1"


@dataclass
class _Verifier:
    signer_ref: FormalStartSignerRef
    accepted: bool = True
    raises: bool = False

    def verify_signature(self, *, content_hash: str, signature: str) -> bool:
        assert content_hash.startswith("sha256:")
        assert signature
        if self.raises:
            raise RuntimeError("test verifier unavailable")
        return self.accepted


@dataclass
class _ObjectStore:
    preview: object
    execution: FormalExecutionStartAuthority | None = None
    evaluation: FormalEvaluationStartAuthority | None = None

    def load_preview(self, preview_id: UUID):  # type: ignore[no-untyped-def]
        if self.preview.preview_id != preview_id:  # type: ignore[attr-defined]
            raise NotFound("missing preview")
        return self.preview

    def load_execution_authority(self, authority_hash: str):  # type: ignore[no-untyped-def]
        if self.execution is None or self.execution.authority_hash != authority_hash:
            raise NotFound("missing execution authority")
        return self.execution

    def load_evaluation_authority(self, authority_hash: str):  # type: ignore[no-untyped-def]
        if self.evaluation is None or self.evaluation.authority_hash != authority_hash:
            raise NotFound("missing evaluation authority")
        return self.evaluation


@dataclass
class _Repository:
    authority: object
    intents: dict[UUID, FormalStartIntentView] = field(default_factory=dict)

    def migrate(self) -> None:
        return None

    def resolve_formal_operator_authority(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return self.authority.resolve_formal_operator_authority(None, None, None)

    def assert_operator_candidate_ids_available(self, candidate_ids, **kwargs):  # type: ignore[no-untyped-def]
        return self.authority.assert_operator_candidate_ids_available(
            candidate_ids, **kwargs
        )

    def create_formal_start_intent(self, intent):  # type: ignore[no-untyped-def]
        existing = next(
            (
                value
                for value in self.intents.values()
                if value.intent_id == intent.intent_id
                or value.preview_id == intent.preview_id
                or value.idempotency_key == intent.idempotency_key
            ),
            None,
        )
        if existing is not None:
            return existing, False
        self.intents[intent.intent_id] = intent
        return intent, True

    def get_formal_start_intent(self, intent_id):  # type: ignore[no-untyped-def]
        try:
            return self.intents[intent_id]
        except KeyError as error:
            raise NotFound("missing intent") from error

    def get_formal_start_intent_by_idempotency(self, key):  # type: ignore[no-untyped-def]
        return next(
            (value for value in self.intents.values() if value.idempotency_key == key),
            None,
        )

    def record_formal_start_reconciliation(
        self,
        intent_id,
        *,
        state,
        blocker_codes,
        checked_at,
        error_code=None,
        error_message=None,
    ):  # type: ignore[no-untyped-def]
        current = self.get_formal_start_intent(intent_id)
        if current.state in {"cancelled", "failed"}:
            return current
        updated = current.model_copy(
            update={
                "state": state,
                "blocker_codes": blocker_codes,
                "error_code": error_code,
                "error_message": error_message,
                "authority_reconcile_count": current.authority_reconcile_count + 1,
                "version": current.version + 1,
                "updated_at": checked_at,
                "last_reconciled_at": checked_at,
                "ready_at": checked_at if state == "ready_for_round_creation" else None,
                "authority_ready": state == "ready_for_round_creation",
            }
        )
        updated = FormalStartIntentView.model_validate(updated.model_dump(mode="json"))
        self.intents[intent_id] = updated
        return updated

    def cancel_formal_start_intent(self, intent_id, *, cancelled_at):  # type: ignore[no-untyped-def]
        current = self.get_formal_start_intent(intent_id)
        if current.state == "failed":
            raise Conflict("failed")
        if current.state == "cancelled":
            return current
        updated = FormalStartIntentView.model_validate(
            current.model_copy(
                update={
                    "state": "cancelled",
                    "blocker_codes": (),
                    "ready_at": None,
                    "authority_ready": False,
                    "cancelled_at": cancelled_at,
                    "updated_at": cancelled_at,
                    "version": current.version + 1,
                }
            ).model_dump(mode="json")
        )
        self.intents[intent_id] = updated
        return updated

    def list_recoverable_formal_start_intent_ids(self, limit):  # type: ignore[no-untyped-def]
        return tuple(
            value.intent_id
            for value in list(self.intents.values())[:limit]
            if value.state in {"awaiting_authority", "ready_for_round_creation"}
        )


def _production_evidence(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = publish_production_evidence_root(
        ProductionEvidenceRootContent(
            root_id="formal-start-test-root",
            root_version=1,
            root_uri=tmp_path.resolve().as_uri(),
            access_policy_hash=_hash("evidence-access"),
            retention_policy_hash=_hash("evidence-retention"),
            deployment_owner="formal-evidence-owner",
            max_object_bytes=1024 * 1024,
        )
    )
    verifier = publish_formal_evidence_verifier_identity(
        FormalEvidenceVerifierIdentityContent(
            verifier_id="formal-independent-verifier",
            verifier_version="1.0.0",
            executable_hash=_hash("verifier-executable"),
            configuration_hash=_hash("verifier-config"),
            source_commit="c" * 40,
            attestation_scheme="test-attestation-v1",
            attestation_key_id="independent-verifier-test-key",
            identity_evidence_uri=(tmp_path / "verifier.json").resolve().as_uri(),
            identity_evidence_hash=_hash("verifier-identity"),
        )
    )
    return root, verifier


def _authorities(
    fixture,
    preview,
    tmp_path,
    *,
    idempotency_key=START_KEY,
):  # type: ignore[no-untyped-def]
    plan = preview.resolved_plan
    root, independent_verifier = _production_evidence(tmp_path)
    intent_id, task_id, round_id = derive_formal_start_ids(idempotency_key)
    execution = publish_formal_execution_start_authority(
        FormalExecutionStartAuthorityContent(
            authority_id=uuid4(),
            intent_id=intent_id,
            preview_id=preview.preview_id,
            task_id=task_id,
            round_id=round_id,
            decision="authorized",
            formal_authorization_hash=fixture.authorization.authorization_hash,
            resolved_plan_hash=preview.resolved_plan_hash,
            candidate_family_hash=plan.source_family_hash,
            adapter_profile_id=plan.authority.adapter_profile,
            adapter_profile_version="1.0.0",
            adapter_profile_hash=_hash("execution-adapter-profile"),
            host_id=fixture.authorization.host_id,
            resource_id=fixture.authorization.resource_id,
            window_starts_at=fixture.authorization.window_starts_at,
            window_expires_at=fixture.authorization.window_expires_at,
            budget=plan.budget,
            lease_policy_hash=_hash("lease-policy"),
            fencing_policy_hash=_hash("fencing-policy"),
            cleanup_policy_hash=_hash("cleanup-policy"),
            issued_at=NOW,
            expires_at=fixture.authorization.window_expires_at,
            signer=B_SIGNER,
        ),
        signature="execution-signature",
    )
    evaluation = publish_formal_evaluation_start_authority(
        FormalEvaluationStartAuthorityContent(
            authority_id=uuid4(),
            intent_id=intent_id,
            preview_id=preview.preview_id,
            task_id=task_id,
            round_id=round_id,
            decision="authorized",
            formal_authorization_hash=fixture.authorization.authorization_hash,
            resolved_plan_hash=preview.resolved_plan_hash,
            candidate_family_hash=plan.source_family_hash,
            search_plan_hash=_hash("formal-search-plan"),
            holdout_plan_commitment=_hash("formal-holdout-commitment"),
            holdout_plan_authority_id="formal-holdout-plan-authority",
            holdout_plan_authority_hash=_hash("holdout-plan-authority"),
            selection_rule_hash=plan.selection_rule_hash,
            family_alpha=0.05,
            evidence_root=root,
            independent_verifier=independent_verifier,
            issued_at=NOW,
            expires_at=fixture.authorization.window_expires_at,
            signer=D_SIGNER,
        ),
        signature="evaluation-signature",
    )
    return execution, evaluation


def _request(
    fixture,
    preview,
    execution_hash,
    evaluation_hash,
    *,
    idempotency_key=START_KEY,
):  # type: ignore[no-untyped-def]
    core = {
        "preview_id": preview.preview_id,
        "resolved_plan_hash": preview.resolved_plan_hash,
        "formal_authorization_hash": fixture.authorization.authorization_hash,
        "execution_authority_hash": execution_hash,
        "evaluation_authority_hash": evaluation_hash,
        "idempotency_key": idempotency_key,
        "expected_service_identity": fixture.compiler.service_identity.model_dump(mode="json"),
    }
    assertion = publish_formal_start_actor_assertion(
        FormalStartActorAssertionContent(
            assertion_id=uuid4(),
            actor_id="formal-project-operator",
            action="create",
            subject_digest=formal_start_content_hash(core),
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
            signer=ACTOR_SIGNER,
        ),
        signature="actor-signature",
    )
    return FormalStartIntentRequest(**core, actor_assertion=assertion)


def _coordinator(fixture, store, **overrides):  # type: ignore[no-untyped-def]
    values = {
        "actor_verifier": _Verifier(ACTOR_SIGNER),
        "execution_verifier": _Verifier(B_SIGNER),
        "evaluation_verifier": _Verifier(D_SIGNER),
    }
    values.update(overrides)
    return FormalStartCoordinator(
        fixture.compiler,
        object_store=store,
        clock=lambda: NOW,
        **values,
    )


def test_formal_start_waits_then_recovers_without_creating_a_round(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview)
    repository = _Repository(fixture.repository)
    coordinator = _coordinator(fixture, store)
    request = _request(
        fixture, preview, execution.authority_hash, evaluation.authority_hash
    )

    waiting = coordinator.create(request, repository)

    assert waiting.state == "awaiting_authority"
    assert waiting.blocker_codes == (
        "formal_evaluation_start_authority_unavailable",
        "formal_execution_start_authority_unavailable",
    )
    assert waiting.round_creation_allowed is False
    assert waiting.hcu_accessed is False
    store.execution = execution
    store.evaluation = evaluation

    recovered = coordinator.recover(repository)

    assert len(recovered) == 1
    assert recovered[0].state == "ready_for_round_creation"
    assert recovered[0].authority_ready is True
    assert recovered[0].round_creation_allowed is False
    assert recovered[0].hcu_accessed is False


def test_formal_start_fails_closed_without_production_verifiers(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview, execution, evaluation)
    repository = _Repository(fixture.repository)
    request = _request(
        fixture, preview, execution.authority_hash, evaluation.authority_hash
    )

    with pytest.raises(OperatorFormalStartAuthorityInvalid, match="verifiers"):
        _coordinator(fixture, store, execution_verifier=None).create(
            request, repository
        )

    assert not repository.intents


def test_formal_start_rejects_actor_signature_before_persistence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview, execution, evaluation)
    repository = _Repository(fixture.repository)
    request = _request(
        fixture, preview, execution.authority_hash, evaluation.authority_hash
    )

    with pytest.raises(OperatorFormalStartAuthenticationInvalid):
        _coordinator(
            fixture,
            store,
            actor_verifier=_Verifier(ACTOR_SIGNER, accepted=False),
        ).create(request, repository)

    assert not repository.intents


def test_formal_start_authority_drift_becomes_terminal_safe_failure(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    drifted = execution.model_copy(
        update={"host_id": "another-host", "authority_hash": execution.authority_hash}
    )
    store = _ObjectStore(preview, drifted, evaluation)
    repository = _Repository(fixture.repository)
    request = _request(
        fixture, preview, execution.authority_hash, evaluation.authority_hash
    )

    failed = _coordinator(fixture, store).create(request, repository)

    assert failed.state == "failed"
    assert failed.error_code == "formal_start_authority_invalid"
    assert failed.error_message == "Formal Start Authority verification failed closed."
    assert failed.round_creation_allowed is False


def test_formal_execution_authority_is_issued_after_preview_and_not_from_future(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)

    assert execution.schema_version == "m2a-formal-execution-start-authority-v2"
    early = execution.model_dump(mode="json", exclude={"authority_hash", "signature"})
    early["issued_at"] = (fixture.authorization.window_starts_at - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValidationError, match="window is invalid"):
        FormalExecutionStartAuthorityContent.model_validate(early)

    future_content = FormalExecutionStartAuthorityContent.model_validate(
        execution.model_dump(mode="json", exclude={"authority_hash", "signature"})
        | {"issued_at": (NOW + timedelta(minutes=1)).isoformat()}
    )
    future = publish_formal_execution_start_authority(
        future_content,
        signature="future-execution-signature",
    )
    failed = _coordinator(
        fixture,
        _ObjectStore(preview, future, evaluation),
    ).create(
        _request(fixture, preview, future.authority_hash, evaluation.authority_hash),
        _Repository(fixture.repository),
    )

    assert failed.state == "failed"
    assert failed.error_code == "formal_start_authority_invalid"


def test_formal_start_rejects_owner_verifier_role_reuse(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    owner = fixture.authorization.verifier
    reused = FormalStartSignerRef(
        signer_id=owner.verifier_id,
        signer_version=owner.verifier_version,
        signer_hash=owner.verifier_hash,
        signature_scheme=owner.signature_scheme,
        key_id=owner.key_id,
    )
    content = FormalEvaluationStartAuthorityContent.model_validate(
        evaluation.model_dump(mode="json", exclude={"authority_hash", "signature"})
        | {"signer": reused.model_dump(mode="json")}
    )
    reused_evaluation = publish_formal_evaluation_start_authority(
        content,
        signature="owner-reused-evaluation-signature",
    )

    failed = _coordinator(
        fixture,
        _ObjectStore(preview, execution, reused_evaluation),
        evaluation_verifier=_Verifier(reused),
    ).create(
        _request(
            fixture,
            preview,
            execution.authority_hash,
            reused_evaluation.authority_hash,
        ),
        _Repository(fixture.repository),
    )

    assert failed.state == "failed"
    assert failed.error_code == "formal_start_authority_invalid"


def test_formal_start_is_idempotent_and_rejects_ref_substitution(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview, execution, evaluation)
    repository = _Repository(fixture.repository)
    coordinator = _coordinator(fixture, store)
    request = _request(
        fixture, preview, execution.authority_hash, evaluation.authority_hash
    )

    first = coordinator.create(request, repository)
    replay = coordinator.create(request, repository)

    assert first.intent_id == replay.intent_id
    assert replay.replayed is True
    changed = request.model_copy(
        update={"evaluation_authority_hash": _hash("substituted-authority")}
    )
    changed_core = changed.model_dump(mode="json", exclude={"actor_assertion"})
    changed_assertion = publish_formal_start_actor_assertion(
        FormalStartActorAssertionContent.model_validate(
            request.actor_assertion.model_dump(
                mode="json", exclude={"assertion_hash", "signature"}
            )
            | {"subject_digest": formal_start_content_hash(changed_core)}
        ),
        signature="changed-actor-signature",
    )
    changed = changed.model_copy(update={"actor_assertion": changed_assertion})
    with pytest.raises(OperatorPlanHashMismatch):
        coordinator.create(changed, repository)


def test_formal_start_authorities_cannot_be_reused_for_another_round(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    other_preview = preview.model_copy(update={"preview_id": uuid4()})
    store = _ObjectStore(other_preview, execution, evaluation)
    repository = _Repository(fixture.repository)

    result = _coordinator(fixture, store).create(
        _request(
            fixture,
            other_preview,
            execution.authority_hash,
            evaluation.authority_hash,
            idempotency_key="formal-start-intent-second-round-v1",
        ),
        repository,
    )

    assert result.state == "failed"
    assert result.error_code == "formal_start_authority_invalid"
    assert result.round_creation_allowed is False


@pytest.mark.parametrize(
    ("error", "expected_state", "expected_code"),
    [
        (
            OperatorProfileNotFound("profile temporarily unavailable"),
            "awaiting_authority",
            None,
        ),
        (
            OperatorProfileRevoked("profile was revoked"),
            "failed",
            "formal_start_plan_invalid",
        ),
    ],
)
def test_formal_start_revalidation_classifies_temporary_and_terminal_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_state: str,
    expected_code: str | None,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview)
    repository = _Repository(fixture.repository)
    coordinator = _coordinator(fixture, store)
    coordinator.create(
        _request(
            fixture,
            preview,
            execution.authority_hash,
            evaluation.authority_hash,
        ),
        repository,
    )

    def fail_revalidation(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise error

    monkeypatch.setattr(fixture.compiler, "revalidate", fail_revalidation)
    recovered = coordinator.recover(repository)[0]

    assert recovered.state == expected_state
    assert recovered.error_code == expected_code
    if expected_state == "awaiting_authority":
        assert recovered.blocker_codes == (
            "formal_operator_authority_revalidation_failed",
        )


def test_formal_start_cancel_requires_exact_authenticated_action(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview)
    repository = _Repository(fixture.repository)
    coordinator = _coordinator(fixture, store)
    created = coordinator.create(
        _request(fixture, preview, execution.authority_hash, evaluation.authority_hash),
        repository,
    )
    subject = formal_start_action_subject_digest(
        action="cancel", intent_id=created.intent_id
    )
    assertion = publish_formal_start_actor_assertion(
        FormalStartActorAssertionContent(
            assertion_id=uuid4(),
            actor_id="formal-project-operator",
            action="cancel",
            subject_digest=subject,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
            signer=ACTOR_SIGNER,
        ),
        signature="cancel-signature",
    )

    cancelled = coordinator.cancel(
        FormalStartIntentActionRequest(
            intent_id=created.intent_id,
            action="cancel",
            actor_assertion=assertion,
        ),
        repository,
    )

    assert cancelled.state == "cancelled"
    assert cancelled.authority_ready is False


@pytest.mark.parametrize("action", ["reconcile", "cancel"])
def test_formal_start_actions_reject_another_authenticated_actor(
    tmp_path: Path,
    action: str,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview)
    repository = _Repository(fixture.repository)
    coordinator = _coordinator(fixture, store)
    created = coordinator.create(
        _request(fixture, preview, execution.authority_hash, evaluation.authority_hash),
        repository,
    )
    assertion = publish_formal_start_actor_assertion(
        FormalStartActorAssertionContent(
            assertion_id=uuid4(),
            actor_id="another-formal-operator",
            action=action,
            subject_digest=formal_start_action_subject_digest(
                action=action,
                intent_id=created.intent_id,
            ),
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
            signer=ACTOR_SIGNER,
        ),
        signature="other-actor-signature",
    )
    request = FormalStartIntentActionRequest(
        intent_id=created.intent_id,
        action=action,
        actor_assertion=assertion,
    )

    with pytest.raises(
        OperatorFormalStartAuthenticationInvalid,
        match="does not own this Intent",
    ):
        if action == "reconcile":
            coordinator.reconcile(request, repository)
        else:
            coordinator.cancel(request, repository)

    unchanged = repository.get_formal_start_intent(created.intent_id)
    assert unchanged.model_dump(mode="json") == created.model_dump(
        mode="json", exclude={"replayed"}
    )


def test_formal_start_request_forbids_client_owned_plan_or_adapter_fields(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    request = _request(
        fixture, preview, execution.authority_hash, evaluation.authority_hash
    )

    with pytest.raises(ValidationError):
        FormalStartIntentRequest.model_validate(
            request.model_dump(mode="json")
            | {
                "candidate_ids": [
                    str(item.candidate_id)
                    for item in preview.resolved_plan.candidates
                ],
                "adapter_profile": "client-selected-adapter",
            }
        )


def test_formal_start_api_is_authenticated_read_only(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    store = _ObjectStore(preview)
    repository = _Repository(fixture.repository)
    created = _coordinator(fixture, store).create(
        _request(fixture, preview, execution.authority_hash, evaluation.authority_hash),
        repository,
    )
    application = create_app(
        repository=repository,  # type: ignore[arg-type]
        formal_start_read_authorizer=lambda request, intent_id: (
            intent_id == created.intent_id
            and request.headers.get("Authorization") == "Bearer read-token"
        ),
    )

    with TestClient(application) as transport:
        rejected = transport.get(
            f"/v1/operator/formal-start-intents/{created.intent_id}"
        )
        assert rejected.status_code == 403
        with OperatorHttpClient("http://testserver", client=transport) as client:
            loaded = client.formal_start_status(
                created.intent_id,
                read_token="read-token",
            )

    assert loaded.model_dump(mode="json") == created.model_dump(
        mode="json", exclude={"replayed"}
    )
    methods = application.openapi()["paths"][
        "/v1/operator/formal-start-intents/{intent_id}"
    ]
    assert set(methods) == {"get"}


def test_formal_start_api_fails_closed_when_read_authentication_is_missing(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "packages")
    preview = fixture.compiler.compile(fixture.request, fixture.repository)
    execution, evaluation = _authorities(fixture, preview, tmp_path)
    repository = _Repository(fixture.repository)
    created = _coordinator(fixture, _ObjectStore(preview)).create(
        _request(fixture, preview, execution.authority_hash, evaluation.authority_hash),
        repository,
    )

    with TestClient(
        create_app(repository=repository)  # type: ignore[arg-type]
    ) as transport:
        response = transport.get(
            f"/v1/operator/formal-start-intents/{created.intent_id}"
        )

    assert response.status_code == 503


def test_formal_start_cli_exposes_only_authenticated_status_read() -> None:
    intent_id = uuid4()

    parsed = build_parser().parse_args(
        ["formal-start", "status", str(intent_id), "--json"]
    )

    assert parsed.command == "formal-start"
    assert parsed.formal_start_command == "status"
    assert parsed.intent_id == intent_id
    assert parsed.read_token_env == "HCUOPT_FORMAL_START_READ_TOKEN"
    assert parsed.json is True
