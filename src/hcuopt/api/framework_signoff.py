# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Shared authenticated entry to the existing Framework Smoke state machine."""

from uuid import UUID

from fastapi import HTTPException, Request

from hcuopt.contracts.v1 import FrameworkSmokeSignoffRequest


def signing_actor(authorizer, request: Request, task_id: UUID) -> str:
    if authorizer is None:
        raise HTTPException(503, "Framework Smoke signing identity is not configured")
    try:
        actor = authorizer(request, task_id)
    except Exception:
        raise HTTPException(503, "Framework Smoke signing identity unavailable") from None
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise HTTPException(403, "Framework Smoke signing authorization required")
    return actor


def submit_signoff(
    authorizer, writer, request: Request, task_id: UUID, payload: FrameworkSmokeSignoffRequest
):
    actor = signing_actor(authorizer, request, task_id)
    if payload.actor != actor:
        raise HTTPException(403, "Framework Smoke signer identity mismatch")
    return writer(task_id, payload.model_copy(update={"actor": actor}))
