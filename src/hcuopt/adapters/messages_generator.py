# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Messages input preparation and Receipt-bound Proposal ingestion, not a scheduler."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from uuid import uuid5

from pydantic import Field, ValidationError, model_validator

from hcuopt.adapters.agent_generator import (
    CandidateProposalBatchStore,
    ProposalPatchStore,
    StoredProposalBatch,
)
from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
from hcuopt.adapters.agent_promotion import (
    BaselineOverlaySource,
    CandidateSourcePackagePublisher,
    apply_single_file_unified_patch,
    build_single_replacement_patch,
)
from hcuopt.adapters.agent_runner import AgentInputFile
from hcuopt.adapters.agent_runner_receipt import RunnerExecutionReceiptStore
from hcuopt.agent.authority import (
    actual_usage_for_runner_receipt,
    reserved_usage_for,
    usage_is_within_reservation,
    validate_runner_receipt,
)
from hcuopt.agent.identity import apex_generation_plan_hash, candidate_generation_request_hash
from hcuopt.agent.patch_identity import normalized_patch_hash_v1, raw_patch_hash_v1
from hcuopt.contracts.agent_runner_v1 import RunnerExecutionReceiptRef
from hcuopt.contracts.agent_v1 import (
    CandidateGenerationRequest,
    CandidateProposal,
    CandidateProposalBatch,
    GenerationRun,
    GeneratorAttempt,
)
from hcuopt.contracts.base import ContractModel
from hcuopt.contracts.platform_v1 import AdapterProvenance
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.generators.anthropic_messages import (
    INPUT_NAME,
    MAX_INPUT_BYTES,
    MAX_RESPONSE_BYTES,
    MessagesError,
    messages_url,
    proposal_text,
    strict_json,
    usage_tokens,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.source_hash import file_uri_to_path


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class MessagesSettings(ContractModel):
    """Non-secret deployment settings; the allowlisted Runner receives the key separately."""

    base_url: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=200)
    max_output_tokens: int = Field(default=4096, strict=True, ge=1, le=16_384)
    timeout_seconds: int = Field(default=120, strict=True, ge=1, le=300)
    allow_http: bool = Field(default=False, strict=True)
    thinking_mode: str = Field(default="disabled", pattern="^(disabled|provider_default)$")


class _ProposalText(ContractModel):
    optimization_intent: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=10_000)
    risk_summary: str = Field(min_length=1, max_length=4000)
    patch: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    old_text: str | None = Field(default=None, min_length=1, max_length=256_000)
    new_text: str | None = Field(default=None, min_length=1, max_length=256_000)

    @model_validator(mode="after")
    def require_one_edit_encoding(self) -> _ProposalText:
        patch_mode = self.patch is not None
        replacement_mode = self.old_text is not None and self.new_text is not None
        if patch_mode == replacement_mode or (self.old_text is None) != (self.new_text is None):
            raise ValueError("Proposal must use exactly one supported edit encoding")
        return self


class _ProposalsText(ContractModel):
    proposals: tuple[_ProposalText, ...] = Field(max_length=8)


