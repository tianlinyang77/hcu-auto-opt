# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Expiring single-reader credentials, scoped to explicit generation Runs."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Request
from fastapi.security import HTTPBasicCredentials
from pydantic import AwareDatetime, Field

from hcuopt.contracts.base import ContractModel
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.measurement.evidence import canonical_json_bytes


class RunReadAccess(ContractModel):
    schema_version: Literal["agent-inspection-access-v1"] = "agent-inspection-access-v1"
    username: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    password_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)
    expires_at: AwareDatetime


def _private_directory(path: Path) -> None:
    if os.name != "posix":
        raise ValueError("inspection credentials require POSIX private file permissions")
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError("credential directory must be owned by this user with mode 0700")


class FileRunReadAccess:
    """Reload private configuration on every request; no token or decision cache."""

    def __init__(self, path: Path, *, clock=None) -> None:
        self.path = path.absolute()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def authorize(self, credentials: HTTPBasicCredentials, run_id: UUID) -> bool:
        _private_directory(self.path.parent)
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("credential configuration must be an owner-only regular file")
        # Reuse the native no-symlink/no-special-file reader; no Windows fallback.
        raw = HashedEvidenceReader(self.path.parent, max_bytes=16_384)._secure_read(self.path)
        access = RunReadAccess.model_validate_json(raw)
        username_ok = hmac.compare_digest(credentials.username.encode(), access.username.encode())
        password_ok = hmac.compare_digest(
            hashlib.sha256(credentials.password.encode()).hexdigest(), access.password_sha256
        )
        return (
            username_ok
            and password_ok
            and run_id in access.run_ids
            and self.clock() < access.expires_at
        )


def require_private_transport(request: Request) -> None:
    # HTTP is acceptable only across a loopback connection, e.g. an SSH forward.
    # Never trust X-Forwarded-* here; the deployment controls trusted proxy settings.
    if request.url.scheme == "https":
        return
    if (
        request.client is not None
        and request.client.host in {"127.0.0.1", "::1"}
        and request.url.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        return
    raise ValueError("inspection requires HTTPS or a loopback connection")


def provision_run_read_access(
    access_file: Path,
    credential_file: Path,
    *,
    run_ids: tuple[UUID, ...],
    username: str = "operator",
    ttl_seconds: int = 3600,
) -> RunReadAccess:
    """Write a new credential exactly once; never print or overwrite its secret."""
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 86_400:
        raise ValueError("credential lifetime must be between 60 seconds and 24 hours")
    access_file, credential_file = access_file.absolute(), credential_file.absolute()
    for path in (access_file, credential_file):
        _private_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise ValueError("credential output already exists; use a new private path")
    if access_file == credential_file:
        raise ValueError("credential outputs must be distinct")
    password = secrets.token_urlsafe(32)
    access = RunReadAccess(
        username=username,
        password_sha256=hashlib.sha256(password.encode()).hexdigest(),
        run_ids=run_ids,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )
    outputs = (
        (credential_file, f"username: {username}\npassword: {password}\n".encode()),
        (access_file, canonical_json_bytes(access)),
    )
    for path, payload in outputs:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    return access
