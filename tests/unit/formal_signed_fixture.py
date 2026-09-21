# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real actor/B/D cryptography over synthetic authority, never deployment material."""

from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hcuopt.api.formal_start_management import FormalIntentSubmission
from hcuopt.contracts.m2_formal_start_v1 import (
    FormalEvaluationStartAuthorityContent,
    FormalExecutionStartAuthorityContent,
    FormalStartActorAssertionContent,
    formal_start_content_hash,
    publish_formal_evaluation_start_authority,
    publish_formal_execution_start_authority,
    publish_formal_start_actor_assertion,
)
from hcuopt.deployment.formal_signing import FormalEd25519Signer
from tests.unit.test_formal_start_management_api import setup_management


def setup_signed_management(tmp_path):
    management, repository, payload = setup_management(tmp_path)
    coordinator = management.coordinator
    store = coordinator.object_store
    for role, model, publish in (
        ("execution", FormalExecutionStartAuthorityContent,
         publish_formal_execution_start_authority),
        ("evaluation", FormalEvaluationStartAuthorityContent,
         publish_formal_evaluation_start_authority),
    ):
        signer = FormalEd25519Signer(Ed25519PrivateKey.generate(), role=role,
                                    signer_id=f"test.{role}", key_id=f"test-{role}-key")
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
    actor = FormalEd25519Signer(Ed25519PrivateKey.generate(), role="actor",
                                signer_id="test.actor", key_id="test-actor-key")
    submission = FormalIntentSubmission.model_validate(payload)
    old_assertion = management.capabilities[0].assertion
    content = FormalStartActorAssertionContent.model_validate({
        **old_assertion.model_dump(mode="json", exclude={"assertion_hash", "signature"}),
        "subject_digest": formal_start_content_hash(submission), "signer": actor.signer_ref,
    })
    assertion = publish_formal_start_actor_assertion(
        content, signature=actor.sign_assertion(content_hash=formal_start_content_hash(content)),
    )
    coordinator.actor_verifier = actor.verifier()
    management = replace(management, capabilities=(replace(
        management.capabilities[0], assertion=assertion, submission=submission,
    ),))
    return management, repository, payload
