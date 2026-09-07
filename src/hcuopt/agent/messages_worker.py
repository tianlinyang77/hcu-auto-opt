# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""One claimed Messages attempt; A remains the sole scheduler and settlement owner."""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hcuopt.adapters.agent_generator import (
    CandidateProposalBatchStore,
    ProposalPatchStore,
    StoredProposalBatch,
    _publish_once,
)
from hcuopt.adapters.agent_runner import (
    AgentInputFile,
    AgentRunLimits,
    AgentRunRequest,
    LocalCommandAgentRunner,
)
from hcuopt.adapters.agent_runner_receipt import RunnerExecutionReceiptStore
from hcuopt.adapters.messages_generator import (
    MessagesProposalIngestor,
    MessagesSettings,
    messages_input_manifest_hash,
    messages_profile_for_input,
)
from hcuopt.agent.authority import reserved_usage_for, validate_runner_receipt
from hcuopt.agent.identity import apex_generation_plan_hash, candidate_generation_request_hash
from hcuopt.contracts.agent_runner_v1 import RunnerExecutionReceiptRef
from hcuopt.contracts.agent_v1 import GenerationAttemptClaim
from hcuopt.domain.errors import SourceArtifactError
from hcuopt.generators import anthropic_messages
from hcuopt.measurement.evidence import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class MessagesAttemptResult:
    receipt_ref: RunnerExecutionReceiptRef
    batch: StoredProposalBatch | None
    prepared_input_uri: str


