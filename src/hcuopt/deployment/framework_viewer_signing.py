# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Optional task-scoped routes; never instantiate the general control-plane API."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request

from hcuopt.api.framework_signoff import signing_actor, submit_signoff
from hcuopt.contracts.v1 import FrameworkSmokeSignoffRequest, FrameworkSmokeSignoffView
from hcuopt.deployment.framework_signoff_identity import FrameworkSigningSession
from hcuopt.domain.errors import Conflict, ContractError, NotFound


@dataclass
class ViewerSigning:
    session: FrameworkSigningSession
    write: Callable
    read: Callable


def read_framework_signoff(repository, task_id: UUID):
    """Project an existing durable decision; no write or alternate state machine."""
    with repository.connection() as connection:
        return connection.execute(
            """
            SELECT signoff.*, task.state AS task_state
            FROM framework_smoke_signoffs AS signoff
            JOIN tasks AS task ON task.task_id = signoff.task_id
            WHERE signoff.task_id = %s
            """,
            (task_id,),
        ).fetchone()


def attach_signing(app: FastAPI, signing: ViewerSigning, access_active: Callable[[], bool]):
    def authorizer(request, task):
        return signing.session(request, task) if access_active() else None

    @app.get("/v1/framework-smoke/tasks/{requested_task}/signoff")
    def status(requested_task: UUID, request: Request):
        actor = signing_actor(authorizer, request, requested_task)
        try:
            row = signing.read(requested_task)
            result = FrameworkSmokeSignoffView.model_validate(row) if row is not None else None
            if result is not None and result.task_id != requested_task:
                raise ValueError("signoff task mismatch")
            return {
                "actor": actor,
                "task_id": requested_task,
                "expires_at": signing.session.identity.expires_at,
                "signoff": result,
                "automatic_release_allowed": False,
            }
        except Exception:
            raise HTTPException(503, "Framework Smoke signing status unavailable") from None

    @app.post(
        "/v1/framework-smoke/tasks/{requested_task}/signoff",
        response_model=FrameworkSmokeSignoffView,
    )
    def submit(requested_task: UUID, payload: FrameworkSmokeSignoffRequest, request: Request):
        try:
            return submit_signoff(authorizer, signing.write, request, requested_task, payload)
        except HTTPException:
            raise
        except NotFound:
            raise HTTPException(404, "Framework Smoke task or evidence unavailable") from None
        except (Conflict, ContractError):
            raise HTTPException(
                409, "Framework Smoke signing conflict; review evidence again"
            ) from None
        except Exception:
            raise HTTPException(
                503, "Framework Smoke signing result unknown; check status"
            ) from None
