# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Ed25519 adapters for deployment-supplied keys, not authority issuance policy."""

import base64
import hashlib
import re
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from hcuopt.contracts.formal_profile_authorization_v1 import FormalProfileGrantVerifierRef
from hcuopt.contracts.m2_formal_start_v1 import FormalStartSignerRef
from hcuopt.measurement.evidence import canonical_json_bytes

SigningRole = Literal["owner", "actor", "execution", "evaluation"]
SCHEME = "hcuopt-formal-ed25519-v1"


class FormalEd25519Verifier:
    """Public-key-only verifier; the coordinator still enforces role separation.

    Trusted deployment config selects the key, role and identity, never HTTP.
    signer_hash is the raw public-key digest so sharing a key across identities
    cannot evade existing key-separation checks by simply renaming the signer.
    """

    def __init__(self, public_key: Ed25519PublicKey, *, role: SigningRole,
                 signer_id: str, key_id: str):
        if role not in {"owner", "actor", "execution", "evaluation"}:
            raise ValueError("Unsupported Formal signing role")
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("Formal signing requires an Ed25519 key")
        self._public_key = public_key
        self._role = role
        raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self._signer_ref = FormalStartSignerRef(
            signer_id=signer_id, signer_version="1", key_id=key_id,
            signer_hash="sha256:" + hashlib.sha256(raw).hexdigest(), signature_scheme=SCHEME,
        )

    @property
    def signer_ref(self) -> FormalStartSignerRef:
        return self._signer_ref

    def _message(self, content_hash: str) -> bytes:
        if (not isinstance(content_hash, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash)):
            raise ValueError("Formal signing requires a canonical SHA256 digest")
        return canonical_json_bytes({
            "domain": SCHEME, "role": self._role,
            "signer": self.signer_ref.model_dump(mode="json"), "content_hash": content_hash,
        })

    def verify_signature(self, *, content_hash: str, signature: str) -> bool:
        try:
            if not isinstance(signature, str) or len(signature) != 88:
                return False
            raw = base64.b64decode(signature, validate=True)
            if len(raw) != 64 or base64.b64encode(raw).decode("ascii") != signature:
                return False
            self._public_key.verify(raw, self._message(content_hash))
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False


class FormalEd25519Signer:
    """Private-key adapter for a trusted signer process; never attach it to FastAPI.

    This module does not generate keys, persist secrets, select an actor, approve
    a window or mint Authority content. Existing policy issuers remain in charge.
    """

    def __init__(self, private_key: Ed25519PrivateKey, *, role: SigningRole,
                 signer_id: str, key_id: str):
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError("Formal signing requires an Ed25519 private key")
        self._private_key = private_key
        self._verifier = FormalEd25519Verifier(
            private_key.public_key(), role=role, signer_id=signer_id, key_id=key_id,
        )

    @property
    def signer_ref(self) -> FormalStartSignerRef:
        return self._verifier.signer_ref

    def verifier(self) -> FormalEd25519Verifier:
        return self._verifier

    def sign_authority(self, *, content_hash: str) -> str:
        if self._verifier._role not in {"execution", "evaluation"}:
            raise ValueError("Actor signing key cannot sign B/D Authority")
        return self._sign(content_hash)

    def sign_assertion(self, *, content_hash: str) -> str:
        if self._verifier._role != "actor":
            raise ValueError("B/D signing key cannot sign an actor assertion")
        return self._sign(content_hash)

    def _sign(self, content_hash: str) -> str:
        return base64.b64encode(
            self._private_key.sign(self._verifier._message(content_hash))
        ).decode("ascii")


class FormalOwnerEd25519Verifier(FormalEd25519Verifier):
    """Adapter for the pre-existing owner window-authorization verifier protocol."""

    def __init__(self, public_key: Ed25519PublicKey, *, verifier_id: str, key_id: str):
        super().__init__(public_key, role="owner", signer_id=verifier_id, key_id=key_id)

    @property
    def verifier_ref(self) -> FormalProfileGrantVerifierRef:
        ref = self.signer_ref
        return FormalProfileGrantVerifierRef(
            verifier_id=ref.signer_id, verifier_version=ref.signer_version,
            verifier_hash=ref.signer_hash, signature_scheme=ref.signature_scheme, key_id=ref.key_id,
        )

    def verify_signature(self, *, authorization_hash: str, signature: str) -> bool:
        return super().verify_signature(content_hash=authorization_hash, signature=signature)


class FormalOwnerEd25519Signer:
    """Sign an already decided window digest; never create or approve the decision."""

    def __init__(self, private_key: Ed25519PrivateKey, *, verifier_id: str, key_id: str):
        self._signer = FormalEd25519Signer(
            private_key, role="owner", signer_id=verifier_id, key_id=key_id,
        )
        self._verifier = FormalOwnerEd25519Verifier(
            private_key.public_key(), verifier_id=verifier_id, key_id=key_id,
        )

    @property
    def verifier_ref(self) -> FormalProfileGrantVerifierRef:
        return self._verifier.verifier_ref

    def verifier(self) -> FormalOwnerEd25519Verifier:
        return self._verifier

    def sign_authorization(self, *, authorization_hash: str) -> str:
        return self._signer._sign(authorization_hash)
