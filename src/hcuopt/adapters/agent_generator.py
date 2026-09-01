# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
from hcuopt.agent.identity import (
    candidate_generation_request_hash,
    candidate_proposal_batch_hash,
)
from hcuopt.agent.patch_identity import normalized_patch_hash_v1, raw_patch_hash_v1
from hcuopt.contracts.agent_v1 import (
    CandidateGenerationRequest,
    CandidateProposalBatch,
)
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path

MAX_PROPOSAL_PATCH_BYTES = 4 * 1024 * 1024
MAX_PROPOSAL_BATCH_BYTES = 8 * 1024 * 1024


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest_path(root: Path, namespace: str, digest: str, filename: str) -> Path:
    value = digest.removeprefix("sha256:")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SourceArtifactError("Proposal Store received an invalid SHA256 identity")
    return root / namespace / "sha256" / value[:2] / value[2:] / filename


def _read_regular(path: Path, *, maximum_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SourceArtifactError(f"Proposal Store entry is not a regular file: {path}")
    if path.stat().st_size > maximum_bytes:
        raise SourceArtifactError(f"Proposal Store entry exceeds its size limit: {path}")
    return path.read_bytes()


def _publish_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _read_regular(path, maximum_bytes=len(payload)) != payload:
            raise SourceArtifactError(
                "Proposal Store immutable identity already contains different bytes"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            if _read_regular(path, maximum_bytes=len(payload)) != payload:
                raise SourceArtifactError(
                    "Proposal Store concurrent publication changed immutable bytes"
                ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _resolve_store_uri(root: Path, uri: str, *, namespace: str) -> Path:
    path = file_uri_to_path(uri).resolve(strict=True)
    expected_root = (root / namespace / "sha256").resolve(strict=True)
    try:
        path.relative_to(expected_root)
    except ValueError as error:
        raise SourceArtifactError("Proposal URI is outside the deployment-owned Store") from error
    return path


@dataclass(frozen=True, slots=True)
class StoredProposalPatch:
    uri: str
    patch_hash: str
    normalized_patch_hash: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StoredProposalBatch:
    uri: str
    batch_hash: str
    batch: CandidateProposalBatch


class ProposalPatchStore:
    def __init__(self, root: Path, *, profile: str) -> None:
        self.root = root.resolve()
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="candidate_proposal_patch_store",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def publish(self, raw_patch: bytes) -> StoredProposalPatch:
        if len(raw_patch) > MAX_PROPOSAL_PATCH_BYTES:
            raise SourceArtifactError("Candidate Proposal Patch exceeds its size limit")
        patch_hash = raw_patch_hash_v1(raw_patch)
        normalized_hash = normalized_patch_hash_v1(raw_patch)
        path = _digest_path(
            self.root,
            "patches",
            patch_hash,
            "proposal.diff",
        )
        _publish_once(path, raw_patch)
        return StoredProposalPatch(
            uri=path.resolve(strict=True).as_uri(),
            patch_hash=patch_hash,
            normalized_patch_hash=normalized_hash,
            size_bytes=len(raw_patch),
        )

    def read(
        self,
        uri: str,
        *,
        expected_patch_hash: str,
        expected_normalized_patch_hash: str,
    ) -> bytes:
        path = _resolve_store_uri(self.root, uri, namespace="patches")
        payload = _read_regular(path, maximum_bytes=MAX_PROPOSAL_PATCH_BYTES)
        if raw_patch_hash_v1(payload) != expected_patch_hash:
            raise SourceArtifactError("Candidate Proposal Patch raw Hash changed in Store")
        if normalized_patch_hash_v1(payload) != expected_normalized_patch_hash:
            raise SourceArtifactError(
                "Candidate Proposal Patch normalized Hash changed in Store"
            )
        return payload


class CandidateProposalBatchStore:
    def __init__(self, root: Path, *, profile: str) -> None:
        self.root = root.resolve()
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="candidate_proposal_batch_store",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def publish(self, batch: CandidateProposalBatch) -> StoredProposalBatch:
        batch_hash = candidate_proposal_batch_hash(batch)
        payload = canonical_json_bytes(batch)
        if len(payload) > MAX_PROPOSAL_BATCH_BYTES:
            raise SourceArtifactError("Candidate Proposal Batch exceeds its size limit")
        path = _digest_path(self.root, "batches", batch_hash, "batch.json")
        _publish_once(path, payload)
        return self.load(batch_hash, expected_batch_id=batch.batch_id)

    def load(
        self,
        expected_hash: str,
        *,
        expected_batch_id: UUID | None = None,
    ) -> StoredProposalBatch:
        path = _digest_path(self.root, "batches", expected_hash, "batch.json")
        payload = _read_regular(path, maximum_bytes=MAX_PROPOSAL_BATCH_BYTES)
        batch = CandidateProposalBatch.model_validate_json(payload)
        if expected_batch_id is not None and batch.batch_id != expected_batch_id:
            raise SourceArtifactError("Candidate Proposal Batch identity changed in Store")
        actual_hash = candidate_proposal_batch_hash(batch)
        if actual_hash != expected_hash:
            raise SourceArtifactError("Candidate Proposal Batch content changed in Store")
        return StoredProposalBatch(
            uri=path.resolve(strict=True).as_uri(),
            batch_hash=actual_hash,
            batch=batch,
        )


class DeterministicCandidateGeneratorAdapter:
    """Synthetic CI generator that still consumes deployment-owned knowledge bytes."""

    def __init__(
        self,
        *,
        generator_id: str,
        profile: str,
        knowledge_store: KnowledgeSnapshotStore,
        patch_store: ProposalPatchStore,
        batch_store: CandidateProposalBatchStore,
        raw_patch: bytes,
        touched_paths: tuple[str, ...],
        optimization_intent: str,
        rationale: str,
        risk_summary: str,
        attempt_number: int = 1,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.generator_id = generator_id
        self.knowledge_store = knowledge_store
        self.patch_store = patch_store
        self.batch_store = batch_store
        self.raw_patch = raw_patch
        self.touched_paths = touched_paths
        self.optimization_intent = optimization_intent
        self.rationale = rationale
        self.risk_summary = risk_summary
        self.attempt_number = attempt_number
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="candidate_proposal_generation",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="fake",
        )

    def generate_proposals(
        self,
        request: CandidateGenerationRequest,
        output_dir: Path,
    ) -> CandidateProposalBatch:
        self.knowledge_store.load(
            request.knowledge_snapshot_id,
            request.knowledge_snapshot_hash,
        )
        request_hash = candidate_generation_request_hash(request)
        stored_patch = self.patch_store.publish(self.raw_patch)
        proposal_id = uuid5(
            NAMESPACE_URL,
            "hcuopt:m2b-deterministic-proposal:"
            f"{request.request_id}:{self.generator_id}:"
            f"{stored_patch.normalized_patch_hash}",
        )
        batch_id = uuid5(
            NAMESPACE_URL,
            f"hcuopt:m2b-deterministic-batch:{request.request_id}:{proposal_id}",
        )
        raw_output = canonical_json_bytes(
            {
                "schema_version": "m2b-deterministic-generator-output-v1",
                "batch_id": str(batch_id),
                "proposal_id": str(proposal_id),
                "request_hash": request_hash,
                "patch_hash": stored_patch.patch_hash,
                "normalized_patch_hash": stored_patch.normalized_patch_hash,
            }
        )
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_output_path = output_dir / f"{batch_id}.json"
        _publish_once(raw_output_path, raw_output)
        started_at = self.clock()
        finished_at = self.clock()
        batch = CandidateProposalBatch(
            batch_id=batch_id,
            request_id=request.request_id,
            request_hash=request_hash,
            generation_run_id=request.generation_run_id,
            generator_id=self.generator_id,
            adapter_provenance=self.provenance,
            status="succeeded",
            proposals=(
                {
                    "proposal_id": proposal_id,
                    "request_id": request.request_id,
                    "request_hash": request_hash,
                    "generation_run_id": request.generation_run_id,
                    "generator_id": self.generator_id,
                    "ordinal": 0,
                    "optimization_intent": self.optimization_intent,
                    "rationale": self.rationale,
                    "risk_summary": self.risk_summary,
                    "patch_uri": stored_patch.uri,
                    "patch_hash": stored_patch.patch_hash,
                    "normalized_patch_hash": stored_patch.normalized_patch_hash,
                    "touched_paths": self.touched_paths,
                    "replacement_point": request.replacement_point,
                },
            ),
            raw_output_uri=raw_output_path.resolve(strict=True).as_uri(),
            raw_output_hash=_sha256(raw_output),
            output_bytes=len(raw_output),
            token_count=0,
            attempt_count=self.attempt_number,
            wall_seconds=max(0.0, (finished_at - started_at).total_seconds()),
            started_at=started_at,
            finished_at=finished_at,
            synthetic=True,
        )
        self.batch_store.publish(batch)
        return batch


__all__ = [
    "CandidateProposalBatchStore",
    "DeterministicCandidateGeneratorAdapter",
    "ProposalPatchStore",
    "StoredProposalBatch",
    "StoredProposalPatch",
]
