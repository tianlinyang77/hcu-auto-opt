# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Deployment-owned, Campaign-scoped human signing capability."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Request


@dataclass(frozen=True)
class EndpointCampaignSignoffIdentity:
    """Authorize one actor to sign one Campaign until an explicit expiry."""

    campaign_id: UUID
    actor: str
    token_sha256: str = field(repr=False)
    expires_at: datetime
    browser_origin: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.campaign_id, UUID)
            or not isinstance(self.actor, str)
            or not self.actor.strip()
            or self.actor != self.actor.strip()
            or len(self.actor) > 200
        ):
            raise ValueError("invalid Endpoint Campaign signing identity")
        if not re.fullmatch(r"[0-9a-f]{64}", self.token_sha256):
            raise ValueError("invalid Endpoint Campaign signing token digest")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("Endpoint Campaign signing expiry must be timezone-aware")
        if not re.fullmatch(r"https?://[^/\s]+", self.browser_origin):
            raise ValueError("invalid Endpoint Campaign signing browser origin")
        if self.browser_origin.startswith("http://") and not re.fullmatch(
            r"http://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}",
            self.browser_origin,
        ):
            raise ValueError("HTTP signing origin must be explicit loopback")
        if ":" in self.browser_origin.rsplit("//", 1)[1]:
            try:
                port = int(self.browser_origin.rsplit(":", 1)[1])
            except ValueError as exc:
                raise ValueError("invalid Endpoint Campaign signing origin port") from exc
            if not 1 <= port <= 65535:
                raise ValueError("invalid Endpoint Campaign signing origin port")

    def __call__(self, request: Request, campaign_id: UUID) -> str | None:
        if campaign_id != self.campaign_id or datetime.now(timezone.utc) >= self.expires_at:
            return None
        origin = request.headers.get("origin")
        if origin is not None and origin != self.browser_origin:
            return None
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            return None
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        token = authorization[len("Bearer ") :]
        if (
            not 32 <= len(token) <= 256
            or not token.isascii()
            or any(character.isspace() for character in token)
        ):
            return None
        actual = hashlib.sha256(token.encode("ascii")).hexdigest()
        return self.actor if secrets.compare_digest(actual, self.token_sha256) else None


def endpoint_campaign_signoff_identity_from_env(
) -> EndpointCampaignSignoffIdentity | None:
    """Load an optional identity without ever accepting the plaintext token."""

    names = {
        "campaign_id": "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_CAMPAIGN_ID",
        "actor": "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ACTOR",
        "token_sha256": "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_TOKEN_SHA256",
        "expires_at": "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_EXPIRES_AT",
        "browser_origin": "HCUOPT_ENDPOINT_CAMPAIGN_SIGNING_ORIGIN",
    }
    values = {key: os.getenv(name) for key, name in names.items()}
    if all(value is None for value in values.values()):
        return None
    if any(value is None for value in values.values()):
        raise RuntimeError("Endpoint Campaign signing environment is incomplete")
    try:
        expires_at = datetime.fromisoformat(values["expires_at"].replace("Z", "+00:00"))
        return EndpointCampaignSignoffIdentity(
            campaign_id=UUID(values["campaign_id"]),
            actor=values["actor"],
            token_sha256=values["token_sha256"],
            expires_at=expires_at,
            browser_origin=values["browser_origin"],
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Endpoint Campaign signing environment is invalid") from exc


__all__ = [
    "EndpointCampaignSignoffIdentity",
    "endpoint_campaign_signoff_identity_from_env",
]
