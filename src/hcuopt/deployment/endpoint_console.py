# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
"""Managed local endpoint result console; no database or device access."""

import json
import secrets
import threading
import time
from pathlib import Path
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from hcuopt.deployment.viewer_service import (
    ViewerServiceError,
    database_endpoint,
    default_instance_parent,
    private_instance,
    write_record,
)


class ConsoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: UUID
    static_root: Path
    port: int = Field(default=4196, ge=1024, le=65535)
    ssh_target: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.@-]+$")
    ssh_remote_port: int = Field(default=18012, ge=1024, le=65535)


def load_config(path: Path) -> ConsoleConfig:
    config = ConsoleConfig.model_validate_json(path.read_text(encoding="utf-8"))
    if config.ssh_target and config.ssh_target.startswith("-"):
        raise ValueError("invalid SSH target")
    if not config.static_root.is_absolute():
        config.static_root = (path.absolute().parent / config.static_root).resolve()
    if not (config.static_root / "index.html").is_file():
        raise ValueError("frontend build missing; run npm ci and npm run build in web")
    return config


def read_summary(client: httpx.Client, campaign_id: UUID) -> dict:
    response = client.get(f"/v1/endpoint-validation-campaigns/{campaign_id}/summary")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("campaign"), dict):
        raise ValueError("invalid campaign summary")
    campaign = data.get("campaign", {})
    if (
        campaign.get("campaign_id") != str(campaign_id)
        or campaign.get("automatic_release_allowed") is not False
        or data.get("automatic_release_allowed") is not False
    ):
        raise ValueError("campaign identity or release boundary mismatch")
    return data


def create_app(config, client, instance, control_key, stop):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    origin = f"http://127.0.0.1:{config.port}"

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host") != f"127.0.0.1:{config.port}":
            from fastapi.responses import Response
            return Response(status_code=403)
        if request.headers.get("origin") not in {None, origin}:
            from fastapi.responses import Response
            return Response(status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Referrer-Policy": "no-referrer"})
        return response

    def authorize(request: Request, requested_instance: UUID):
        if requested_instance != instance or not secrets.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + control_key).encode(),
        ):
            raise HTTPException(403, "Instance control credential required")

    @app.get("/v1/viewer-control/{requested_instance}")
    def status(requested_instance: UUID, request: Request):
        authorize(request, requested_instance)
        try:
            read_summary(client, config.campaign_id)
            upstream = "available"
        except (httpx.HTTPError, ValueError, TypeError):
            upstream = "unavailable"
        return {"instance_id": str(instance), "task_id": str(config.campaign_id),
                "status": "running", "upstream": upstream, "read_only": True}

    @app.post("/v1/viewer-control/{requested_instance}/stop")
    def shutdown(requested_instance: UUID, request: Request):
        authorize(request, requested_instance)
        stop()
        return {"instance_id": str(instance), "status": "stopping"}

    @app.get("/v1/endpoint-validation-campaigns/{requested_campaign}/summary")
    def summary(requested_campaign: UUID):
        if requested_campaign != config.campaign_id:
            raise HTTPException(403, "Campaign outside this console")
        try:
            return read_summary(client, config.campaign_id)
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(503, "控制面暂时不可用，请检查 VPN 和远端 API") from None

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def unavailable(path: str):
        raise HTTPException(403, "此入口仅查看已登记的 Campaign，未开放写操作")

    @app.get("/")
    def landing(request: Request):
        if request.query_params.get("endpointCampaign") != str(config.campaign_id):
            return RedirectResponse(f"/?endpointCampaign={config.campaign_id}")
        from fastapi.responses import FileResponse
        return FileResponse(config.static_root / "index.html")

    app.mount("/", StaticFiles(directory=config.static_root), name="frontend")
    return app


def serve(config: ConsoleConfig, *, instance_parent: Path | None = None) -> int:
    directory = private_instance(instance_parent or default_instance_parent())
    instance = UUID(directory.name)
    key_path = directory / "control-key.txt"
    record = {"instance_id": str(instance), "task_id": str(config.campaign_id),
              "port": config.port, "status": "starting", "kind": "endpoint-console"}
    stopped = threading.Event()
    server = thread = None

    def stop():
        stopped.set()
        if server is not None:
            server.should_exit = True

    try:
        key = secrets.token_urlsafe(32)
        key_path.write_text(key, encoding="utf-8")
        write_record(directory / "instance.json", record)
        with database_endpoint(config, before_close=stop) as (port, alive):
            with httpx.Client(base_url=f"http://127.0.0.1:{port or config.ssh_remote_port}",
                              trust_env=False, follow_redirects=False, timeout=5) as client:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        read_summary(client, config.campaign_id)
                        break
                    except httpx.TransportError:
                        if not alive() or time.monotonic() >= deadline:
                            raise ViewerServiceError("console_upstream_unavailable") from None
                        time.sleep(0.25)
                app = create_app(config, client, instance, key, stop)
                server = uvicorn.Server(uvicorn.Config(
                    app, host="127.0.0.1", port=config.port, access_log=False,
                    log_level="critical", timeout_graceful_shutdown=10,
                ))

                def run():
                    try:
                        server.run()
                    except SystemExit:
                        pass

                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() >= deadline:
                        raise ViewerServiceError("console_port_unavailable")
                    time.sleep(0.05)
                record.update(status="running", url=f"http://127.0.0.1:{config.port}/"
                              f"?endpointCampaign={config.campaign_id}")
                write_record(directory / "instance.json", record)
                print(json.dumps({**record, "instance_directory": str(directory)}), flush=True)
                try:
                    while not stopped.wait(0.25):
                        if not alive() or not thread.is_alive():
                            raise ViewerServiceError("console_or_tunnel_stopped")
                finally:
                    stop()
                    thread.join(timeout=15)
                    if thread.is_alive():
                        raise ViewerServiceError("console_shutdown_incomplete")
                record["status"] = "stopped"
    except KeyboardInterrupt:
        record["status"] = "stopped"
    except Exception:
        record["status"] = "failed"
        raise
    finally:
        stop()
        if thread:
            thread.join(timeout=15)
            if thread.is_alive():
                record["status"] = "failed"
        try:
            key_path.unlink(missing_ok=True)
        finally:
            write_record(directory / "instance.json", record)
    if record["status"] != "stopped":
        raise ViewerServiceError("console_shutdown_incomplete")
    return 0
