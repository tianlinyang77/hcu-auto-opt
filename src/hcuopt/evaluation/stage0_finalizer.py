from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hcuopt.domain.enums import ProjectMode
from hcuopt.evaluation.stage0_protocol import load_registered_stage0_protocol
from hcuopt.evaluation.stage0_verifier import (
    Stage0EvidenceReader,
    Stage0ProbeEvidenceReference,
    Stage0VerificationContext,
    Stage0VerificationResult,
    Stage0Verifier,
)
from hcuopt.measurement.evidence import write_evidence, write_evidence_bytes

FORMAL_STAGE0_SCOPE_WARNING = (
    "MDE 仅绑定本次 Target、Workload、指标、协议和样本预算，不是机器永久属性；"
    "Stage 0 不构成优化收益或自动发布授权。"
)


@dataclass(frozen=True, slots=True)
class Stage0ReportArtifacts:
    machine_report_uri: str
    machine_report_hash: str
    markdown_report_uri: str
    markdown_report_hash: str


class Stage0FinalizationService(Protocol):
    """Deployment boundary between persisted references and Formal authority."""

    def verify(
        self,
        context: Stage0VerificationContext,
        references: tuple[Stage0ProbeEvidenceReference, ...],
        *,
        protocol_version: str,
    ) -> Stage0VerificationResult: ...

    def publish_report(
        self,
        context: Stage0VerificationContext,
        verification: Stage0VerificationResult,
        *,
        mode: ProjectMode,
        reasons: tuple[str, ...],
        accepted_target_risks: tuple[str, ...],
    ) -> Stage0ReportArtifacts: ...


class FileStage0Finalizer:
    """Read raw evidence below one trusted root and publish immutable reports."""

    def __init__(self, evidence_root: Path) -> None:
        self.evidence_root = evidence_root.resolve(strict=True)
        if not self.evidence_root.is_dir():
            raise ValueError("Stage 0 evidence root must be a directory")

    def verify(
        self,
        context: Stage0VerificationContext,
        references: tuple[Stage0ProbeEvidenceReference, ...],
        *,
        protocol_version: str,
    ) -> Stage0VerificationResult:
        protocol = load_registered_stage0_protocol(protocol_version)
        reader = Stage0EvidenceReader(self.evidence_root)
        return Stage0Verifier(protocol, reader).verify(context, references)

    def publish_report(
        self,
        context: Stage0VerificationContext,
        verification: Stage0VerificationResult,
        *,
        mode: ProjectMode,
        reasons: tuple[str, ...],
        accepted_target_risks: tuple[str, ...],
    ) -> Stage0ReportArtifacts:
        report_root = (
            self.evidence_root
            / "reports"
            / str(context.stage0_run_id)
            / verification.input_digest.removeprefix("sha256:")
        )
        machine_report = write_evidence(
            report_root / "stage0-report.json",
            {
                "schema_version": "stage0-formal-report-v1",
                "task_id": str(context.task_id),
                "stage0_run_id": str(context.stage0_run_id),
                "target_snapshot_id": str(context.target_snapshot_id),
                "target_id": context.target.target_id,
                "workload_id": context.workload_id,
                "adapter_profile": context.adapter_profile,
                "mode": mode.value,
                "reasons": list(reasons),
                "accepted_target_risks": list(accepted_target_risks),
                "automatic_release_allowed": False,
                "scope_warning": FORMAL_STAGE0_SCOPE_WARNING,
                "verification": verification.model_dump(mode="json"),
            },
        )
        markdown_report = write_evidence_bytes(
            report_root / "stage0-report.md",
            _render_markdown(
                context,
                verification,
                mode=mode,
                reasons=reasons,
                accepted_target_risks=accepted_target_risks,
            ).encode("utf-8"),
        )
        return Stage0ReportArtifacts(
            machine_report_uri=machine_report.uri,
            machine_report_hash=machine_report.sha256,
            markdown_report_uri=markdown_report.uri,
            markdown_report_hash=markdown_report.sha256,
        )

def _render_markdown(
    context: Stage0VerificationContext,
    verification: Stage0VerificationResult,
    *,
    mode: ProjectMode,
    reasons: tuple[str, ...],
    accepted_target_risks: tuple[str, ...],
) -> str:
    reason_lines = [f"- {reason}" for reason in reasons] or ["- 无"]
    risk_lines = [f"- `{risk}`" for risk in accepted_target_risks] or ["- 无"]
    failure_lines = [f"- `{code}`" for code in verification.failure_codes] or ["- 无"]
    return "\n".join(
        [
            "# Stage 0 Formal 验证报告",
            "",
            f"- Task：`{context.task_id}`",
            f"- Stage0Run：`{context.stage0_run_id}`",
            f"- Target Snapshot：`{context.target_snapshot_id}`",
            f"- Target：`{context.target.target_id}`",
            f"- Workload：`{context.workload_id}`",
            f"- 协议：`{verification.protocol_version}`",
            f"- 协议 Hash：`{verification.protocol_hash}`",
            f"- 输入证据 Hash：`{verification.input_digest}`",
            "",
            "## 闸门结论",
            "",
            f"- G0-M 测量可信度：`{verification.measurement.value}`",
            f"- G0-P Profiler：`{verification.profiler.value}`",
            f"- G0-H 热补丁/Overlay：`{verification.hot_patch.value}`",
            f"- 项目模式：`{mode.value}`",
            "- 自动发布：`false`",
            "",
            "## 判决原因",
            "",
            *reason_lines,
            "",
            "## 验证失败代码",
            "",
            *failure_lines,
            "",
            "## 已接受的 Target 风险",
            "",
            *risk_lines,
            "",
            f"> {FORMAL_STAGE0_SCOPE_WARNING}",
            "",
        ]
    )
