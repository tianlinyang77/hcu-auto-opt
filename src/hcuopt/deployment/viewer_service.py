# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Single-user local viewer lifecycle. Optional signing; no HCU, migration or release."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
import psycopg
import uvicorn
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field

from hcuopt.contracts.v1 import FrameworkSmokeSummary
from hcuopt.deployment.framework_signoff_identity import (
    FrameworkSigningSession,
    FrameworkSignoffIdentity,
)
from hcuopt.deployment.framework_smoke_viewer import create_viewer
from hcuopt.deployment.framework_viewer_signing import ViewerSigning
from hcuopt.storage.repository import PostgresRepository


class ViewerServiceError(ValueError):
    """Public errors must never include DSNs or credential contents."""


class SigningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")
    ttl_seconds: int = Field(default=1800, ge=60, le=3600)


class ViewerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    task_id: UUID
    database_schema: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    adapter_profile: Literal["bw20-framework-smoke-v1"] = "bw20-framework-smoke-v1"
    static_root: Path
    port: int = Field(default=4194, ge=1024, le=65535)
    ssh_target: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.]+@[a-zA-Z0-9_.-]+$")
    ssh_remote_port: int = Field(default=55433, ge=1, le=65535)
    signing: SigningConfig | None = None


def load_config(path: Path) -> ViewerConfig:
    try:
        config = ViewerConfig.model_validate_json(path.read_text(encoding="utf-8"))
        if not config.static_root.is_absolute():
            config.static_root = Path(os.path.abspath(path.absolute().parent / config.static_root))
        return config
    except (OSError, ValueError) as exc:
        raise ViewerServiceError("invalid_viewer_config") from exc


def readonly_dsn(value: str, schema: str, port: int | None = None) -> str:
    """Only explicit loopback connections; discard environment/libpq option surprises."""
    try:
        fields = conninfo_to_dict(value)
        allowed = {"host", "port", "user", "password", "dbname", "sslmode"}
        if (
            not value
            or set(fields) - allowed
            or fields.get("host") != "127.0.0.1"
            or not all(fields.get(key) for key in ("port", "user", "password", "dbname"))
        ):
            raise ValueError("invalid")
        # Validate even when called outside config parsing.
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid")
        if port is not None:
            fields["port"] = str(port)
        if not 1 <= int(fields["port"]) <= 65535:
            raise ValueError("invalid")
        return make_conninfo(
            **fields,
            hostaddr="127.0.0.1",
            connect_timeout=5,
            options=(
                f"-c search_path={schema} -c default_transaction_read_only=on "
                "-c statement_timeout=10000 -c lock_timeout=5000"
            ),
        )
    except (ValueError, psycopg.Error) as exc:
        raise ViewerServiceError("invalid_loopback_database_config") from exc