def prepare_messages_input(
    request: CandidateGenerationRequest,
    *,
    baseline: BaselineOverlaySource,
    knowledge_store: KnowledgeSnapshotStore,
    settings: MessagesSettings,
    hotspot_summary: str,
) -> AgentInputFile:
    """Package only an explicit source file and verified advisory knowledge.

    The caller owns the selected hotspot summary and source-disclosure approval.
    The profiler URI is a reference, never fetched or sent as raw Holdout data.
    This function does not grant permission to send the resulting bytes anywhere.
    """
    request = CandidateGenerationRequest.model_validate(request.model_dump(mode="json"))
    settings = MessagesSettings.model_validate(settings.model_dump(mode="json"))
    messages_url(settings.base_url, allow_http=settings.allow_http)
    if not isinstance(hotspot_summary, str) or not 1 <= len(hotspot_summary) <= 10_000:
        raise SourceArtifactError("hotspot summary must be bounded explicit advisory text")
    path = baseline.path
    if (
        not path.endswith(".py")
        or "\\" in path
        or ":" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise SourceArtifactError("Messages input requires one safe Python source path")
    source = CandidateSourcePackagePublisher._read_frozen_baseline(
        baseline, expected_source_hash=request.baseline_source_hash
    )
    knowledge = knowledge_store.load(request.knowledge_snapshot_id, request.knowledge_snapshot_hash)
    try:
        envelope = {
            "schema_version": "m2b-messages-input-v1",
            "provider": settings.model_dump(mode="json"),
            "context": {
                "request_hash": candidate_generation_request_hash(request),
                "baseline_source_hash": request.baseline_source_hash,
                "hotspot_id": str(request.hotspot_id),
                "replacement_point": request.replacement_point,
                "workload_id": request.workload_id,
                "workload_hash": request.workload_hash,
                "profiler_evidence_hash": request.profiler_evidence_hash,
                "hotspot_summary": hotspot_summary,
                "hotspot_summary_authority": "operator_advisory_not_measurement",
                "max_proposals": request.max_proposals,
                "source_path": path,
                "source_content_hash": _hash(source),
                "source": source.decode("utf-8", errors="strict"),
                "knowledge_snapshot_hash": request.knowledge_snapshot_hash,
                "knowledge": [
                    {
                        "knowledge_id": item.reference.knowledge_id,
                        "version": item.reference.version,
                        "license_id": item.reference.license_id,
                        "content_hash": item.reference.content_hash,
                        "content": item.payload.decode("utf-8", errors="strict"),
                        "authority": "advisory_only",
                    }
                    for item in knowledge.sources
                ],
                "performance_conclusion": "not_measured",
                "formal_intake_allowed": False,
                "automatic_release_allowed": False,
            },
        }
        payload = canonical_json_bytes(envelope)
    except UnicodeError as error:
        raise SourceArtifactError("Messages input must contain UTF-8 text") from error
    if len(payload) > MAX_INPUT_BYTES:
        raise SourceArtifactError("Messages input exceeds its size limit")
    return AgentInputFile(path=INPUT_NAME, content=payload)


def messages_input_manifest_hash(item: AgentInputFile) -> str:
    """Exact existing Runner manifest encoding, including its no-newline rule."""
    return _hash(
        json.dumps(
            [{"path": item.path, "content_hash": _hash(item.content)}],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def messages_profile_for_input(item: AgentInputFile) -> str:
    """Bind endpoint/model/source/knowledge input into the existing frozen Plan."""
    return "m2b-messages-v1-" + messages_input_manifest_hash(item).removeprefix("sha256:")


class MessagesProposalIngestor:
    """Parse an existing successful Runner Receipt; never accept model-owned authority.

    Request, Plan, Attempt and input must be supplied by the deployment/claim owner,
    not by a public HTTP caller or the model. A still settles with its claim token;
    this service neither claims jobs nor writes Generation Run/Review state.
    """

    def __init__(
        self,
        *,
        receipt_store: RunnerExecutionReceiptStore,
        patch_store: ProposalPatchStore,
        batch_store: CandidateProposalBatchStore,
        profile: str,
    ) -> None:
        self.receipt_store = receipt_store
        self.patch_store = patch_store
        self.batch_store = batch_store
        self.provenance = AdapterProvenance(
            profile=profile,
            capability="candidate_proposal_generation",
            adapter_name=type(self).__name__,
            adapter_version="1.0.0",
            implementation_kind="real",
        )

    def ingest(
        self,
        run: GenerationRun,
        attempt: GeneratorAttempt,
        receipt_ref: RunnerExecutionReceiptRef,
        *,
        prepared_input: AgentInputFile,
    ) -> StoredProposalBatch:
        run = GenerationRun.model_validate(run.model_dump(mode="json"))
        attempt = GeneratorAttempt.model_validate(attempt.model_dump(mode="json"))
        if (
            run.request_hash != candidate_generation_request_hash(run.request)
            or run.plan_hash != apex_generation_plan_hash(run.plan)
            or attempt.plan_id != run.plan.plan_id
            or attempt.started_at is None
            or attempt.state != "running"
            or attempt.generator_ordinal >= len(run.plan.generators)
        ):
            raise SourceArtifactError("Messages generation authority is invalid")
        generator = run.plan.generators[attempt.generator_ordinal]
        if (
            generator.adapter_profile != self.provenance.profile
            or generator.adapter_profile != messages_profile_for_input(prepared_input)
            or attempt.adapter_profile != self.provenance.profile
            or attempt.reserved != reserved_usage_for(generator)
        ):
            raise SourceArtifactError("Messages Profile or reservation differs from frozen Plan")
        receipt = self.receipt_store.load(receipt_ref)
        execution = receipt.execution
        if execution.status != "succeeded":
            raise SourceArtifactError("unsuccessful Runner cannot produce a Messages Batch")
        if (
            prepared_input.path != INPUT_NAME
            or execution.input_manifest_hash != messages_input_manifest_hash(prepared_input)
            or len(prepared_input.content) > MAX_INPUT_BYTES
            or not math.isfinite(execution.wall_seconds_consumed)
        ):
            raise SourceArtifactError("Messages input does not match executed Runner manifest")
        envelope = strict_json(prepared_input.content)
        if not isinstance(envelope, dict):
            raise SourceArtifactError("Messages input envelope is invalid")
        context = envelope["context"]
        if (
            context["request_hash"] != run.request_hash
            or context["baseline_source_hash"] != run.request.baseline_source_hash
            or context["source_content_hash"] != _hash(context["source"].encode("utf-8"))
        ):
            raise SourceArtifactError("Messages input Request or source binding changed")
        assert receipt.raw_output_uri is not None and receipt.raw_output_hash is not None
        if receipt.raw_output_bytes > MAX_RESPONSE_BYTES:
            raise SourceArtifactError("Messages response exceeds ingestion limit")
        # Reread after Store validation and verify the exact bytes actually parsed.
        raw_path = file_uri_to_path(receipt.raw_output_uri)
        with raw_path.open("rb") as stream:
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if _hash(raw) != receipt.raw_output_hash or len(raw) != receipt.raw_output_bytes:
            raise SourceArtifactError("Messages raw output changed before parsing")
        batch_id = uuid5(attempt.attempt_id, f"messages-batch-v1:{receipt_ref.content_hash}")
        batch_fields = {
            "batch_id": batch_id,
            "request_id": run.request.request_id,
            "request_hash": run.request_hash,
            "generation_run_id": run.generation_run_id,
            "generator_id": generator.generator_id,
            "adapter_provenance": self.provenance,
            "raw_output_uri": receipt.raw_output_uri,
            "raw_output_hash": receipt.raw_output_hash,
            "output_bytes": receipt.raw_output_bytes,
            "token_count": execution.tokens_consumed,
            "attempt_count": attempt.attempt_number,
            "wall_seconds": execution.wall_seconds_consumed,
            "started_at": attempt.started_at,
            "finished_at": attempt.started_at + timedelta(seconds=execution.wall_seconds_consumed),
            "synthetic": (
                execution.synthetic or execution.runner_provenance.implementation_kind == "fake"
            ),
        }
        # Validate cross-attempt identity BEFORE parsing or writing any Proposal.
        failed = CandidateProposalBatch(
            **batch_fields,
            status="failed",
            error_code="invalid_model_proposal",
            error_message="Model output did not satisfy the bounded Proposal contract",
        )
        validate_runner_receipt(run, attempt, generator, receipt, failed)
        if not usage_is_within_reservation(
            actual_usage_for_runner_receipt(receipt, proposal_count=0), attempt.reserved
        ):
            raise SourceArtifactError("Messages execution exceeds its frozen reservation")
        try:
            reply = strict_json(raw)
            if not isinstance(reply, dict) or usage_tokens(reply) != execution.tokens_consumed:
                raise MessagesError("provider_usage_binding_mismatch")
            parsed = _ProposalsText.model_validate(
                strict_json(proposal_text(reply, expected_model=envelope["provider"]["model"]))
            )
            if not parsed.proposals:
                return self.batch_store.publish(
                    CandidateProposalBatch(
                        **batch_fields,
                        status="failed",
                        error_code="no_model_proposals",
                        error_message="Model returned no optimization proposals",
                    )
                )
            if len(parsed.proposals) > min(generator.max_proposals, run.request.max_proposals):
                raise MessagesError("model_proposal_limit")
            proposals = []
            patches = []
            for ordinal, item in enumerate(parsed.proposals):
                patch = (
                    item.patch.encode("utf-8", errors="strict")
                    if item.patch is not None
                    else build_single_replacement_patch(
                        context["source"].encode("utf-8"),
                        expected_path=context["source_path"],
                        old_text=item.old_text or "",
                        new_text=item.new_text or "",
                    )
                )
                # Reuse C's existing no-execution parser and baseline applicability check.
                apply_single_file_unified_patch(
                    context["source"].encode("utf-8"),
                    patch,
                    expected_path=context["source_path"],
                )
                proposal = CandidateProposal(
                    proposal_id=uuid5(batch_id, f"proposal:{ordinal}"),
                    request_id=run.request.request_id,
                    request_hash=run.request_hash,
                    generation_run_id=run.generation_run_id,
                    generator_id=generator.generator_id,
                    ordinal=ordinal,
                    optimization_intent=item.optimization_intent,
                    rationale=item.rationale,
                    risk_summary=item.risk_summary,
                    patch_uri="pending://deployment-store",
                    patch_hash=raw_patch_hash_v1(patch),
                    normalized_patch_hash=normalized_patch_hash_v1(patch),
                    touched_paths=(context["source_path"],),
                    replacement_point=run.request.replacement_point,
                )
                proposals.append(proposal)
                patches.append(patch)
        except (MessagesError, ValidationError, SourceArtifactError, UnicodeError):
            # No partial success: one malformed member rejects the whole response.
            # A can settle this failed Batch with the original successful Receipt.
            return self.batch_store.publish(failed)
        for index, patch in enumerate(patches):
            stored = self.patch_store.publish(patch)
            proposals[index] = proposals[index].model_copy(update={"patch_uri": stored.uri})
        batch = CandidateProposalBatch(**batch_fields, status="succeeded", proposals=proposals)
        validate_runner_receipt(run, attempt, generator, receipt, batch)
        return self.batch_store.publish(batch)
