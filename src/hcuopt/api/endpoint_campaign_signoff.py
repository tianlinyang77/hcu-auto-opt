# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Authenticated human signoff entry for an Endpoint Campaign."""

from uuid import UUID

from fastapi import HTTPException, Request

from hcuopt.contracts.endpoint_adjudication_v1 import EndpointCampaignSignoffRequest


def submit_endpoint_campaign_signoff(
    authorizer,
    writer,
    request: Request,
    campaign_id: UUID,
    payload: EndpointCampaignSignoffRequest,
):
    if authorizer is None:
        raise HTTPException(503, "Endpoint Campaign signing identity is not configured")
    try:
        actor = authorizer(request, campaign_id)
    except Exception:
        raise HTTPException(503, "Endpoint Campaign signing identity unavailable") from None
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise HTTPException(403, "Endpoint Campaign signing authorization required")
    if payload.actor != actor:
        raise HTTPException(403, "Endpoint Campaign signer identity mismatch")
    return writer(campaign_id, payload.model_copy(update={"actor": actor}))


__all__ = ["submit_endpoint_campaign_signoff"]
