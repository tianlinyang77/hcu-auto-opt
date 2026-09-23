# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hcuopt.deployment.formal_signing import FormalOwnerEd25519Signer
from hcuopt.deployment.formal_trust import FormalPublicTrust


def trust_bundle():
    keys = {role: Ed25519PrivateKey.generate() for role in
            ("owner", "actor", "execution", "evaluation")}
    bundle = {"schema_version": "formal-public-trust-v1"}
    for role, key in keys.items():
        raw = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        bundle[role] = {"identity_id": f"test.{role}", "key_id": f"{role}-v1",
                        "public_key_base64": base64.b64encode(raw).decode("ascii")}
    return keys, bundle


def test_owner_roundtrip_and_trust_reload(tmp_path):
    keys, bundle = trust_bundle()
    path = tmp_path / "trust.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    trust = FormalPublicTrust.from_file(deployment_root=tmp_path, path=path)
    signer = FormalOwnerEd25519Signer(keys["owner"], verifier_id="test.owner", key_id="owner-v1")
    digest = "sha256:" + "a" * 64
    signature = signer.sign_authorization(authorization_hash=digest)
    assert trust.owner.verifier_ref == signer.verifier_ref
    assert trust.owner.verify_signature(authorization_hash=digest, signature=signature)
    assert not trust.actor.verify_signature(content_hash=digest, signature=signature)
    reloaded = FormalPublicTrust.from_file(deployment_root=tmp_path, path=path)
    assert reloaded.owner.verify_signature(authorization_hash=digest, signature=signature)


@pytest.mark.parametrize("bad", ["same_key", "same_identity", "secret", "encoding", "missing"])
def test_invalid_trust_rejected_without_echo(tmp_path, bad):
    _, bundle = trust_bundle()
    if bad == "same_key":
        bundle["actor"]["public_key_base64"] = bundle["owner"]["public_key_base64"]
    elif bad == "same_identity":
        bundle["actor"]["identity_id"] = bundle["owner"]["identity_id"]
    elif bad == "secret":
        bundle["owner"]["private_key"] = "sensitive-test-value"
    elif bad == "encoding":
        bundle["owner"]["public_key_base64"] = "!" * 44
    else:
        del bundle["owner"]
    path = tmp_path / "trust.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or unavailable") as error:
        FormalPublicTrust.from_file(deployment_root=tmp_path, path=path)
    assert "sensitive-test-value" not in str(error.value)


def test_trust_path_cannot_escape_root(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    _, bundle = trust_bundle()
    path = tmp_path / "outside.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or unavailable"):
        FormalPublicTrust.from_file(deployment_root=root, path=path)


@pytest.mark.parametrize("tamper", ["signature", "content", "key_rotation"])
def test_coordinator_rejects_invalid_owner_before_start(tmp_path, tamper):
    from tests.unit.formal_signed_fixture import setup_signed_management

    management, repo, _ = setup_signed_management(tmp_path)
    compiler = management.coordinator.compiler
    path = tmp_path / "public-trust.json"
    if tamper == "key_rotation":
        bundle = json.loads(path.read_text(encoding="utf-8"))
        _, replacement = trust_bundle()
        bundle["owner"] = replacement["owner"]
        path.write_text(json.dumps(bundle), encoding="utf-8")
    else:
        update = {"signature": "A" * 88} if tamper == "signature" else {"host_id": "other-host"}
        compiler.authorization = compiler.authorization.model_copy(update=update)
    trust = FormalPublicTrust.from_file(deployment_root=tmp_path, path=path)
    with pytest.raises(ValueError):
        trust.coordinator(compiler, object_store=management.coordinator.object_store)
    assert not repo.intents


def test_runtime_from_persisted_public_trust_and_capability(tmp_path):
    from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentRequest
    from hcuopt.deployment.formal_runtime import FormalDeploymentRuntime
    from tests.unit.formal_signed_fixture import setup_signed_management

    management, repo, payload = setup_signed_management(tmp_path)
    capability = management.capabilities[0]
    access = tmp_path / "capabilities.json"
    access.write_text(json.dumps({
        "schema_version": "formal-intent-capabilities-v1", "capabilities": [{
            "token_sha256": capability.token_sha256,
            "assertion": capability.assertion.model_dump(mode="json"), "submission": payload,
        }],
    }), encoding="utf-8")
    ids = []
    for _ in range(2):
        runtime = FormalDeploymentRuntime.from_deployment_files(
            repo, compiler=management.coordinator.compiler,
            object_store=management.coordinator.object_store,
            deployment_root=tmp_path, trust_path=tmp_path / "public-trust.json",
            capabilities_path=access, clock=management.coordinator.clock,
        )
        assert not runtime.dispatcher.enabled
        result = runtime.management.coordinator.create(FormalStartIntentRequest(
            **payload, actor_assertion=runtime.management.capabilities[0].assertion,
        ), repo)
        assert result.state == "ready_for_round_creation"
        ids.append(result.intent_id)
    assert ids[0] == ids[1]
    assert len(repo.intents) == 1
