# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hcuopt.deployment.formal_signing import FormalEd25519Signer, FormalEd25519Verifier

DIGEST = "sha256:" + "a" * 64


def signer(role="execution", key=None, name="signer.one"):
    return FormalEd25519Signer(key or Ed25519PrivateKey.generate(), role=role,
                               signer_id=name, key_id="key-v1")


@pytest.mark.parametrize("role", ["actor", "execution", "evaluation"])
def test_real_signature_roundtrip_and_tampering(role):
    private = signer(role)
    sign = private.sign_assertion if role == "actor" else private.sign_authority
    signature = sign(content_hash=DIGEST)
    public = private.verifier()
    assert public.verify_signature(content_hash=DIGEST, signature=signature)
    assert not public.verify_signature(content_hash="sha256:" + "b" * 64, signature=signature)
    assert not signer(role).verifier().verify_signature(content_hash=DIGEST, signature=signature)
    assert not hasattr(public, "sign_authority")
    assert not hasattr(public, "_private_key")


@pytest.mark.parametrize("signature", ["", "x" * 88, "!" * 88, "a" * 89, None])
def test_bad_encoding_rejected(signature):
    assert not signer().verifier().verify_signature(content_hash=DIGEST, signature=signature)


def test_same_key_cannot_hide_behind_role_or_name():
    key = Ed25519PrivateKey.generate()
    execution, evaluation = signer(key=key), signer("evaluation", key, "signer.two")
    assert execution.signer_ref.signer_hash == evaluation.signer_ref.signer_hash
    signature = execution.sign_authority(content_hash=DIGEST)
    assert not evaluation.verifier().verify_signature(content_hash=DIGEST, signature=signature)
    other_role = FormalEd25519Verifier(key.public_key(), role="evaluation",
                                      signer_id="signer.one", key_id="key-v1")
    assert not other_role.verify_signature(content_hash=DIGEST, signature=signature)


def test_wrong_signing_method_and_digest_rejected():
    with pytest.raises(ValueError, match="Actor"):
        signer("actor").sign_authority(content_hash=DIGEST)
    with pytest.raises(ValueError, match="B/D"):
        signer().sign_assertion(content_hash=DIGEST)
    with pytest.raises(ValueError, match="canonical"):
        signer().sign_authority(content_hash="not-a-digest")