def private_instance(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    if parent.resolve() != parent.absolute():
        raise ViewerServiceError("redirected_instance_parent")
    directory = parent / str(uuid4())
    directory.mkdir(mode=0o700)
    if os.name == "nt":
        # Protect directory before generating any credential; do not change parent ACLs.
        principal = os.environ["USERDOMAIN"] + "\\" + os.environ["USERNAME"]
        result = subprocess.run(
            ["icacls", str(directory), "/inheritance:r", "/grant:r", principal + ":(OI)(CI)F"],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            directory.rmdir()  # This newly created directory contains no secrets/files.
            raise ViewerServiceError("private_directory_acl_failed")
    else:
        directory.chmod(0o700)
    return directory


def default_instance_parent() -> Path:
    if os.name == "nt":
        # Packaged Windows applications may virtualize LocalAppData. Select its
        # concrete private location before creation; explicit parents remain strict.
        return (Path(os.environ["LOCALAPPDATA"]) / "hcuopt" / "managed-viewers").resolve()
    return (Path.home() / ".local" / "state" / "hcuopt" / "managed-viewers").resolve()


def write_record(path: Path, record: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def database_endpoint(config: ViewerConfig, *, before_close: Callable[[], None] = lambda: None):
    """Own only the optional SSH process created here; never attach/kill existing tunnels."""
    if not config.ssh_target:
        try:
            yield None, lambda: True
        finally:
            before_close()
        return
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            "ssh",
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-L",
            f"127.0.0.1:{port}:127.0.0.1:{config.ssh_remote_port}",
            config.ssh_target,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        yield port, lambda: process.poll() is None
    finally:
        # Revoke access BEFORE potentially slow tunnel shutdown, including errors.
        before_close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()  # Only our retained Popen, never a discovered PID.
                process.wait(timeout=5)


def make_reader(config: ViewerConfig, dsn: str):
    repository = PostgresRepository(dsn)

    def read():
        raw = repository.framework_smoke_summary(config.task_id)
        if raw["task"]["adapter_profile"] != config.adapter_profile:
            raise ViewerServiceError("unexpected_task_profile")
        return FrameworkSmokeSummary.model_validate({**raw, "adapter_mode": "real"})

    return read


def signing_dsn(value: str, config: ViewerConfig, read_value: str, port: int | None = None) -> str:
    """Separate, explicitly supplied write connection to the same frozen database.

    The viewer read connection remains read-only. No role grants or migration here.
    """
    validated = conninfo_to_dict(readonly_dsn(value, config.database_schema, port))
    read_fields = conninfo_to_dict(readonly_dsn(read_value, config.database_schema, port))
    if any(validated[key] != read_fields[key] for key in ("host", "port", "dbname")):
        raise ViewerServiceError("signing_database_mismatch")
    validated["options"] = (
        f"-c search_path={config.database_schema} -c default_transaction_read_only=off "
        "-c statement_timeout=10000 -c lock_timeout=5000"
    )
    return make_conninfo(**validated)


def serve(config: ViewerConfig, database_dsn: str, *, instance_parent: Path | None = None,
          signing_database_dsn: str | None = None) -> int:
    # Reject DSN/config before any process, credentials or listening socket are created.
    readonly_dsn(database_dsn, config.database_schema)
    if (config.signing is None) != (signing_database_dsn is None):
        raise ViewerServiceError("signing_requires_config_and_separate_dsn")
    if config.signing is not None:
        signing_dsn(signing_database_dsn, config, database_dsn)
    if (
        config.static_root.resolve(strict=True) != config.static_root.absolute()
        or not (config.static_root / "index.html").is_file()
    ):
        raise ViewerServiceError("invalid_static_build")
    directory = private_instance(instance_parent or default_instance_parent())
    instance = UUID(directory.name)
    record = {
        "instance_id": str(instance),
        "task_id": str(config.task_id),
        "port": config.port,
        "status": "starting",
    }
    server = thread = None
    stopped = threading.Event()
    access = directory / "access-key.txt"
    control = directory / "control-key.txt"
    signing_key = directory / "signing-key.txt"
    signing = None

    def revoke_signing():
        if signing is not None:
            signing.session.revoke()
        signing_key.unlink(missing_ok=True)

    def request_shutdown():
        stopped.set()
        if signing is not None:
            signing.session.revoke()
        if server:
            server.should_exit = True

    try:
        access.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        control.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        write_record(directory / "instance.json", record)
        with database_endpoint(config, before_close=request_shutdown) as (port, alive):
            dsn = readonly_dsn(database_dsn, config.database_schema, port)
            reader = make_reader(config, dsn)
            deadline = time.monotonic() + 20
            while True:
                if not alive():
                    raise ViewerServiceError("database_tunnel_stopped")
                try:
                    reader()  # No schema migration or writes, even on first deployment.
                    break
                except (psycopg.OperationalError, psycopg.InterfaceError):
                    if time.monotonic() >= deadline:
                        raise ViewerServiceError("database_unavailable") from None
                    time.sleep(0.25)
            if config.signing is not None:
                token = secrets.token_urlsafe(32)
                identity = FrameworkSignoffIdentity(
                    task_id=config.task_id, actor=config.signing.actor,
                    token_sha256=hashlib.sha256(token.encode("ascii")).hexdigest(),
                    expires_at=datetime.now(timezone.utc) + timedelta(
                        seconds=config.signing.ttl_seconds),
                    browser_origin=f"http://127.0.0.1:{config.port}",
                )
                write_repository = PostgresRepository(
                    signing_dsn(signing_database_dsn, config, database_dsn, port))
                read_repository = PostgresRepository(dsn)
                signing = ViewerSigning(FrameworkSigningSession(identity),
                                        write_repository.signoff_framework_task,
                                        read_repository.framework_signoff)
                signing_key.write_text(token, encoding="utf-8")
                del token
                record["signing_actor"] = identity.actor
                record["signing_expires_at"] = identity.expires_at.isoformat()
                record["signing_credential_file"] = str(signing_key)
            app = create_viewer(
                task_id=config.task_id,
                credential=access.read_text(),
                read_summary=reader,
                static_root=config.static_root,
                control=(instance, control.read_text(), request_shutdown),
                access_active=lambda: not stopped.is_set(),
                signing=signing,
                revoke_signing=revoke_signing if signing else None,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=config.port,
                    access_log=False,
                    log_level="critical",
                    timeout_graceful_shutdown=15,
                )
            )

            def run_server():
                try:
                    server.run()
                except SystemExit:
                    # Uvicorn reports bind failure as SystemExit in its server thread.
                    # The startup watchdog below handles it and revokes credentials.
                    return

            thread = threading.Thread(target=run_server, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise ViewerServiceError("viewer_start_failed")
                time.sleep(0.05)
            url = f"http://127.0.0.1:{config.port}"
            with httpx.Client(trust_env=False, timeout=15) as client:
                path = url + f"/v1/framework-smoke-inspection/{config.task_id}"
                if client.get(path).status_code != 401:
                    raise ViewerServiceError("viewer_auth_selfcheck_failed")
                response = client.get(
                    path, headers={"Authorization": "Bearer " + access.read_text()}
                )
                if response.status_code != 200 or client.get(url).status_code != 200:
                    raise ViewerServiceError("viewer_read_selfcheck_failed")
            record.update(status="running", url=url + f"/?frameworkSmoke={config.task_id}")
            write_record(directory / "instance.json", record)
            print(
                json.dumps(
                    {**record, "instance_directory": str(directory), "credential_file": str(access)}
                ),
                flush=True,
            )
            while not stopped.wait(0.25):
                if not alive() or not thread.is_alive():
                    raise ViewerServiceError("viewer_or_tunnel_stopped")
            server.should_exit = True
            thread.join(timeout=20)
            if thread.is_alive():
                raise ViewerServiceError("viewer_shutdown_incomplete")
            record["status"] = "stopped"
    except KeyboardInterrupt:
        record["status"] = "stopped"
    except Exception as exc:
        record["status"] = "failed"
        raise ViewerServiceError("viewer_deployment_failed") from exc
    finally:
        request_shutdown()
        if thread:
            thread.join(timeout=20)
            if thread.is_alive():
                record["status"] = "failed"
        # Revoke this instance's files on normal/failure exit. No recursive deletion.
        cleanup_failed = False
        for credential in (access, control, signing_key):
            try:
                credential.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
                record["status"] = "failed"
        try:
            write_record(directory / "instance.json", record)
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            raise ViewerServiceError("viewer_cleanup_incomplete") from None
    if record["status"] != "stopped":
        raise ViewerServiceError("viewer_shutdown_incomplete")
    return 0


def control_instance(directory: Path, action: Literal["status", "stop", "revoke-signing"]) -> dict:
    """Authenticated instance-specific HTTP; no PID/port-name based termination."""
    try:
        if (
            action not in {"status", "stop", "revoke-signing"}
            or directory.resolve(strict=True) != directory.absolute()
        ):
            raise ValueError("invalid")
        record = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
        instance = UUID(record["instance_id"])
        if str(instance) != directory.name or type(record["port"]) is not int:
            raise ValueError("invalid")
        port = record["port"]
        if not 1024 <= port <= 65535:
            raise ValueError("invalid")
        if record["status"] in {"stopped", "failed"}:
            return {
                "instance_id": str(instance),
                "status": record["status"],
                "source": "local_record",
            }
        key = (directory / "control-key.txt").read_text(encoding="utf-8")
        url = f"http://127.0.0.1:{port}/v1/viewer-control/{instance}"
        with httpx.Client(trust_env=False, timeout=10, follow_redirects=False) as client:
            headers = {"Authorization": "Bearer " + key}
            # Validate live identity BEFORE stop, even if an unrelated server reused the port.
            response = client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            if data.get("instance_id") != str(instance) or data.get("task_id") != record["task_id"]:
                raise ValueError("invalid")
            if action in {"stop", "revoke-signing"}:
                response = client.post(url + "/" + action, headers=headers)
                response.raise_for_status()
                data = response.json()
                if data.get("instance_id") != str(instance):
                    raise ValueError("invalid")
            return data
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        raise ViewerServiceError("instance_control_unavailable") from exc
