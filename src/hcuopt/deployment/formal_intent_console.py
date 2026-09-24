# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Narrow deployment surface for Formal intent preparation and submission only."""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from hcuopt.api.formal_start_management import FormalStartManagement
from hcuopt.domain.errors import Conflict, ContractError, NotFound
from hcuopt.operator.formal_start import FormalStartRepository


def create_formal_intent_console(
    *,
    management: FormalStartManagement,
    repository: FormalStartRepository,
    static_root: Path,
    browser_origin: str,
) -> FastAPI:
    """No generic control-plane router, database migration, worker or recovery job.

    Serve through TLS or bind only to loopback. The trusted deployment supplies
    the exact public origin, protected capability configuration and repository.
    """
    origin = urlsplit(browser_origin)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.path
        or origin.query
        or origin.fragment
        or (origin.scheme == "http" and origin.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise ValueError("Formal console requires an exact TLS or loopback origin")
    # Trigger port validation, including malformed/out-of-range values.
    _ = origin.port
    if not (static_root / "index.html").is_file() or not (static_root / "assets").is_dir():
        raise ValueError("Formal console requires a built frontend")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.repository = repository

    @app.middleware("http")
    async def protect_origin(request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            request.headers.get("host") != origin.netloc
            or request.headers.get("origin") not in {None, browser_origin}
            or request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}
        ):
            response = JSONResponse({"detail": "Formal console origin rejected"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, _error: Conflict):  # type: ignore[no-untyped-def]
        return JSONResponse(
            {"detail": "Formal request conflicts with authority or state"}, status_code=409
        )

    @app.exception_handler(NotFound)
    async def missing(_request: Request, _error: NotFound):  # type: ignore[no-untyped-def]
        return JSONResponse({"detail": "Formal authority object is unavailable"}, status_code=404)

    @app.exception_handler(ContractError)
    async def invalid(_request: Request, _error: ContractError):  # type: ignore[no-untyped-def]
        return JSONResponse({"detail": "Formal request contract rejected"}, status_code=422)

    app.include_router(management.router())

    @app.get("/", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(static_root / "index.html")

    app.mount("/assets", StaticFiles(directory=static_root / "assets"), name="assets")
    return app