class MessagesGenerationWorker:
    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.receipts = RunnerExecutionReceiptStore(self.root / "runner")
        self.patches = ProposalPatchStore(self.root / "proposals", profile="m2b-messages-store-v1")
        self.batches = CandidateProposalBatchStore(
            self.root / "proposals", profile="m2b-messages-store-v1"
        )

    def execute_claim(
        self,
        claim: GenerationAttemptClaim,
        prepared_input: AgentInputFile,
        *,
        api_key: str,
    ) -> MessagesAttemptResult:
        claim = GenerationAttemptClaim.model_validate(claim.model_dump(mode="json"))
        run, attempt, generator = claim.run, claim.attempt, claim.generator
        artifact = Path(anthropic_messages.__file__).resolve()
        artifact_hash = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        if (
            run.state != "running"
            or run.request_hash != candidate_generation_request_hash(run.request)
            or run.plan_hash != apex_generation_plan_hash(run.plan)
            or generator.generator_artifact_hash != artifact_hash
            or generator.adapter_profile != messages_profile_for_input(prepared_input)
            or attempt.adapter_profile != generator.adapter_profile
            or attempt.reserved != reserved_usage_for(generator)
        ):
            raise SourceArtifactError(
                "Messages worker rejected changed input, artifact or authority"
            )
        if prepared_input.path != anthropic_messages.INPUT_NAME:
            raise SourceArtifactError("Messages worker requires the fixed input name")
        if len(prepared_input.content) > anthropic_messages.MAX_INPUT_BYTES:
            raise SourceArtifactError("Messages worker input exceeds its byte limit")
        envelope = anthropic_messages.strict_json(prepared_input.content)
        if (
            not isinstance(envelope, dict)
            or envelope["context"]["request_hash"] != run.request_hash
        ):
            raise SourceArtifactError("Messages input Request differs from the claimed authority")
        settings = MessagesSettings.model_validate(envelope["provider"])
        # A caps the Claim lease at the frozen generator timeout. Reserve five
        # seconds INSIDE that timeout for publication and DB settlement.
        runner_timeout = generator.timeout_seconds - 5
        if (
            settings.timeout_seconds + 2 > runner_timeout
            or settings.max_output_tokens > generator.max_tokens_per_attempt
            or attempt.attempt_number > generator.max_attempts
            or attempt.lease_expires_at is None
            or attempt.lease_expires_at <= self.clock() + timedelta(seconds=runner_timeout + 2)
        ):
            raise SourceArtifactError(
                "Messages attempt needs sufficient frozen budget and claim lease"
            )
        input_hash = hashlib.sha256(prepared_input.content).hexdigest()
        if not api_key or any(char in api_key for char in "\r\n\x00"):
            raise SourceArtifactError("Messages worker requires a valid deployment credential")
        attempt_root = self.root / "attempts" / str(attempt.attempt_id)
        attempt_root.mkdir(parents=True, exist_ok=True)
        try:
            # An uncertain or completed invocation must not incur another provider
            # charge. Recover its Receipt, or let A expire/settle and retry with a
            # NEW Attempt. This is not a replacement for A's DB claim/fencing.
            with (attempt_root / "started.json").open("xb") as marker:
                marker.write(
                    canonical_json_bytes(
                        {
                            "attempt_id": str(attempt.attempt_id),
                            "request_hash": run.request_hash,
                            "input_hash": "sha256:" + input_hash,
                        }
                    )
                )
        except FileExistsError as error:
            raise SourceArtifactError(
                "Messages attempt already started; recover, do not rerun"
            ) from error
        input_path = self.root / "inputs" / input_hash / anthropic_messages.INPUT_NAME
        _publish_once(input_path, prepared_input.content)
        _publish_once(attempt_root / "input.json", prepared_input.content)
        runner = LocalCommandAgentRunner(
            allowed_executables=(Path(sys.executable),),
            allowed_argv_prefixes=((str(artifact),),),
            allowed_environment_names=frozenset({"HCUOPT_MODEL_API_KEY"}),
        )
        request = AgentRunRequest(
            attempt_id=attempt.attempt_id,
            generation_run_id=run.generation_run_id,
            request_id=run.request.request_id,
            request_hash=run.request_hash,
            plan_id=run.plan.plan_id,
            generator_id=generator.generator_id,
            executable=Path(sys.executable),
            generator_artifact=artifact,
            generator_artifact_hash=artifact_hash,
            argv=(str(artifact),),
            input_files=(prepared_input,),
            environment=(("HCUOPT_MODEL_API_KEY", api_key),),
            limits=AgentRunLimits(
                attempt_number=attempt.attempt_number,
                timeout_seconds=float(runner_timeout),
                max_stdout_bytes=generator.max_output_bytes_per_attempt,
                max_stderr_bytes=min(8192, generator.max_output_bytes_per_attempt),
                max_total_output_bytes=generator.max_output_bytes_per_attempt,
                max_tokens=generator.max_tokens_per_attempt,
            ),
        )
        result = runner.run(request, self.root / "work")
        receipt_ref = self.receipts.publish(result)
        # Preserve the index even if later ingestion finds invalid authority.
        index = self.root / "attempts" / str(attempt.attempt_id) / "receipt-ref.json"
        _publish_once(index, canonical_json_bytes(receipt_ref))
        return self.recover_claim(claim, prepared_input)

    def recover_claim(
        self, claim: GenerationAttemptClaim, prepared_input: AgentInputFile
    ) -> MessagesAttemptResult:
        """Rebuild from immutable evidence only; never execute or renew a lease."""
        claim = GenerationAttemptClaim.model_validate(claim.model_dump(mode="json"))
        attempt_root = self.root / "attempts" / str(claim.attempt.attempt_id)
        if (attempt_root / "input.json").read_bytes() != prepared_input.content:
            raise SourceArtifactError("Messages recovery input differs from executed input")
        receipt_ref = RunnerExecutionReceiptRef.model_validate_json(
            (attempt_root / "receipt-ref.json").read_bytes()
        )
        receipt = self.receipts.load(receipt_ref)
        if (
            claim.run.request_hash != candidate_generation_request_hash(claim.run.request)
            or claim.run.plan_hash != apex_generation_plan_hash(claim.run.plan)
            or claim.generator.adapter_profile != messages_profile_for_input(prepared_input)
            or receipt.execution.input_manifest_hash != messages_input_manifest_hash(prepared_input)
        ):
            raise SourceArtifactError("Messages recovery input or authority changed")
        # Successful executions need a Batch for validation; ingestion validates
        # that case. Failed executions must still bind to the original Claim.
        batch = None
        if receipt.execution.status == "succeeded":
            batch = MessagesProposalIngestor(
                receipt_store=self.receipts,
                patch_store=self.patches,
                batch_store=self.batches,
                profile=claim.generator.adapter_profile,
            ).ingest(claim.run, claim.attempt, receipt_ref, prepared_input=prepared_input)
        else:
            validate_runner_receipt(claim.run, claim.attempt, claim.generator, receipt, None)
        input_hash = hashlib.sha256(prepared_input.content).hexdigest()
        input_path = self.root / "inputs" / input_hash / anthropic_messages.INPUT_NAME
        return MessagesAttemptResult(receipt_ref, batch, input_path.resolve().as_uri())
