# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import ValidationError

from hcuopt.contracts.agent_read_model_v1 import (
    AGENT_GENERATION_ARTIFACT_NAMES,
    AGENT_GENERATION_MANIFEST_VERSION,
    AgentGenerationEvidenceManifest,
    AgentGenerationEvidencePublication,
    AgentGenerationManifestEntry,
    AgentPublishedEvidenceRef,
)
from hcuopt.contracts.agent_v1 import GenerationRunStatusView
from hcuopt.contracts.agent_verification_v1 import (
    AGENT_PROPOSAL_VERIFIER_VERSION,
    AgentEvidenceRef,
    AgentGenerationReadModel,
    AgentProposalVerificationContext,
    AgentProposalVerificationResult,
)
from hcuopt.contracts.platform_v1 import EvidenceBundle
from hcuopt.domain.errors import ContractError
from hcuopt.evaluation.agent_proposal_reporting import build_agent_generation_evidence
from hcuopt.evaluation.agent_proposal_verifier import (
    AgentProposalEvidenceError,
    AgentProposalVerifier,
    build_agent_generation_read_model,
)
from hcuopt.evaluation.evidence_reader import EvidenceReadError, HashedEvidenceReader

_Model = TypeVar("_Model")
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class AgentGenerationReadModelError(ContractError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AgentGenerationReadRepository(Protocol):
    def generation_run_status(self, generation_run_id: UUID) -> GenerationRunStatusView: ...

    def get_agent_generation_evidence_publication(
        self, generation_run_id: UUID
    ) -> AgentGenerationEvidencePublication: ...


def _published_ref(value: Mapping[str, object]) -> AgentPublishedEvidenceRef:
    try:
        return AgentPublishedEvidenceRef.model_validate(
            {
                "uri": value["uri"],
                "content_hash": value["sha256"],
                "byte_count": value["byte_count"],
            },
            strict=True,
        )
    except (KeyError, ValidationError) as exc:
        raise AgentGenerationReadModelError(
            "agent_evidence_artifact_ref_invalid",
            "Agent Generation report returned an invalid artifact reference",
        ) from exc


def build_agent_generation_evidence_publication(
    context: AgentProposalVerificationContext,
    result: AgentProposalVerificationResult,
    artifacts: Mapping[str, Mapping[str, object]],
) -> AgentGenerationEvidencePublication:
    expected = AGENT_GENERATION_ARTIFACT_NAMES | {"sha256sums.json"}
    if set(artifacts) != expected:
        raise AgentGenerationReadModelError(
            "agent_evidence_artifacts_incomplete",
            "Agent Generation report publication requires the complete artifact set",
        )
    if result.generation_run_id != context.generation_run_id:
        raise AgentGenerationReadModelError(
            "agent_evidence_run_mismatch",
            "Agent Generation result and verification context cross Runs",
        )
    return AgentGenerationEvidencePublication(
        generation_run_id=context.generation_run_id,
        input_digest=result.input_digest,
        context=context,
        verification=_published_ref(artifacts["verification.json"]),
        evidence_bundle=_published_ref(artifacts["evidence-bundle.json"]),
        read_model=_published_ref(artifacts["read-model.json"]),
        report=_published_ref(artifacts["report.md"]),
        manifest=_published_ref(artifacts["sha256sums.json"]),
        published_at=result.evidence_created_at,
    )


class AgentGenerationEvidenceReadService:
    """Rebuild and verify the D-owned model before every read-only response."""

    def __init__(
        self,
        repository: AgentGenerationReadRepository,
        reader: HashedEvidenceReader,
        *,
        verifier: AgentProposalVerifier | None = None,
    ) -> None:
        self.repository = repository
        self.reader = reader
        self.verifier = verifier or AgentProposalVerifier(reader)

    def get(self, generation_run_id: UUID) -> AgentGenerationReadModel:
        try:
            publication = self.repository.get_agent_generation_evidence_publication(
                generation_run_id
            )
            live_status = self.repository.generation_run_status(generation_run_id)
        except ValidationError as exc:
            self._fail(
                "agent_evidence_index_invalid",
                "persisted Agent Generation evidence index is invalid",
                cause=exc,
            )
        if publication.verifier_version != AGENT_PROPOSAL_VERIFIER_VERSION:
            self._fail(
                "agent_verifier_version_drift",
                "persisted Agent verifier version differs from the active verifier",
            )
        if live_status.run.state not in _TERMINAL_STATES:
            self._fail(
                "agent_generation_not_terminal",
                "Agent Generation evidence is unavailable before terminal state",
            )
        stored_status = self._load_model(
            publication.context.generation_status,
            GenerationRunStatusView,
        )
        if stored_status != live_status:
            self._fail(
                "agent_generation_status_drift",
                "persisted A status and Budget ledger differ from live authority",
            )
        try:
            verification = self.verifier.verify(publication.context)
        except AgentProposalEvidenceError as exc:
            self._fail(exc.code, str(exc), cause=exc)
        if verification.input_digest != publication.input_digest:
            self._fail(
                "agent_verification_digest_drift",
                "recomputed D verification digest differs from the publication",
            )
        if verification.evidence_created_at != publication.published_at:
            self._fail(
                "agent_evidence_publication_time_drift",
                "Agent Generation publication time differs from D verification evidence",
            )

        persisted_verification = self._load_model(
            publication.verification,
            AgentProposalVerificationResult,
        )
        if persisted_verification != verification:
            self._fail(
                "agent_verification_artifact_drift",
                "persisted verification result differs from D recomputation",
            )

        expected_bundle = build_agent_generation_evidence(publication.context, verification)
        persisted_bundle = self._load_model(publication.evidence_bundle, EvidenceBundle)
        if persisted_bundle != expected_bundle:
            self._fail(
                "agent_evidence_bundle_drift",
                "persisted EvidenceBundle differs from D recomputation",
            )

        expected_read_model = build_agent_generation_read_model(verification)
        persisted_read_model = self._load_model(
            publication.read_model,
            AgentGenerationReadModel,
        )
        if persisted_read_model != expected_read_model:
            self._fail(
                "agent_read_model_drift",
                "persisted Agent read model differs from D recomputation",
            )

        self._read(publication.report)
        manifest = self._load_model(
            publication.manifest,
            AgentGenerationEvidenceManifest,
        )
        expected_manifest = {
            name: AgentGenerationManifestEntry(
                uri=reference.uri,
                sha256=reference.content_hash,
                byte_count=reference.byte_count,
            )
            for name, reference in {
                "verification.json": publication.verification,
                "evidence-bundle.json": publication.evidence_bundle,
                "read-model.json": publication.read_model,
                "report.md": publication.report,
            }.items()
        }
        if manifest.schema_version != AGENT_GENERATION_MANIFEST_VERSION or (
            manifest.files != expected_manifest
        ):
            self._fail(
                "agent_evidence_manifest_drift",
                "Agent Generation manifest does not bind the published artifacts",
            )
        return expected_read_model

    def _load_model(
        self,
        reference: AgentEvidenceRef | AgentPublishedEvidenceRef,
        model: type[_Model],
    ) -> _Model:
        raw = self._read(reference)
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            self._fail(
                "agent_evidence_schema_invalid",
                f"Agent Generation evidence does not match {model.__name__}",
                cause=exc,
            )

    def _read(self, reference: AgentEvidenceRef | AgentPublishedEvidenceRef) -> bytes:
        try:
            raw = self.reader.read_raw_bytes(reference.uri, reference.content_hash)
        except EvidenceReadError as exc:
            self._fail(exc.code, str(exc), cause=exc)
        except (OSError, ValueError) as exc:
            self._fail(
                "agent_evidence_unreadable",
                "Agent Generation evidence could not be read safely",
                cause=exc,
            )
        byte_count = getattr(reference, "byte_count", None)
        if byte_count is not None and len(raw) != byte_count:
            self._fail(
                "agent_evidence_size_mismatch",
                "Agent Generation artifact byte count differs from its publication",
            )
        return raw

    @staticmethod
    def _fail(
        code: str,
        message: str,
        *,
        cause: Exception | None = None,
    ) -> None:
        error = AgentGenerationReadModelError(code, message)
        if cause is None:
            raise error
        raise error from cause


__all__ = [
    "AgentGenerationEvidenceReadService",
    "AgentGenerationReadModelError",
    "AgentGenerationReadRepository",
    "build_agent_generation_evidence_publication",
]
