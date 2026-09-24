# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in, non-executing Formal intent management; never a worker dispatcher."""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from hcuopt.contracts.m2_formal_start_v1 import (
    FormalStartActorAssertion,
    FormalStartIntentRequest,
    FormalStartIntentResult,
    FrozenFormalStartModel,
    formal_start_content_hash,
)
from hcuopt.contracts.operator_v1 import OperatorServiceIdentityAssertion
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.measurement.m2_formal_receipt import _read_regular
from hcuopt.operator.formal_start import FormalStartCoordinator


class FormalIntentSubmission(FrozenFormalStartModel):
    """Frozen references only. Identity and signatures come from deployment."""

    preview_id: UUID
    resolved_plan_hash: str = Field(pattern=SHA256_PATTERN)
    formal_authorization_hash: str = Field(pattern=SHA256_PATTERN)
    execution_authority_hash: str = Field(pattern=SHA256_PATTERN)
    evaluation_authority_hash: str = Field(pattern=SHA256_PATTERN)
    idempotency_key: str = Field(min_length=8, max_length=300)
    expected_service_identity: OperatorServiceIdentityAssertion


@dataclass(frozen=True, repr=False)
class FormalIntentCapability:
    """Deployment-owned credential digest and immutable, pre-signed assertion.

    Use a dedicated random token (at least 32 random bytes), not a model API key.
    Persist this binding outside the browser to survive service restarts. Issuance
    and revocation belong to deployment, not this HTTP endpoint.
    """

    token_sha256: str
    assertion: FormalStartActorAssertion
    submission: FormalIntentSubmission | None = None

    def __post_init__(self) -> None:
        if len(self.token_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.token_sha256
        ):
            raise ValueError("Formal capability requires a SHA256 credential digest")
        if self.assertion.action != "create":
            raise ValueError("Formal create capability requires a create assertion")
        if self.submission is not None and formal_start_content_hash(self.submission) != (
            self.assertion.subject_digest
        ):
            raise ValueError("Formal submission does not match its signed scope")


@dataclass(frozen=True)
class FormalStartManagement:
    coordinator: FormalStartCoordinator
    capabilities: tuple[FormalIntentCapability, ...]

    def __post_init__(self) -> None:
        digests = [item.token_sha256 for item in self.capabilities]
        if len(digests) != len(set(digests)):
            raise ValueError("Formal capability credentials must be unique")

    @classmethod
    def from_file(
        cls, coordinator: FormalStartCoordinator, *, deployment_root: Path, path: Path
    ) -> "FormalStartManagement":
        """Load only administrator-selected paths; errors never echo file content.

        The deployment root and its parents must be administrator-controlled.
        This is a startup snapshot: replacing the file requires an app restart.
        """
        try:
            raw = _read_regular(deployment_root, path, 512 * 1024)
            bundle = _CapabilityBundle.model_validate_json(raw)
            return cls(
                coordinator,
                tuple(
                    FormalIntentCapability(item.token_sha256, item.assertion, item.submission)
                    for item in bundle.capabilities
                ),
            )
        except Exception:
            raise ValueError("Formal capability configuration is unavailable or invalid") from None

    def router(self) -> APIRouter:
        router = APIRouter()

        def authenticate(request: Request) -> FormalIntentCapability:
            headers = request.headers.getlist("authorization")
            value = headers[0] if len(headers) == 1 else ""
            scheme, _, token = value.partition(" ")
            if scheme.lower() != "bearer" or not token or len(token) > 4096:
                raise _rejected()
            digest = sha256(token.encode("utf-8")).hexdigest()
            capability = next(
                (item for item in self.capabilities if compare_digest(item.token_sha256, digest)),
                None,
            )
            if capability is None:
                raise _rejected()
            return capability

        @router.get("/v1/operator/formal-start-submission", response_model=FormalIntentSubmission)
        def prepare(request: Request, response: Response) -> FormalIntentSubmission:
            capability = authenticate(request)
            now = self.coordinator.clock()
            if not capability.assertion.issued_at <= now < capability.assertion.expires_at:
                raise _rejected()
            if capability.submission is None:
                raise HTTPException(503, "Formal submission is not configured")
            # Read-only preparation is not an Authority verdict. POST revalidates
            # all signatures, profiles and authorities through the coordinator.
            response.headers["Cache-Control"] = "no-store"
            return capability.submission

        @router.post(
            "/v1/operator/formal-start-intents",
            response_model=FormalStartIntentResult,
        )
        def create_intent(
            payload: FormalIntentSubmission, request: Request, response: Response
        ) -> FormalStartIntentResult:
            # Explicit header only: cookies/query parameters cannot authenticate.
            capability = authenticate(request)
            if capability.assertion.subject_digest != (formal_start_content_hash(payload)):
                raise _rejected()
            signed = FormalStartIntentRequest(
                **payload.model_dump(), actor_assertion=capability.assertion
            )
            # The existing coordinator rechecks signature, expiry, service,
            # immutable references and B/D authorities before any accepted write.
            response.headers["Cache-Control"] = "no-store"
            return self.coordinator.create(signed, request.app.state.repository)

        return router


def _rejected() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail="Formal Start capability was rejected",
        headers={"Cache-Control": "no-store"},
    )


class _CapabilityRecord(FrozenFormalStartModel):
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assertion: FormalStartActorAssertion
    submission: FormalIntentSubmission | None = None


class _CapabilityBundle(FrozenFormalStartModel):
    schema_version: Literal["formal-intent-capabilities-v1"]
    capabilities: tuple[_CapabilityRecord, ...] = Field(max_length=100)
