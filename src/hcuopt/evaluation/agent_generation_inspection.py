# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Pre-review D inspection, separate from immutable terminal publication/signoff."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from hcuopt.adapters.agent_generator import _publish_once
from hcuopt.adapters.agent_knowledge import KnowledgeSnapshotStore
from hcuopt.agent.identity import knowledge_snapshot_hash
from hcuopt.contracts.agent_v1 import GenerationRunStatusView
from hcuopt.contracts.agent_verification_v1 import (
    AgentGenerationReadModel,
    AgentProposalVerificationContext,
)
from hcuopt.contracts.base import ContractModel
from hcuopt.evaluation.agent_generation_read_model import AgentGenerationReadModelError
from hcuopt.evaluation.agent_proposal_verifier import (
    AgentProposalVerifier,
    build_agent_generation_read_model,
)
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.measurement.evidence import canonical_json_bytes


class AgentGenerationInspection(ContractModel):
    schema_version: Literal["m2b-agent-inspection-v1"] = "m2b-agent-inspection-v1"
    generation_run_id: UUID
    generation_state: Literal["awaiting_review", "failed"]
    inspection_kind: Literal["pre_signoff_read_only"] = "pre_signoff_read_only"
    # D v1's Synthetic label classifies development evidence, not whether the
    # Runner implementation or provider was real. Never infer model quality.
    evidence_scope: Literal["development_only_not_performance"] = "development_only_not_performance"
    read_model: AgentGenerationReadModel
    formal_intake_allowed: Literal[False] = False
    automatic_release_allowed: Literal[False] = False


class AgentGenerationInspectionService:
    def __init__(self, repository: Any, root: Path) -> None:
        self.repository = repository
        self.root = root.resolve(strict=True)
        self.reader = HashedEvidenceReader(self.root)

    def prepare(
        self,
        generation_run_id: UUID,
        *,
        knowledge_store: KnowledgeSnapshotStore,
        task_id: UUID,
        target_id: str,
    ) -> AgentGenerationInspection:
        """Deployment-only snapshot creation; never write A state or a review record."""
        status = self.repository.generation_run_status(generation_run_id)
        self._require_settled(status)
        run = status.run
        knowledge = knowledge_store.load(
            run.request.knowledge_snapshot_id, run.request.knowledge_snapshot_hash
        ).snapshot
        directory = self.root / "inspections" / str(generation_run_id)

        def save(name, model):
            payload = canonical_json_bytes(model)
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            path = directory / digest.removeprefix("sha256:") / name
            _publish_once(path, payload)
            return {"uri": path.as_uri(), "content_hash": digest}

        context = AgentProposalVerificationContext(
            task_id=task_id,
            target_id=target_id,
            generation_run_id=generation_run_id,
            baseline_epoch_id=run.request.baseline_epoch_id,
            knowledge={
                **save("knowledge.json", knowledge),
                "identity_hash": knowledge_snapshot_hash(knowledge),
            },
            request={**save("request.json", run.request), "identity_hash": run.request_hash},
            plan={**save("plan.json", run.plan), "identity_hash": run.plan_hash},
            generation_status=save("status.json", status),
        )
        # Verify all referenced artifacts before making the descriptor readable.
        self._inspect(context, status)
        _publish_once(directory / "context.json", canonical_json_bytes(context))
        return self.get(generation_run_id)

    def get(self, generation_run_id: UUID) -> AgentGenerationInspection:
        path = self.root / "inspections" / str(generation_run_id) / "context.json"
        # Deployment-owned fixed path; HTTP callers cannot supply a URI/context.
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise AgentGenerationReadModelError("agent_inspection_path_invalid", "unsafe context")
        payload = HashedEvidenceReader(self.root, max_bytes=1024 * 1024)._secure_read(path)
        context = AgentProposalVerificationContext.model_validate_json(payload)
        if context.generation_run_id != generation_run_id:
            raise AgentGenerationReadModelError(
                "agent_inspection_run_mismatch", "context crosses Run"
            )
        status = self.repository.generation_run_status(generation_run_id)
        return self._inspect(context, status)

    def _inspect(self, context, status):
        self._require_settled(status)
        stored = GenerationRunStatusView.model_validate_json(
            self.reader.read_bytes(
                context.generation_status.uri, context.generation_status.content_hash
            )
        )
        if stored != status:
            raise AgentGenerationReadModelError("agent_inspection_status_drift", "A status changed")
        result = AgentProposalVerifier(self.reader).verify(context)
        if self.repository.generation_run_status(context.generation_run_id) != status:
            raise AgentGenerationReadModelError(
                "agent_inspection_status_drift", "A changed during read"
            )
        return AgentGenerationInspection(
            generation_run_id=context.generation_run_id,
            generation_state=status.run.state,
            read_model=build_agent_generation_read_model(result),
        )

    @staticmethod
    def _require_settled(status):
        if status.run.state not in {"awaiting_review", "failed"} or any(
            item.state not in {"succeeded", "failed", "cancelled"} for item in status.attempts
        ):
            raise AgentGenerationReadModelError(
                "agent_inspection_not_settled", "Attempts not settled"
            )
