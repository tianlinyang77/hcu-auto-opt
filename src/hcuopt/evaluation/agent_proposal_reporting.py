# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from hcuopt.contracts.agent_verification_v1 import (
    AgentGenerationEvidenceSummaryV1,
    AgentProposalVerificationContext,
    AgentProposalVerificationResult,
)
from hcuopt.contracts.platform_v1 import EvidenceBundle
from hcuopt.evaluation.agent_proposal_verifier import build_agent_generation_read_model
from hcuopt.measurement.evidence import (
    EvidenceArtifact,
    write_evidence,
    write_evidence_bytes,
)

AGENT_GENERATION_WARNING = (
    "本报告仅用于 Scripted/开发环境的 Proposal 人工评审；不构成性能结论、"
    "Formal Intake、HCU 运行或自动发布授权。"
)


def build_agent_generation_evidence(
    context: AgentProposalVerificationContext,
    result: AgentProposalVerificationResult,
) -> EvidenceBundle:
    summary = AgentGenerationEvidenceSummaryV1(
        generation_run_id=context.generation_run_id,
        input_digest=result.input_digest,
        knowledge_hash=result.knowledge_hash,
        request_hash=result.request_hash,
        plan_hash=result.plan_hash,
        status=result.status,
        budget=result.budget,
        kept_proposal_ids=[item.proposal_id for item in result.proposals if item.status == "kept"],
        eliminated_proposal_ids=[
            item.proposal_id for item in result.proposals if item.status == "eliminated"
        ],
        attempt_ids=[item.attempt_id for item in result.attempts],
        failure_codes=result.failure_codes,
        human_review_status=result.human_review_status,
        package_promotion_status=result.package_promotion_status,
        formal_readiness=result.formal_readiness,
    )
    evidence_id = uuid5(
        NAMESPACE_URL,
        f"hcuopt:m2b-generation:{context.task_id}:{context.generation_run_id}:"
        f"{result.input_digest}",
    )
    return EvidenceBundle(
        evidence_id=evidence_id,
        task_id=context.task_id,
        baseline_epoch_id=context.baseline_epoch_id,
        target_id=context.target_id,
        evidence_type="m2b_generation",
        protocol_version="m2b-proposal-verifier-v1",
        summary=summary.model_dump(mode="json"),
        raw_uris=list(result.evidence_uris),
        adapter_provenance=list(result.adapter_provenance),
        synthetic=True,
        created_at=result.evidence_created_at,
    )


def write_agent_generation_report(
    root: Path,
    context: AgentProposalVerificationContext,
    result: AgentProposalVerificationResult,
) -> dict[str, dict[str, object]]:
    if root.is_symlink():
        raise ValueError("generation report root cannot be a symlink")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("generation report root must be a directory")
    bundle = build_agent_generation_evidence(context, result)
    artifacts: dict[str, EvidenceArtifact] = {}
    artifacts["verification.json"] = write_evidence(root / "verification.json", result)
    artifacts["evidence-bundle.json"] = write_evidence(root / "evidence-bundle.json", bundle)
    artifacts["read-model.json"] = write_evidence(
        root / "read-model.json", build_agent_generation_read_model(result)
    )
    artifacts["report.md"] = write_evidence_bytes(
        root / "report.md", _render_report(result).encode("utf-8")
    )
    manifest = {
        "schema_version": "m2b-generation-manifest-v1",
        "files": {name: _entry(item) for name, item in sorted(artifacts.items())},
    }
    artifacts["sha256sums.json"] = write_evidence(root / "sha256sums.json", manifest)
    return {name: _entry(item) for name, item in sorted(artifacts.items())}


def _entry(artifact: EvidenceArtifact) -> dict[str, object]:
    return {
        "uri": artifact.uri,
        "sha256": artifact.sha256,
        "byte_count": artifact.byte_count,
    }


def _render_report(result: AgentProposalVerificationResult) -> str:
    proposal_lines = [
        f"- `{item.proposal_id}`: **{item.status}**"
        + (f" (`{item.reason_code}`)" if item.reason_code else "")
        for item in result.proposals
    ] or ["- 无 Proposal"]
    failure_lines = [f"- `{item}`" for item in result.failure_codes] or ["- 无"]
    return "\n".join(
        [
            "# M2b Proposal 独立验证报告",
            "",
            f"- 状态：`{result.status}`",
            f"- Input Digest：`{result.input_digest}`",
            f"- Attempt 数：`{result.budget.attempt_count}`",
            f"- Proposal 数：`{result.budget.proposal_count}`",
            f"- 人工审核：`{result.human_review_status}`",
            f"- Package 晋级：`{result.package_promotion_status}`",
            f"- Formal Readiness：`{result.formal_readiness}`",
            "- 性能结论：`not_measured`",
            "- 自动发布：`false`",
            "",
            "## Proposal 裁决",
            "",
            *proposal_lines,
            "",
            "## 失败与淘汰原因",
            "",
            *failure_lines,
            "",
            AGENT_GENERATION_WARNING,
            "",
        ]
    )
