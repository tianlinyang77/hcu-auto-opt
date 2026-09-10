# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Local-only, single-task view of the existing Framework Smoke model.

Read-only by default, signing only through an explicit independent capability.
No job/lease writes, raw evidence download, or arbitrary file serving.
The embedding deployment owns the database connection and access credential.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from hcuopt.contracts.v1 import FrameworkSmokeSummary
from hcuopt.deployment.framework_viewer_signing import ViewerSigning, attach_signing


def project_summary(summary: FrameworkSmokeSummary) -> dict:
    """Expose a curated read model, not raw command lines, paths or event payloads."""
    evaluations = sorted(summary.evaluations,
                         key=lambda value: (str(value["created_at"]),
                                            str(value["evaluation_run_id"])))
    latest = evaluations[-1] if evaluations else None
    metrics = {} if latest is None else latest.get("metrics", {})
    checks = {key: metrics.get(key) if type(metrics.get(key)) is bool else None for key in (
        "baseline_execution_succeeded", "noop_execution_succeeded", "output_equivalent",
        "cleanup_healthy")}
    passed = latest is not None and latest.get("passed") is True
    # Match the repository's latest-bundle order; an unrelated/old bundle cannot
    # make a newly completed evaluation ready for human review.
    bundles = sorted(summary.evidence_bundles,
                     key=lambda value: (str(value.get("created_at", "")),
                                        str(value.get("evidence_id", ""))))
    bundle = bundles[-1] if bundles else None
    evidence_ready = (bundle is not None and latest is not None
                      and bool(bundle.get("evidence_id"))
                      and str(bundle.get("task_id")) == str(summary.task.task_id)
                      and str(bundle.get("evaluation_run_id")) == str(latest["evaluation_run_id"]))
    task = summary.task
    # The UI never upgrades pass based on output equivalence alone.
    ready = (task.state.value == "awaiting_signoff" and passed and evidence_ready
             and summary.adapter_mode == "real" and latest.get("synthetic") is False
             and all(value is True for value in checks.values()))
    return {
        "schema": "framework-smoke-inspection-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "live_postgresql_framework_smoke_read_model",
        "task": {"task_id": str(task.task_id), "name": task.name, "state": task.state.value,
                 "profile": task.adapter_profile, "updated_at": task.updated_at.isoformat()},
        "target": {"target_id": summary.target.target_id,
                   "host": summary.target.execution_host.name,
                   "device_index": summary.target.execution_host.accelerator.device_index,
                   "architecture": summary.target.execution_host.accelerator.architecture,
                   "source_commit": summary.target.source_baseline.commit,
                   "image_digest": summary.target.inference_image.registry_digest},
        "counts": {"sources": len(summary.source_snapshots), "artifacts": len(summary.artifacts),
                   "executions": len(summary.execution_attempts),
                   "evaluations": len(evaluations),
                   "evidence_bundles": len(summary.evidence_bundles)},
        "checks": checks, "evaluation_passed": passed if latest is not None else None,
        "evaluation_id": str(latest["evaluation_run_id"]) if latest else None,
        "review_evidence_id": str(bundle["evidence_id"]) if evidence_ready else None,
        "ready_for_human_review": ready, "write_actions_available": False,
        "adapter_mode": summary.adapter_mode, "performance_conclusion": "not_measured",
        "automatic_release_allowed": False,
        "artifacts": [{"artifact_id": str(item["artifact_id"]), "kind": item["kind"],
                       "content_hash": item["content_hash"]} for item in summary.artifacts],
        "events": [{"event_type": item["event_type"], "created_at": str(item["created_at"])}
                   for item in summary.events[-30:]],
    }


def create_viewer(*, task_id: UUID, credential: str,
                  read_summary: Callable[[], FrameworkSmokeSummary],
                  static_root: Path | None = None,
                  control: tuple[UUID, str, Callable[[], None]] | None = None,
                  access_active: Callable[[], bool] | None = None,
                  signing: ViewerSigning | None = None,
                  revoke_signing: Callable[[], None] | None = None) -> FastAPI:
    if len(credential) < 32 or not credential.isascii():
        raise ValueError("viewer requires an independent strong ASCII access credential")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    if signing is not None:
        import hashlib

        identity = signing.session.identity
        if identity.task_id != task_id or identity.token_sha256 in {
            hashlib.sha256(key.encode("ascii")).hexdigest()
            for key in (credential, control[1] if control else credential)
        }:
            raise ValueError("signing credential must be independent and task-bound")
        attach_signing(app, signing, access_active or (lambda: True))

    if control is not None:
        instance_id, control_key, stop = control
        if len(control_key) < 32 or not control_key.isascii() or control_key == credential:
            raise ValueError("control credential must be strong and separate")

        def authorize_control(request: Request, requested_instance: UUID):
            expected = ("Bearer " + control_key).encode("ascii")
            supplied = request.headers.get("authorization", "").encode("utf-8")
            if not secrets.compare_digest(supplied, expected):
                raise HTTPException(401, "Instance control credential required")
            if requested_instance != instance_id:
                raise HTTPException(403, "Instance mismatch")

        @app.get("/v1/viewer-control/{requested_instance}")
        def control_status(requested_instance: UUID, request: Request):
            authorize_control(request, requested_instance)
            return {"instance_id": str(instance_id), "task_id": str(task_id), "status": "running"}

        @app.post("/v1/viewer-control/{requested_instance}/stop")
        def control_stop(requested_instance: UUID, request: Request):
            authorize_control(request, requested_instance)
            stop()
            return {"instance_id": str(instance_id), "status": "stopping"}

        if signing is not None and revoke_signing is not None:
            @app.post("/v1/viewer-control/{requested_instance}/revoke-signing")
            def control_revoke_signing(requested_instance: UUID, request: Request):
                authorize_control(request, requested_instance)
                # Reject new writes even if credential file cleanup fails.
                signing.session.revoke()
                try:
                    revoke_signing()
                except Exception:
                    raise HTTPException(
                        503, "Signing revoked; credential cleanup incomplete"
                    ) from None
                return {"instance_id": str(instance_id), "status": "signing_revoked"}

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.url.hostname not in {"127.0.0.1", "localhost", "[::1]", "::1"}:
            from starlette.responses import Response
            return Response("Local access only", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/v1/framework-smoke-inspection/{requested_task}")
    def inspection(requested_task: UUID, request: Request):
        if access_active is not None and not access_active():
            raise HTTPException(503, "Viewer is stopping")
        supplied = request.headers.get("authorization", "").encode("utf-8")
        if not secrets.compare_digest(supplied, ("Bearer " + credential).encode("ascii")):
            raise HTTPException(401, "Task access credential required")
        if requested_task != task_id:
            raise HTTPException(403, "Credential is not authorized for this task")
        try:
            summary = read_summary()
            if summary.task.task_id != task_id:
                raise ValueError("read model task mismatch")
            result = project_summary(summary)
            result["write_actions_available"] = signing is not None and signing.session.active
            return result
        except Exception as exc:
            # Never reflect DB connection strings, internal paths or credential values.
            raise HTTPException(503, "Task read model unavailable") from exc

    if static_root is not None:
        root = static_root.absolute()
        if root.resolve(strict=True) != root or not (root / "index.html").is_file():
            raise ValueError("viewer requires an existing, non-redirected frontend build")
        app.mount("/", StaticFiles(directory=root, html=True), name="frontend")
    return app
