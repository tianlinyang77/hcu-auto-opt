# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from hcuopt.adapters.agent_generator import AgentProposalMaterializer
from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
from hcuopt.adapters.agent_runner import (
    AgentInputFile,
    AgentRunLimits,
    AgentRunnerAdapter,
    AgentRunRequest,
)
from hcuopt.adapters.agent_runner_receipt import RunnerExecutionReceiptStore
from hcuopt.agent.anthropic_messages_provider import (
    PROPOSAL_OUTPUT_SCHEMA,
    PROVIDER_REQUEST_SCHEMA,
    REQUEST_FILENAME,
)
from hcuopt.agent.identity import candidate_generation_request_hash
from hcuopt.contracts.agent_runner_v1 import RunnerExecutionReceiptRef
from hcuopt.contracts.agent_v1 import CandidateProposalBatch, GenerationAttemptClaim
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.measurement.evidence import canonical_json_bytes

SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_PROVIDER_PROMPT_BYTES = 8 * 1024 * 1024


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_source_path(value: str) -> str:
    parts = value.split("/")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SourceArtifactError("Agent source input path is not normalized and relative")
    return value


@dataclass(frozen=True, slots=True)
class AnthropicMessagesProviderConfig:
    endpoint: str
    model: str
    max_tokens: int
    timeout_seconds: float
    max_response_bytes: int
    allow_insecure_http: bool = False
    thinking_mode: Literal["disabled", "provider_default"] = "disabled"

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint.endswith("/v1/messages"):
            raise SourceArtifactError("Anthropic provider endpoint must end with /v1/messages")
        if not isinstance(self.model, str) or not self.model.strip():
            raise SourceArtifactError("Anthropic provider model is required")
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 100_000_000:
            raise SourceArtifactError("Anthropic provider token limit is invalid")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(float(self.timeout_seconds))
            or not 0 < float(self.timeout_seconds) <= 7_200
        ):
            raise SourceArtifactError("Anthropic provider timeout is invalid")
        if (
            type(self.max_response_bytes) is not int
            or not 1 <= self.max_response_bytes <= 8 * 1024 * 1024
        ):
            raise SourceArtifactError("Anthropic provider response limit is invalid")
        if type(self.allow_insecure_http) is not bool:
            raise SourceArtifactError("Anthropic provider insecure HTTP flag is invalid")
        if self.thinking_mode not in {"disabled", "provider_default"}:
            raise SourceArtifactError("Anthropic provider thinking mode is invalid")


@dataclass(frozen=True, slots=True)
class AgentProviderAttemptResult:
    receipt_ref: RunnerExecutionReceiptRef
    batch: CandidateProposalBatch | None
    batch_uri: str | None
    failure_code: str | None


