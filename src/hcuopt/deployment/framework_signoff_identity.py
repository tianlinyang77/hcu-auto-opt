# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Explicit single-task signing identity, not an approval or a release grant."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Request


@dataclass(frozen=True)
class FrameworkSignoffIdentity:
    """Deployment supplies a separately provisioned token digest and actor.

    This capability identifies possession of a task-scoped signing token, not a
    verified enterprise user. Do not derive it from read/control/model credentials.
    No persistent keys, database writes, or authority are created on import.
    """

    task_id: UUID
    actor: str
    token_sha256: str = field(repr=False)
    expires_at: datetime
    browser_origin: str = "http://127.0.0.1:4194"

    def __post_init__(self):
        if (
            not isinstance(self.task_id, UUID)
            or not isinstance(self.actor, str)
            or not self.actor.strip()
            or self.actor != self.actor.strip()
            or len(self.actor) > 200
        ):
            raise ValueError("invalid signing identity")
        if not re.fullmatch(r"[0-9a-f]{64}", self.token_sha256):
            raise ValueError("invalid signing token digest")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("signing expiry must be timezone-aware")
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", self.browser_origin):
            raise ValueError("signing origin must be explicit loopback")
        if not 1024 <= int(self.browser_origin.rsplit(":", 1)[1]) <= 65535:
            raise ValueError("invalid signing origin port")

    def __call__(self, request: Request, task_id: UUID) -> str | None:
        if task_id != self.task_id or datetime.now(timezone.utc) >= self.expires_at:
            return None
        # CLI requests may omit Origin. Browser cross-origin requests cannot write.
        origin = request.headers.get("origin")
        if origin is not None and origin != self.browser_origin:
            return None
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            return None
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        token = authorization[len("Bearer ") :]
        if not 32 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
            return None
        actual = hashlib.sha256(token.encode("ascii")).hexdigest()
        return self.actor if secrets.compare_digest(actual, self.token_sha256) else None
