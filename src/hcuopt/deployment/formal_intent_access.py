# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Materialize browser access from existing signed authority, never mint authority."""

import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from hcuopt.contracts.m2_formal_start_v1 import (
    FormalStartIntentRequest,
    formal_start_request_subject_digest,
)
from hcuopt.deployment.viewer_service import private_instance
from hcuopt.operator.formal_start import DeploymentFormalStartSignatureVerifier


@dataclass(frozen=True)
class FormalIntentAccessFiles:
    directory: Path
    configuration: Path
    credential: Path


def materialize_formal_intent_access(
    request: FormalStartIntentRequest,
    *,
    expected_actor_id: str,
    verifier: DeploymentFormalStartSignatureVerifier,
    now: datetime,
    private_parent: Path,
) -> FormalIntentAccessFiles:
    """Deployment-only: verify before creating files; never print the credential.

    The trusted caller supplies the actor identity and its allowlisted verifier.
    Browser input cannot call this function or select the verifier. B/D checks
    remain the coordinator's responsibility at submission time.
    """
    try:
        # Reparse even typed objects, since model_copy can bypass validation.
        frozen = FormalStartIntentRequest.model_validate(request.model_dump(mode="json"))
        assertion = frozen.actor_assertion
        if (
            now.tzinfo is None
            or now.utcoffset() is None
            or not expected_actor_id.strip()
            or assertion.actor_id != expected_actor_id
            or assertion.action != "create"
            or assertion.subject_digest != formal_start_request_subject_digest(frozen)
            or not assertion.issued_at <= now < assertion.expires_at
            or verifier.signer_ref != assertion.signer
            or verifier.verify_signature(
                content_hash=assertion.assertion_hash, signature=assertion.signature
            )
            is not True
        ):
            raise ValueError("invalid authority")
    except Exception:
        raise ValueError("Formal access authority was rejected") from None

    # Existing helper applies owner-only permissions before secrets are created.
    directory = private_instance(private_parent)
    configuration = directory / "capabilities.json"
    credential = directory / "access-key.txt"
    token = secrets.token_urlsafe(32)
    bundle = {
        "schema_version": "formal-intent-capabilities-v1",
        "capabilities": [
            {
                "token_sha256": sha256(token.encode("ascii")).hexdigest(),
                "assertion": assertion.model_dump(mode="json"),
                "submission": frozen.model_dump(mode="json", exclude={"actor_assertion"}),
            }
        ],
    }
    try:
        with credential.open("x", encoding="utf-8") as output:
            output.write(token)
        temporary = directory / "capabilities.pending"
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(bundle, output, ensure_ascii=False)
        # Publish the usable configuration last. Partial writes are not loadable.
        temporary.replace(configuration)
    except Exception:
        # This is a new owner-only directory, never an existing deployment.
        # Leave partial files for explicit cleanup; do not log their contents.
        raise ValueError("Formal access files were not fully published") from None
    return FormalIntentAccessFiles(directory, configuration, credential)
