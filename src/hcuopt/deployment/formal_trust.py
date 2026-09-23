# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Load administrator-selected public trust configuration; never load private keys."""

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from hcuopt.contracts.formal_profile_authorization_v1 import FormalProfileWindowAuthorization
from hcuopt.deployment.formal_signing import (
    FormalEd25519Verifier,
    FormalOwnerEd25519Verifier,
)
from hcuopt.measurement.m2_formal_receipt import _read_regular
from hcuopt.operator.formal_start import FormalStartCoordinator


class _Key(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    identity_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,199}$")
    key_id: str = Field(min_length=1, max_length=300)
    public_key_base64: str = Field(min_length=44, max_length=44)

    def public_key(self) -> Ed25519PublicKey:
        raw = base64.b64decode(self.public_key_base64, validate=True)
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != self.public_key_base64:
            raise ValueError("Noncanonical public key")
        return Ed25519PublicKey.from_public_bytes(raw)


class _Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["formal-public-trust-v1"]
    owner: _Key
    actor: _Key
    execution: _Key
    evaluation: _Key


@dataclass(frozen=True)
class FormalPublicTrust:
    owner: FormalOwnerEd25519Verifier
    actor: FormalEd25519Verifier
    execution: FormalEd25519Verifier
    evaluation: FormalEd25519Verifier

    def __post_init__(self) -> None:
        for role in ("owner", "actor", "execution", "evaluation"):
            verifier = getattr(self, role)
            if not isinstance(verifier, FormalEd25519Verifier) or verifier._role != role:
                raise ValueError("Formal trust verifier role differs")
        refs = [item.signer_ref for item in
                (self.owner, self.actor, self.execution, self.evaluation)]
        if (len({ref.signer_hash for ref in refs}) != 4
                or len({ref.signer_id for ref in refs}) != 4):
            raise ValueError("Formal owner/actor/B/D identities and keys must be separate")

    @classmethod
    def from_file(cls, *, deployment_root: Path, path: Path) -> "FormalPublicTrust":
        """Startup snapshot; protect root/parents; changes require service restart.

        No request-controlled path, remote key discovery, trust-on-first-use or
        fallback to a fixture verifier. Unknown/private-key fields are rejected.
        """
        try:
            bundle = _Bundle.model_validate_json(_read_regular(deployment_root, path, 16 * 1024))
            owner = FormalOwnerEd25519Verifier(bundle.owner.public_key(),
                                               verifier_id=bundle.owner.identity_id,
                                               key_id=bundle.owner.key_id)
            others = {
                role: FormalEd25519Verifier(getattr(bundle, role).public_key(), role=role,
                                            signer_id=getattr(bundle, role).identity_id,
                                            key_id=getattr(bundle, role).key_id)
                for role in ("actor", "execution", "evaluation")
            }
            return cls(owner=owner, **others)
        except Exception:
            raise ValueError(
                "Formal public trust configuration is invalid or unavailable"
            ) from None

    def coordinator(self, compiler, *, object_store, clock=None) -> FormalStartCoordinator:
        """Recheck owner cryptography; do not replace Profile/readiness admission.

        The compiler must already be built through the existing verified Profile
        path using this owner verifier. B/D issuers also receive this same owner
        verifier. No authorization content is generated or changed here.
        """
        authorization = FormalProfileWindowAuthorization.model_validate(
            compiler.authorization.model_dump(mode="json")
        )
        if (authorization.verifier != self.owner.verifier_ref
                or not self.owner.verify_signature(
                    authorization_hash=authorization.authorization_hash,
                    signature=authorization.signature,
                )):
            raise ValueError("Formal owner authorization does not match deployment trust")
        return FormalStartCoordinator(
            compiler, object_store=object_store, actor_verifier=self.actor,
            execution_verifier=self.execution, evaluation_verifier=self.evaluation, clock=clock,
        )