class AnthropicMessagesGenerationWorker:
    """Execute one claimed real-provider Attempt without settling A-owned state."""

    def __init__(
        self,
        *,
        config: AnthropicMessagesProviderConfig,
        runner: AgentRunnerAdapter,
        receipt_store: RunnerExecutionReceiptStore,
        knowledge_store: KnowledgeSnapshotStore,
        materializer: AgentProposalMaterializer,
        executable: Path | None = None,
        provider_artifact: Path | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.receipt_store = receipt_store
        self.knowledge_store = knowledge_store
        self.materializer = materializer
        self.executable = (executable or Path(sys.executable)).resolve()
        self.provider_artifact = (
            provider_artifact
            or Path(__file__).parents[1] / "agent" / "anthropic_messages_provider.py"
        ).resolve()

    @property
    def generator_artifact_hash(self) -> str:
        if not self.provider_artifact.is_file() or self.provider_artifact.is_symlink():
            raise SourceArtifactError("Anthropic provider Artifact is not a regular file")
        return _sha256(self.provider_artifact.read_bytes())

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a bounded HCU inference optimization proposal generator. "
            "Return exactly one JSON object and no Markdown fences or commentary. "
            f'The object schema is {{"schema_version":"{PROPOSAL_OUTPUT_SCHEMA}",'
            '"proposals":[{"optimization_intent":"...","rationale":"...",'
            '"risk_summary":"...","touched_paths":["relative/path.py"],'
            '"patch":"unified git diff"}]}. '
            "Every patch must be a minimal startup-overlay-compatible Python/Triton diff, "
            "touch only the supplied source paths, preserve public behavior, and make no "
            "performance, correctness, release, or HCU-execution claim."
        )

    def _provider_request(
        self,
        *,
        claim: GenerationAttemptClaim,
        source_files: Mapping[str, bytes],
        profiler_evidence: bytes,
    ) -> bytes:
        request = claim.request
        if _sha256(profiler_evidence) != request.profiler_evidence_hash:
            raise SourceArtifactError("Profiler Evidence bytes do not match the Request Hash")
        if not source_files:
            raise SourceArtifactError("Agent provider requires at least one frozen source file")
        normalized_sources: list[dict[str, str]] = []
        for path, payload in sorted(source_files.items()):
            normalized = _safe_source_path(path)
            if not isinstance(payload, bytes):
                raise SourceArtifactError("Agent source input must be bytes")
            try:
                content = payload.decode("utf-8")
            except UnicodeError as error:
                raise SourceArtifactError("Agent source input must be UTF-8") from error
            normalized_sources.append(
                {"path": normalized, "sha256": _sha256(payload), "content": content}
            )

        knowledge = self.knowledge_store.load(
            request.knowledge_snapshot_id,
            request.knowledge_snapshot_hash,
        )
        context = {
            "generation_request": request.model_dump(mode="json"),
            "generation_request_hash": candidate_generation_request_hash(request),
            "generator_limits": {
                "max_proposals": min(request.max_proposals, claim.generator.max_proposals)
            },
            "profiler_evidence": {
                "sha256": request.profiler_evidence_hash,
                "content": profiler_evidence.decode("utf-8", errors="replace"),
            },
            "knowledge_sources": [
                {
                    "knowledge_id": item.reference.knowledge_id,
                    "version": item.reference.version,
                    "sha256": item.reference.content_hash,
                    "content": item.payload.decode("utf-8", errors="replace"),
                }
                for item in knowledge.sources
            ],
            "source_files": normalized_sources,
        }
        user_prompt = json.dumps(
            context,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(user_prompt.encode("utf-8")) > MAX_PROVIDER_PROMPT_BYTES:
            raise SourceArtifactError("Anthropic provider prompt exceeds its size limit")
        max_response_bytes = min(
            self.config.max_response_bytes,
            claim.generator.max_output_bytes_per_attempt,
        )
        return canonical_json_bytes(
            {
                "schema_version": PROVIDER_REQUEST_SCHEMA,
                "endpoint": self.config.endpoint,
                "model": self.config.model,
                "max_tokens": min(
                    self.config.max_tokens,
                    claim.generator.max_tokens_per_attempt,
                ),
                "timeout_seconds": min(
                    float(self.config.timeout_seconds),
                    float(claim.generator.timeout_seconds),
                ),
                "system_prompt": self._system_prompt(),
                "user_prompt": user_prompt,
                "max_response_bytes": max_response_bytes,
                "allow_insecure_http": self.config.allow_insecure_http,
                "thinking_mode": self.config.thinking_mode,
            }
        )

    def run_claim(
        self,
        claim: GenerationAttemptClaim,
        *,
        source_files: Mapping[str, bytes],
        profiler_evidence: bytes,
        runner_output_dir: Path,
    ) -> AgentProviderAttemptResult:
        if claim.generator.generator_artifact_hash != self.generator_artifact_hash:
            raise SourceArtifactError(
                "Generation Plan does not bind the deployed provider Artifact"
            )
        if self.materializer.provenance.profile != claim.generator.adapter_profile:
            raise SourceArtifactError(
                "Generation Plan does not bind the Proposal materializer Profile"
            )
        provider_request = self._provider_request(
            claim=claim,
            source_files=source_files,
            profiler_evidence=profiler_evidence,
        )
        output_limit = claim.generator.max_output_bytes_per_attempt
        run_request = AgentRunRequest(
            attempt_id=claim.attempt.attempt_id,
            generation_run_id=claim.run.generation_run_id,
            request_id=claim.request.request_id,
            request_hash=candidate_generation_request_hash(claim.request),
            plan_id=claim.run.plan.plan_id,
            generator_id=claim.generator.generator_id,
            executable=self.executable,
            generator_artifact=self.provider_artifact,
            generator_artifact_hash=claim.generator.generator_artifact_hash,
            argv=(str(self.provider_artifact),),
            limits=AgentRunLimits(
                attempt_number=claim.attempt.attempt_number,
                timeout_seconds=float(claim.generator.timeout_seconds),
                max_stdout_bytes=output_limit,
                max_stderr_bytes=min(16_384, output_limit),
                max_total_output_bytes=output_limit,
                max_tokens=claim.generator.max_tokens_per_attempt,
            ),
            input_files=(AgentInputFile(REQUEST_FILENAME, provider_request),),
        )
        result = self.runner.run(run_request, runner_output_dir / "attempts")
        receipt_ref = self.receipt_store.publish(result)
        if result.proposal_bytes is None:
            return AgentProviderAttemptResult(
                receipt_ref=receipt_ref,
                batch=None,
                batch_uri=None,
                failure_code=result.status,
            )
        receipt = self.receipt_store.load(receipt_ref)
        batch = self.materializer.materialize(
            request=claim.request,
            receipt=receipt,
            raw_output=result.proposal_bytes,
        )
        stored_batch = self.materializer.batch_store.publish(batch)
        return AgentProviderAttemptResult(
            receipt_ref=receipt_ref,
            batch=batch,
            batch_uri=stored_batch.uri,
            failure_code=None,
        )


__all__ = [
    "AgentProviderAttemptResult",
    "AnthropicMessagesGenerationWorker",
    "AnthropicMessagesProviderConfig",
]
