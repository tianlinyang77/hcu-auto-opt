# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Real, HCU-free adapter for independent endpoint campaign adjudication."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hcuopt.contracts.endpoint_adjudication_v1 import (
    EndpointFormalAdjudicationRequest,
    EndpointFormalAdjudicationResult,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.evaluation.endpoint_adjudication import adjudicate_endpoint_campaign


class LocalEndpointCampaignAdjudicator:
    """Bind D to explicit local evidence roots supplied by the deployment."""

    def __init__(self, *, profile: str, allowed_evidence_roots: tuple[Path, ...]) -> None:
        if not allowed_evidence_roots:
            raise ValueError("endpoint D requires at least one allowed evidence root")
        self.allowed_evidence_roots = tuple(
            path.resolve(strict=True) for path in allowed_evidence_roots
        )
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="endpoint_campaign_adjudicator",
            adapter_name=type(self).__name__,
            adapter_version="1",
            implementation_kind="real",
        )

    def adjudicate_endpoint_campaign(
        self, payload: Mapping[str, Any]
    ) -> EndpointFormalAdjudicationResult:
        # Queue payloads are JSON-native, so UUIDs arrive as strings. Validate
        # through Pydantic's JSON path instead of weakening the contract model.
        request = EndpointFormalAdjudicationRequest.model_validate_json(
            json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        )
        return adjudicate_endpoint_campaign(
            request,
            allowed_roots=self.allowed_evidence_roots,
        )


__all__ = ["LocalEndpointCampaignAdjudicator"]
