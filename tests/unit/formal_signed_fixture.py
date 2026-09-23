# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real actor/B/D cryptography over synthetic authority, never deployment material."""

import json
from dataclasses import replace

import pytest

from hcuopt.api.formal_start_management import FormalIntentSubmission
from hcuopt.contracts.formal_profile_authorization_v1 import (
    FormalProfileWindowAuthorizationContent,
    formal_profile_window_authorization_hash,
    publish_formal_profile_window_authorization,
)
from hcuopt.contracts.m2_formal_start_v1 import (
    FormalEvaluationStartAuthorityContent,
    FormalExecutionStartAuthorityContent,
    FormalStartActorAssertionContent,
    formal_start_content_hash,
    publish_formal_evaluation_start_authority,
    publish_formal_execution_start_authority,
    publish_formal_start_actor_assertion,
)
from hcuopt.deployment.formal_signing import FormalEd25519Signer, FormalOwnerEd25519Signer
from hcuopt.deployment.formal_trust import FormalPublicTrust
from tests.unit import test_formal_operator_plans as plans
from tests.unit.test_formal_start_management_api import setup_management
from tests.unit.test_formal_trust import trust_bundle


def setup_signed_management(tmp_path):
    keys, bundle = trust_bundle()
    owner = FormalOwnerEd25519Signer(keys["owner"], verifier_id="test.owner", key_id="owner-v1")
    original_authorization = plans._authorization

    def signed_owner(*args, **kwargs):
        old = original_authorization(*args, **kwargs)
        content = FormalProfileWindowAuthorizationContent.model_validate({
            **old.model_dump(mode="json", exclude={"authorization_hash", "signature"}),
            "verifier": owner.verifier_ref,
        })
        signature = owner.sign_authorization(
            authorization_hash=formal_profile_window_authorization_hash(content),
        )
        return publish_formal_profile_window_authorization(content, signature=signature)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plans, "_authorization", signed_owner)
        management, repository, payload = setup_management(tmp_path)
    coordinator = management.coordinator
    store = coordinator.object_store
    for role, model, publish in (
        ("execution", FormalExecutionStartAuthorityContent,
         publish_formal_execution_start_authority),
        ("evaluation", FormalEvaluationStartAuthorityContent,
         publish_formal_evaluation_start_authority),
    ):
        signer = FormalEd25519Signer(keys[role], role=role,
                                    signer_id=f"test.{role}", key_id=f"{role}-v1")
        old = getattr(store, role)
        content = model.model_validate({
            **old.model_dump(mode="json", exclude={"authority_hash", "signature"}),
            "signer": signer.signer_ref,
        })
        signed = publish(content, signature=signer.sign_authority(
            content_hash=formal_start_content_hash(content),
        ))
        setattr(store, role, signed)
        setattr(coordinator, f"{role}_verifier", signer.verifier())
        payload[f"{role}_authority_hash"] = signed.authority_hash
    actor = FormalEd25519Signer(keys["actor"], role="actor",
                                signer_id="test.actor", key_id="actor-v1")
    submission = FormalIntentSubmission.model_validate(payload)
    old_assertion = management.capabilities[0].assertion
    content = FormalStartActorAssertionContent.model_validate({
        **old_assertion.model_dump(mode="json", exclude={"assertion_hash", "signature"}),
        "subject_digest": formal_start_content_hash(submission), "signer": actor.signer_ref,
    })
    assertion = publish_formal_start_actor_assertion(
        content, signature=actor.sign_assertion(content_hash=formal_start_content_hash(content)),
    )
    trust_path = tmp_path / "public-trust.json"
    trust_path.write_text(json.dumps(bundle), encoding="utf-8")
    trust = FormalPublicTrust.from_file(deployment_root=tmp_path, path=trust_path)
    coordinator = trust.coordinator(
        coordinator.compiler, object_store=store, clock=coordinator.clock,
    )
    management = replace(management, coordinator=coordinator, capabilities=(replace(
        management.capabilities[0], assertion=assertion, submission=submission,
    ),))
    return management, repository, payload
