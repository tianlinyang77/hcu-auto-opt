# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Dedicated read-only inspection app. No control-plane routes or migrations."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from hcuopt.api.inspection_access import (
    FileRunReadAccess,
    provision_run_read_access,
    require_private_transport,
)
from hcuopt.domain.errors import NotFound, SourceArtifactError
from hcuopt.evaluation.agent_generation_inspection import (
    AgentGenerationInspection,
    AgentGenerationInspectionService,
)
from hcuopt.evaluation.agent_generation_read_model import AgentGenerationReadModelError
from hcuopt.storage.repository import PostgresRepository


def create_inspection_app(*, repository, evidence_root: Path, access: FileRunReadAccess) -> FastAPI:
    application = FastAPI(
        title="HCU Agent Inspection (read-only)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    basic = HTTPBasic(auto_error=False)
    basic_dependency = Depends(basic)

    @application.middleware("http")
    async def protect_response_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.get("/healthz")
    def health():
        # Liveness only: deliberately does not touch DB, credentials or Evidence.
        return {"status": "ok", "scope": "read_only_liveness_not_readiness"}

    @application.get(
        "/v1/operator/agent-generations/{generation_run_id}/inspection",
        response_model=AgentGenerationInspection,
    )
    def inspection(
        generation_run_id: UUID,
        request: Request,
        credentials: HTTPBasicCredentials | None = basic_dependency,
    ):
        try:
            require_private_transport(request)
        except ValueError:
            raise HTTPException(403, "Inspection requires a private transport") from None
        if credentials is None:
            raise HTTPException(
                401,
                "Inspection login required",
                headers={"WWW-Authenticate": 'Basic realm="HCU inspection", charset="UTF-8"'},
            )
        try:
            allowed = access.authorize(credentials, generation_run_id)
        except Exception:
            raise HTTPException(503, "Inspection authentication unavailable") from None
        if allowed is not True:
            raise HTTPException(403, "Inspection read access rejected")
        try:
            return AgentGenerationInspectionService(repository, evidence_root).get(
                generation_run_id
            )
        except NotFound:
            raise HTTPException(404, "Inspection not found") from None
        except (AgentGenerationReadModelError, SourceArtifactError, ValueError):
            raise HTTPException(422, "Inspection evidence could not be verified") from None
        except Exception:
            # No DB credentials, source, private paths or verifier details in HTTP.
            return JSONResponse(
                status_code=503,
                content={"code": "inspection_unavailable", "message": "Evidence not available"},
            )

    return application


def app_from_environment() -> FastAPI:
    if os.name != "posix":
        raise ValueError("inspection deployment requires native POSIX evidence reads")
    if os.getenv("HCUOPT_MODEL_API_KEY"):
        raise ValueError("do not inject the model credential into the read-only service")
    url = os.environ["HCUOPT_INSPECTION_DATABASE_URL"]
    root = Path(os.environ["HCUOPT_AGENT_INSPECTION_ROOT"])
    access_file = Path(os.environ["HCUOPT_INSPECTION_ACCESS_FILE"])
    options = conninfo_to_dict(url).get("options", "") + " -cdefault_transaction_read_only=on"
    # No migration/claim/reconcile/Worker is started, regardless of AUTO_MIGRATE.
    repository = PostgresRepository(make_conninfo(url, options=options.strip()))
    return create_inspection_app(
        repository=repository, evidence_root=root, access=FileRunReadAccess(access_file)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser("create-access")
    provision.add_argument("--access-file", type=Path, required=True)
    provision.add_argument("--credential-file", type=Path, required=True)
    provision.add_argument("--run-id", type=UUID, action="append", required=True)
    provision.add_argument("--username", default="operator")
    provision.add_argument("--ttl-seconds", type=int, default=3600)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    try:
        if args.command == "create-access":
            access = provision_run_read_access(
                args.access_file,
                args.credential_file,
                run_ids=tuple(args.run_id),
                username=args.username,
                ttl_seconds=args.ttl_seconds,
            )
            print(f"Read credential created; expires at {access.expires_at.isoformat()}")
            print("Secret is in the specified private credential file; not printed.")
        else:
            import uvicorn

            uvicorn.run(
                app_from_environment(),
                host="127.0.0.1",
                port=args.port,
                proxy_headers=False,
                access_log=False,
            )
    except Exception as error:
        print(f"Inspection setup failed: {type(error).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
