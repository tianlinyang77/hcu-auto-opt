# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Bounded Messages dispatch through A's existing claim and settlement APIs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

from hcuopt.adapters.agent_generator import _publish_once
from hcuopt.adapters.agent_runner import AgentInputFile
from hcuopt.adapters.messages_generator import MessagesSettings, messages_profile_for_input
from hcuopt.agent.identity import apex_generation_plan_hash, candidate_generation_request_hash
from hcuopt.agent.messages_worker import MessagesAttemptResult, MessagesGenerationWorker
from hcuopt.contracts.agent_v1 import GenerationAttemptClaim, GenerationRunStatusView
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.generators import anthropic_messages
from hcuopt.measurement.evidence import canonical_json_bytes


class MessagesDispatchService:
    """One explicitly selected, single-generator Run; no daemon or auto-retry."""

    def __init__(self, repository: Any, worker: MessagesGenerationWorker) -> None:
        self.repository = repository
        self.worker = worker

    def run_once(
        self,
        generation_run_id: UUID,
        prepared_input: AgentInputFile,
        *,
        worker_id: str,
        api_key: str,
        lease_seconds: int | None = None,
    ) -> GenerationRunStatusView:
        status = self.repository.generation_run_status(generation_run_id)
        run = status.run
        # Refuse incompatible deployment inputs before consuming a reservation.
        if len(run.plan.generators) != 1:
            raise SourceArtifactError("Messages dispatch requires one frozen generator")
        generator = run.plan.generators[0]
        lease_seconds = generator.timeout_seconds if lease_seconds is None else lease_seconds
        artifact = Path(anthropic_messages.__file__).resolve()
        if (
            run.generation_run_id != generation_run_id
            or run.request_hash != candidate_generation_request_hash(run.request)
            or run.plan_hash != apex_generation_plan_hash(run.plan)
            or generator.adapter_profile != messages_profile_for_input(prepared_input)
            or generator.generator_artifact_hash
            != "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
            or prepared_input.path != anthropic_messages.INPUT_NAME
            or len(prepared_input.content) > anthropic_messages.MAX_INPUT_BYTES
        ):
            raise SourceArtifactError("Messages dispatch rejected input or frozen authority")
        envelope = anthropic_messages.strict_json(prepared_input.content)
        settings = MessagesSettings.model_validate(envelope["provider"])
        if (
            envelope["context"]["request_hash"] != run.request_hash
            or settings.timeout_seconds + 7 > generator.timeout_seconds
            or settings.max_output_tokens > generator.max_tokens_per_attempt
            or lease_seconds <= generator.timeout_seconds - 3
            or lease_seconds > min(generator.timeout_seconds, 7200)
            or not worker_id.strip()
            or worker_id.strip() != worker_id
            or not api_key
            or any(char in api_key for char in "\r\n\x00")
        ):
            raise SourceArtifactError("Messages dispatch needs valid budget, lease and credential")
        claim = self.repository.claim_generation_attempt(
            worker_id,
            generation_run_id=generation_run_id,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            return self.repository.generation_run_status(generation_run_id)
        if claim.run.generation_run_id != generation_run_id:
            raise SourceArtifactError("Messages dispatch received a cross-run Claim")
        _publish_once(self._claim_path(claim.attempt.attempt_id), canonical_json_bytes(claim))
        result = self.worker.execute_claim(claim, prepared_input, api_key=api_key)
        return self._settle(claim, result)

    def recover(self, generation_run_id: UUID, attempt_id: UUID) -> GenerationRunStatusView:
        """No credential or network path. A alone decides replay versus stale lease."""
        claim = GenerationAttemptClaim.model_validate_json(
            self._claim_path(attempt_id).read_bytes()
        )
        if (
            claim.run.generation_run_id != generation_run_id
            or claim.attempt.attempt_id != attempt_id
        ):
            raise SourceArtifactError("Messages recovery Claim crosses Run or Attempt")
        current = self.repository.generation_run_status(generation_run_id)
        attempt = next((item for item in current.attempts if item.attempt_id == attempt_id), None)
        if (
            attempt is None
            or attempt.claim_token != claim.attempt.claim_token
            or current.run.request_hash != claim.run.request_hash
            or current.run.plan_hash != claim.run.plan_hash
            or attempt.state not in {"running", "succeeded", "failed"}
        ):
            raise SourceArtifactError("Messages recovery authority is stale")
        item = AgentInputFile(
            path=anthropic_messages.INPUT_NAME,
            content=(self._claim_path(attempt_id).parent / "input.json").read_bytes(),
        )
        return self._settle(claim, self.worker.recover_claim(claim, item))

    def _claim_path(self, attempt_id: UUID) -> Path:
        return self.worker.root / "attempts" / str(attempt_id) / "claim.json"

    def _settle(
        self, claim: GenerationAttemptClaim, result: MessagesAttemptResult
    ) -> GenerationRunStatusView:
        assert claim.attempt.claim_token is not None
        self.repository.settle_generation_attempt(
            claim.attempt.attempt_id,
            claim.attempt.claim_token,
            result.batch.batch if result.batch is not None else None,
            result.receipt_ref,
            runner_receipt_reader=self.worker.receipts,
            batch_uri=result.batch.uri if result.batch is not None else None,
        )
        return self.repository.generation_run_status(claim.run.generation_run_id)
