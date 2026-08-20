from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from hcuopt.domain.enums import Stage0RunMode, Stage0RunState, TaskState
from hcuopt.domain.errors import Conflict
from hcuopt.domain.models import Stage0Report
from hcuopt.evaluation.stage0_inputs import (
    Stage0FinalizationSnapshot,
    build_verification_inputs,
    stage0_snapshot_digest,
)
from hcuopt.evaluation.stage0_protocol import (
    LoadedStage0Protocol,
    load_registered_stage0_protocol,
)
from hcuopt.evaluation.stage0_reporting import (
    Stage0ReportArtifacts,
    write_stage0_verification_report,
)
from hcuopt.evaluation.stage0_verifier import (
    Stage0EvidenceError,
    Stage0EvidenceReader,
    Stage0VerificationContext,
    Stage0VerificationResult,
    Stage0Verifier,
)
from hcuopt.stage0 import evaluate_stage0


class Stage0FinalizationRepository(Protocol):
    def load_stage0_finalization_snapshot(
        self, stage0_run_id: UUID
    ) -> Stage0FinalizationSnapshot: ...

    def commit_stage0_finalization(
        self,
        stage0_run_id: UUID,
        *,
        expected_snapshot_digest: str,
        verification: Stage0VerificationResult,
        report: dict[str, Any],
    ) -> dict[str, Any]: ...

    def fail_stage0_finalization(
        self,
        stage0_run_id: UUID,
        *,
        expected_snapshot_digest: str,
        error_code: str,
        message: str,
    ) -> dict[str, Any]: ...


ProtocolLoader = Callable[[str], LoadedStage0Protocol]
ReaderFactory = Callable[[Path], Stage0EvidenceReader]
VerifierFactory = Callable[[LoadedStage0Protocol, Stage0EvidenceReader], Stage0Verifier]
ReportWriter = Callable[
    [Path, Stage0VerificationContext, Stage0VerificationResult, Stage0Report],
    Stage0ReportArtifacts,
]


class Stage0FinalizationInfrastructureError(RuntimeError):
    """The server cannot currently perform Formal verification safely."""


class Stage0FinalizationService:
    """Finalize Stage 0 without holding a database lock across evidence I/O."""

    def __init__(
        self,
        repository: Stage0FinalizationRepository,
        results_root: Path,
        *,
        protocol_loader: ProtocolLoader = load_registered_stage0_protocol,
        reader_factory: ReaderFactory = Stage0EvidenceReader,
        verifier_factory: VerifierFactory = Stage0Verifier,
        report_writer: ReportWriter = write_stage0_verification_report,
    ) -> None:
        self.repository = repository
        self.results_root = Path(results_root).absolute()
        self.protocol_loader = protocol_loader
        self.reader_factory = reader_factory
        self.verifier_factory = verifier_factory
        self.report_writer = report_writer

    def finalize(self, stage0_run_id: UUID) -> dict[str, Any]:
        snapshot = self.repository.load_stage0_finalization_snapshot(stage0_run_id)
        if snapshot.run.state is Stage0RunState.FINALIZED:
            if snapshot.run.report is None:
                raise Conflict("finalized Stage 0 run has no persisted report")
            return snapshot.run.report
        if snapshot.run.state is Stage0RunState.FAILED:
            raise Stage0EvidenceError(
                "stage0_run_failed",
                "Stage 0 finalization previously failed evidence validation",
            )
        snapshot_digest = stage0_snapshot_digest(snapshot)
        if snapshot.run.mode is not Stage0RunMode.FORMAL:
            error = Stage0EvidenceError(
                "dry_run_not_formal",
                "Dry Run evidence cannot authorize formal Stage 0",
            )
            self.repository.fail_stage0_finalization(
                stage0_run_id,
                expected_snapshot_digest=snapshot_digest,
                error_code=error.code,
                message=str(error),
            )
            raise error
        if snapshot.run.state is not Stage0RunState.READY:
            raise Conflict("Stage 0 probe barrier is not ready")
        if snapshot.task.state is not TaskState.STAGE0_PENDING:
            raise Conflict("formal Stage 0 requires a stage0_pending task")
        if snapshot.task.stage0_authority != "none":
            raise Conflict("formal Stage 0 requires a task without prior authority")

        try:
            protocol = self._load_protocol(snapshot.run.protocol_version)
            run_root = self.results_root / str(stage0_run_id)
            context, references = build_verification_inputs(
                snapshot,
                protocol,
                run_root=run_root,
            )
            reader = self._build_reader(run_root)
            verifier = self.verifier_factory(protocol, reader)
            verification = verifier.verify(context, tuple(references.values()))
        except Stage0EvidenceError as exc:
            if exc.code in {"evidence_platform_unsupported", "protocol_not_registered"}:
                raise Stage0FinalizationInfrastructureError(str(exc)) from exc
            self.repository.fail_stage0_finalization(
                stage0_run_id,
                expected_snapshot_digest=snapshot_digest,
                error_code=exc.code,
                message=str(exc),
            )
            raise

        decision = evaluate_stage0(
            verification.to_stage0_evidence(evidence_uri="stage0-report:pending")
        )
        artifacts = self.report_writer(
            run_root,
            context,
            verification,
            decision,
        )
        report = _api_report(context, verification, decision, artifacts)
        return self.repository.commit_stage0_finalization(
            stage0_run_id,
            expected_snapshot_digest=snapshot_digest,
            verification=verification,
            report=report,
        )

    def _load_protocol(self, version: str) -> LoadedStage0Protocol:
        return self.protocol_loader(version)

    def _build_reader(self, run_root: Path) -> Stage0EvidenceReader:
        return self.reader_factory(run_root)


def _api_report(
    context: Stage0VerificationContext,
    verification: Stage0VerificationResult,
    decision: Stage0Report,
    artifacts: Stage0ReportArtifacts,
) -> dict[str, Any]:
    return {
        "task_id": str(context.task_id),
        "mode": decision.mode.value,
        "reasons": list(decision.reasons),
        "automatic_release_allowed": False,
        "evidence_authority": "formal",
        "protocol_version": verification.protocol_version,
        "protocol_hash": verification.protocol_hash,
        "input_digest": verification.input_digest,
        "measurement_gate": verification.measurement.value,
        "profiler_gate": verification.profiler.value,
        "hotpatch_gate": verification.hot_patch.value,
        "failure_codes": list(verification.failure_codes),
        "json_report_uri": artifacts.verification_json.uri,
        "json_report_hash": artifacts.verification_json.sha256,
        "markdown_report_uri": artifacts.verification_markdown.uri,
        "markdown_report_hash": artifacts.verification_markdown.sha256,
        "manifest_uri": artifacts.sha256sums.uri,
        "manifest_hash": artifacts.sha256sums.sha256,
    }
