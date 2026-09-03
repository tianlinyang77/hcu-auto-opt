# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from hcuopt.contracts.agent_verification_v1 import (
    AGENT_PROPOSAL_VERIFIER_VERSION,
    AgentEvidenceRef,
    AgentProposalVerificationContext,
)
from hcuopt.contracts.base import ContractModel

AGENT_GENERATION_READ_MODEL_VERSION = "m2b-agent-generation-read-model-v1"
AGENT_GENERATION_PUBLICATION_VERSION = "m2b-agent-evidence-publication-v1"
AGENT_GENERATION_MANIFEST_VERSION = "m2b-generation-manifest-v1"
AGENT_GENERATION_ARTIFACT_NAMES = frozenset(
    {
        "verification.json",
        "evidence-bundle.json",
        "read-model.json",
        "report.md",
    }
)


class AgentPublishedEvidenceRef(AgentEvidenceRef):
    byte_count: int = Field(ge=1, le=64 * 1024 * 1024)


class AgentGenerationManifestEntry(ContractModel):
    uri: str = Field(min_length=1, max_length=4000)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    byte_count: int = Field(ge=1, le=64 * 1024 * 1024)


class AgentGenerationEvidenceManifest(ContractModel):
    schema_version: Literal["m2b-generation-manifest-v1"] = (
        AGENT_GENERATION_MANIFEST_VERSION
    )
    files: dict[str, AgentGenerationManifestEntry]

    @model_validator(mode="after")
    def require_complete_manifest(self) -> AgentGenerationEvidenceManifest:
        if set(self.files) != AGENT_GENERATION_ARTIFACT_NAMES:
            raise ValueError("Agent Generation manifest has an incomplete artifact set")
        return self


class AgentGenerationEvidencePublication(ContractModel):
    """Immutable index for rebuilding one D-owned Agent Generation read model."""

    schema_version: Literal["m2b-agent-evidence-publication-v1"] = (
        AGENT_GENERATION_PUBLICATION_VERSION
    )
    generation_run_id: UUID
    verifier_version: Literal["m2b-proposal-verifier-v1"] = (
        AGENT_PROPOSAL_VERIFIER_VERSION
    )
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context: AgentProposalVerificationContext
    verification: AgentPublishedEvidenceRef
    evidence_bundle: AgentPublishedEvidenceRef
    read_model: AgentPublishedEvidenceRef
    report: AgentPublishedEvidenceRef
    manifest: AgentPublishedEvidenceRef
    published_at: datetime
    dev_only: Literal[True] = True
    environment: Literal["scripted_dev_only"] = "scripted_dev_only"
    performance_conclusion: Literal["not_measured"] = "not_measured"
    formal_readiness: Literal["hold"] = "hold"
    formal_intake_allowed: Literal[False] = False
    hcu_access_allowed: Literal[False] = False
    measurement_access_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_publication_binding(self) -> AgentGenerationEvidencePublication:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("Agent Generation publication time must be timezone-aware")
        if self.context.generation_run_id != self.generation_run_id:
            raise ValueError("Agent Generation publication crosses Generation Runs")
        references = (
            self.verification,
            self.evidence_bundle,
            self.read_model,
            self.report,
            self.manifest,
        )
        if len({item.uri for item in references}) != len(references):
            raise ValueError("Agent Generation publication reuses an artifact URI")
        if len({item.content_hash for item in references}) != len(references):
            raise ValueError("Agent Generation publication reuses artifact content")
        return self


__all__ = [
    "AGENT_GENERATION_ARTIFACT_NAMES",
    "AGENT_GENERATION_MANIFEST_VERSION",
    "AGENT_GENERATION_PUBLICATION_VERSION",
    "AGENT_GENERATION_READ_MODEL_VERSION",
    "AgentGenerationEvidenceManifest",
    "AgentGenerationManifestEntry",
    "AgentGenerationEvidencePublication",
    "AgentPublishedEvidenceRef",
]
