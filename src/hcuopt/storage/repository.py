from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from hcuopt.contracts.m2 import (
    ArtifactFamilyFreezeRequest,
    BudgetUsage,
    RoundBudget,
    RoundBudgetLedgerEntry,
    RoundBudgetReservation,
    RoundCandidate,
    RoundCandidateBuildTerminal,
    SearchRound,
)
from hcuopt.contracts.m2_candidate_family_v1 import BusinessCandidateFamilyManifest
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextDescriptor,
    FormalAuthorityContextRef,
    FormalEvidenceStoreRef,
    FormalVerifierRef,
    formal_authority_context_ref,
)
from hcuopt.contracts.m2_formal_operator_v1 import FormalOperatorAuthoritySnapshot
from hcuopt.contracts.m2_formal_signoff_v1 import (
    FormalDecisionSignature,
    FormalRoundSignoff,
    FormalRoundSignoffArtifactPublication,
    FormalRoundSignoffIntent,
    FormalRoundSignoffRequest,
    build_formal_round_signoff_intent,
)
from hcuopt.contracts.m2_formal_start_v1 import FormalStartIntentView
from hcuopt.contracts.operator_v1 import (
    ManualOperatorHotspotRef,
    OperatorHotspotAuthorityView,
    OperatorHotspotRef,
    OperatorStartCandidateMember,
    OperatorStartIntentView,
    ProfilerOperatorHotspotRef,
    ResolvedOperatorAuthority,
    RoundPlanPreviewView,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.contracts.platform_v1 import (
    SHA256_PATTERN,
    ArtifactManifest,
    EvaluationRun,
    EvidenceBundle,
    ExecutionAttempt,
    ExecutionRequest,
    SourceSnapshot,
    TargetSpec,
)
from hcuopt.contracts.v1 import (
    BaselineCreate,
    FrameworkSmokeCreate,
    FrameworkSmokeSignoffRequest,
    JobCreate,
    ManualCandidateCreate,
    ManualCandidateSignoffRequest,
    ManualCandidateTaskCreate,
    ManualHotspotIntakeCreate,
    Stage0EvidenceRequest,
    Stage0ProbeResult,
    Stage0RunCreate,
    TaskCreate,
    WorkerRegister,
)
from hcuopt.domain.enums import (
    CandidateState,
    FrameworkSmokeDecision,
    JobState,
    JobType,
    LeaseScope,
    ManualCandidateDecision,
    ManualCandidateKind,
    ManualCandidateVerdict,
    ProjectMode,
    RoundBarrierOutcome,
    RoundBudgetEntryType,
    RoundBudgetReservationState,
    RoundCandidateState,
    RoundPhase,
    RoundTerminalReason,
    SearchRoundRunMode,
    SearchRoundState,
    Stage0ProbeType,
    Stage0RunMode,
    Stage0RunState,
    TaskState,
    WorkerType,
    WorkflowType,
)
from hcuopt.domain.errors import Conflict, NotFound, StaleClaimToken, StaleFencingToken
from hcuopt.domain.transitions import transition_candidate, transition_task
from hcuopt.evaluation.m2_authority import HoldoutRevealResult
from hcuopt.evaluation.m2_finalizer import M2ScriptedRoundFinalizationService
from hcuopt.evaluation.m2_formal_authority import (
    FormalBarrierPersistence,
    FormalEvidenceBundlePersistence,
    FormalHoldoutRevealPersistence,
    FormalMultipleComparisonPersistence,
)
from hcuopt.evaluation.m2_formal_finalizer import M2FormalRoundFinalizationService
from hcuopt.evaluation.m2_formal_signoff import (
    FormalRoundSignoffFinalizationService,
    M2FormalSignoffError,
)
from hcuopt.evaluation.m2_models import (
    MultipleComparisonResult,
    RoundBarrierResult,
    RoundEvidenceBundle,
)
from hcuopt.evaluation.m2_statistics import (
    M2_FWER_PROTOCOL_VERSION,
    SearchBarrierDecision,
    close_scripted_holdout_barrier,
    close_scripted_search_barrier,
    holdout_family_hash,
    m2_fwer_protocol_hash,
    recompute_multiple_comparison_result_hash,
)
from hcuopt.evaluation.m2_verifier import M2RoundEvidenceError, require_formal_round_signoff
from hcuopt.evaluation.stage0_finalizer import Stage0FinalizationService
from hcuopt.evaluation.stage0_protocol import (
    Stage0ProtocolError,
    load_registered_stage0_protocol,
)
from hcuopt.evaluation.stage0_verifier import (
    Stage0EvidenceError,
    Stage0ProbeEvidenceReference,
    Stage0VerificationContext,
    verification_input_digest,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator.errors import OperatorPlanHashMismatch
from hcuopt.operator.start import FrozenScriptedPlans
from hcuopt.stage0 import REQUIRED_STAGE0_PROBES, evaluate_stage0
from hcuopt.storage.agent_generation import AgentGenerationRepositoryMixin
from hcuopt.storage.formal_evidence_acceptance import FormalEvidenceAcceptanceRepositoryMixin
from hcuopt.storage.migrations import migration_plan
from hcuopt.targets import target_fingerprint


class PostgresRepository(
    FormalEvidenceAcceptanceRepositoryMixin,
    AgentGenerationRepositoryMixin,
):
    """Synchronous PostgreSQL boundary shared by API and maintenance commands.

    Each public method owns one short transaction. Long-running work happens in
    workers and never holds a database transaction open.
    """

    def __init__(
        self,
        database_url: str,
        *,
        stage0_finalizer: Stage0FinalizationService | None = None,
        m2_scripted_finalizer: M2ScriptedRoundFinalizationService | None = None,
        m2_formal_finalizer: M2FormalRoundFinalizationService | None = None,
        m2_formal_signoff_finalizer: FormalRoundSignoffFinalizationService | None = None,
    ) -> None:
        self.database_url = database_url
        self.stage0_finalizer = stage0_finalizer
        self.m2_scripted_finalizer = m2_scripted_finalizer
        self.m2_formal_finalizer = m2_formal_finalizer
        self.m2_formal_signoff_finalizer = m2_formal_signoff_finalizer

    @contextmanager
    def connection(self) -> Iterator[Connection[dict[str, Any]]]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def migrate(self) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, sql in migration_plan():
                if version not in applied:
                    connection.execute(sql)

    def create_task(self, request: TaskCreate) -> dict[str, Any]:
        if request.automatic_release_allowed:
            raise Conflict("MVP forbids automatic production release")
        task_id = uuid4()
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed
                )
                VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    task_id,
                    request.name,
                    request.workload_id,
                    request.idempotency_key,
                    TaskState.STAGE0_PENDING.value,
                    Jsonb(request.budget),
                ),
            ).fetchone()
        assert row is not None
        if row["name"] != request.name or row["workload_id"] != request.workload_id:
            raise Conflict("idempotency_key was already used with a different task")
        return row

    @staticmethod
    def _target_fingerprint(target: TargetSpec) -> str:
        return target_fingerprint(target)

    @staticmethod
    def _digest_json(value: Mapping[str, Any]) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _m2_payload_hash(value: Any) -> str:
        return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    @staticmethod
    def _search_round_authority(row: Mapping[str, Any]) -> SearchRound:
        return SearchRound.model_validate({name: row[name] for name in SearchRound.model_fields})

    @staticmethod
    def _formal_authority_context(
        row: Mapping[str, Any],
    ) -> FormalAuthorityContextDescriptor:
        return FormalAuthorityContextDescriptor(
            authority_context_id=row["authority_context_id"],
            context_hash=row["context_hash"],
            round_id=row["round_id"],
            task_id=row["task_id"],
            target_snapshot_id=row["target_snapshot_id"],
            stage0_run_id=row["stage0_run_id"],
            stage0_protocol_hash=row["stage0_protocol_hash"],
            baseline_epoch_id=row["baseline_epoch_id"],
            hotspot_id=row["hotspot_id"],
            target_profile_hash=row["target_profile_hash"],
            workload_profile_hash=row["workload_profile_hash"],
            measurement_profile_hash=row["measurement_profile_hash"],
            candidate_family_hash=row["candidate_family_hash"],
            artifact_family_hash=row["artifact_family_hash"],
            search_plan_hash=row["search_plan_hash"],
            holdout_plan_commitment=row["holdout_plan_commitment"],
            holdout_plan_authority_id=row["holdout_plan_authority_id"],
            holdout_plan_authority_hash=row["holdout_plan_authority_hash"],
            selection_rule_hash=row["selection_rule_hash"],
            evidence_store=FormalEvidenceStoreRef(
                store_id=row["evidence_store_id"],
                store_version=row["evidence_store_version"],
                store_hash=row["evidence_store_hash"],
                access_policy_hash=row["evidence_access_policy_hash"],
            ),
            verifier=FormalVerifierRef(
                verifier_id=row["verifier_id"],
                verifier_version=row["verifier_version"],
                verifier_hash=row["verifier_hash"],
            ),
            sealed_by=row["sealed_by"],
            sealed_at=row["sealed_at"],
            run_mode=row["run_mode"],
            project_mode=row["project_mode"],
            synthetic=row["synthetic"],
            automatic_release_allowed=row["automatic_release_allowed"],
        )

    def _load_formal_authority_context(
        self,
        connection: Connection[dict[str, Any]],
        reference: FormalAuthorityContextRef,
    ) -> tuple[dict[str, Any], FormalAuthorityContextDescriptor]:
        row = connection.execute(
            """
            SELECT * FROM formal_round_authority_contexts
            WHERE round_id = %s AND authority_context_id = %s
            FOR SHARE
            """,
            (reference.round_id, reference.authority_context_id),
        ).fetchone()
        if row is None:
            raise Conflict("M2a Formal Authority Context is not durable")
        context = self._formal_authority_context(row)
        if canonical_json_bytes(formal_authority_context_ref(context)) != canonical_json_bytes(
            reference
        ):
            raise Conflict("M2a Formal authority reference belongs to another Context")
        return row, context

    @staticmethod
    def _formal_signoff_intent(
        row: Mapping[str, Any],
        context: FormalAuthorityContextDescriptor,
    ) -> FormalRoundSignoffIntent:
        values = {
            name: row[name]
            for name in FormalRoundSignoffIntent.model_fields
            if name not in {"schema_version", "authority_context"}
        }
        return FormalRoundSignoffIntent(
            authority_context=formal_authority_context_ref(context),
            **values,
        )

    def list_scripted_operator_hotspots(
        self,
        target: TargetOperatorProfileRefs,
        workload: WorkloadOperatorProfileRefs,
    ) -> tuple[OperatorHotspotAuthorityView, ...]:
        """List Hotspots from the latest fully matching frozen Scripted Authority."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                WITH matched_authority AS (
                    SELECT
                        snapshot.target_snapshot_id,
                        run.stage0_run_id,
                        baseline.stage0_protocol_hash,
                        baseline.baseline_epoch_id,
                        source.source_hash AS baseline_source_hash,
                        baseline.workload_id,
                        baseline.workload_hash,
                        baseline.configuration_hash,
                        baseline.image_digest,
                        baseline.adapter_profile
                    FROM target_snapshots AS snapshot
                    JOIN stage0_runs AS run
                      ON run.target_snapshot_id = snapshot.target_snapshot_id
                    JOIN tasks AS stage0_task ON stage0_task.task_id = run.task_id
                    JOIN baseline_epochs AS baseline
                      ON baseline.target_snapshot_id = snapshot.target_snapshot_id
                     AND baseline.stage0_run_id = run.stage0_run_id
                    JOIN source_snapshots AS source
                      ON source.snapshot_id = baseline.source_snapshot_id
                    WHERE snapshot.target_id = %s
                      AND snapshot.target_fingerprint = %s
                      AND run.adapter_profile = %s
                      AND run.mode = 'dry_run'
                      AND run.state = 'finalized'
                      AND stage0_task.stage0_authority = 'synthetic'
                      AND baseline.frozen = TRUE
                      AND baseline.baseline_kind = 'search_round'
                      AND baseline.stage0_protocol_hash = %s
                      AND baseline.workload_id = %s
                      AND baseline.workload_hash = %s
                      AND baseline.configuration_hash = %s
                      AND baseline.adapter_profile = %s
                      AND source.synthetic = TRUE
                      AND EXISTS (
                          SELECT 1 FROM hotspots AS available
                          WHERE available.baseline_epoch_id = baseline.baseline_epoch_id
                            AND available.candidate_kind = 'fixture'
                      )
                    ORDER BY baseline.created_at DESC, baseline.baseline_epoch_id DESC
                    LIMIT 1
                )
                SELECT
                    authority.*,
                    hotspot.hotspot_id,
                    hotspot.symbol,
                    hotspot.share_ratio,
                    hotspot.opportunity_score,
                    hotspot.patchability,
                    hotspot.intake_hash AS hotspot_intake_hash,
                    hotspot.evidence AS hotspot_evidence
                FROM matched_authority AS authority
                JOIN hotspots AS hotspot
                  ON hotspot.baseline_epoch_id = authority.baseline_epoch_id
                WHERE hotspot.candidate_kind = 'fixture'
                ORDER BY hotspot.created_at, hotspot.hotspot_id
                """,
                (
                    target.target_id,
                    target.target_spec_hash,
                    target.adapter_profile,
                    target.required_stage0_protocol_hash,
                    workload.workload_id,
                    workload.workload_hash,
                    workload.configuration_hash,
                    target.adapter_profile,
                ),
            ).fetchall()
        discovered: list[OperatorHotspotAuthorityView] = []
        for row in rows:
            evidence = dict(row["hotspot_evidence"])
            hotspot_payload: dict[str, Any] = {
                "source": (
                    "manual"
                    if evidence.get("manual_intake_uri") is not None
                    or evidence.get("manual_intake_hash") is not None
                    else "profiler"
                ),
                "hotspot_id": row["hotspot_id"],
                "hotspot_intake_hash": row["hotspot_intake_hash"],
                "profiler_evidence_uri": evidence.get("profiler_raw_output_uri"),
                "profiler_evidence_hash": evidence.get("profiler_raw_output_hash"),
                "correctness_evidence_uri": evidence.get("correctness_spec_uri"),
                "correctness_evidence_hash": evidence.get("correctness_spec_hash"),
                "replacement_point": evidence.get("replacement_point"),
                "workload_hash": row["workload_hash"],
                "shape": tuple(evidence.get("shape", ())),
                "dtype": evidence.get("dtype"),
            }
            if hotspot_payload["source"] == "manual":
                hotspot_payload.update(
                    manual_intake_uri=evidence.get("manual_intake_uri"),
                    manual_intake_hash=evidence.get("manual_intake_hash"),
                )
            try:
                hotspot = (
                    ManualOperatorHotspotRef.model_validate(hotspot_payload)
                    if hotspot_payload["source"] == "manual"
                    else ProfilerOperatorHotspotRef.model_validate(hotspot_payload)
                )
            except ValidationError as error:
                raise Conflict(
                    "stored Scripted Hotspot Authority is incomplete or invalid"
                ) from error
            authority = ResolvedOperatorAuthority(
                target_snapshot_id=row["target_snapshot_id"],
                stage0_run_id=row["stage0_run_id"],
                stage0_protocol_hash=row["stage0_protocol_hash"],
                baseline_epoch_id=row["baseline_epoch_id"],
                baseline_source_hash=row["baseline_source_hash"],
                hotspot_id=row["hotspot_id"],
                replacement_point=evidence["replacement_point"],
                workload_id=row["workload_id"],
                workload_hash=row["workload_hash"],
                configuration_hash=row["configuration_hash"],
                image_digest=row["image_digest"],
                adapter_profile=row["adapter_profile"],
                profiler_evidence_uri=evidence["profiler_raw_output_uri"],
                profiler_evidence_hash=evidence["profiler_raw_output_hash"],
                synthetic=True,
            )
            discovered.append(
                OperatorHotspotAuthorityView(
                    hotspot=hotspot,
                    authority=authority,
                    symbol=row["symbol"],
                    share_ratio=row["share_ratio"],
                    opportunity_score=row["opportunity_score"],
                    patchability=row["patchability"],
                )
            )
        return tuple(discovered)

    def resolve_scripted_operator_authority(
        self,
        target: TargetOperatorProfileRefs,
        workload: WorkloadOperatorProfileRefs,
        hotspot: OperatorHotspotRef,
    ) -> ResolvedOperatorAuthority:
        """Resolve the latest fully matching frozen Scripted Authority."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    snapshot.target_snapshot_id,
                    run.stage0_run_id,
                    baseline.stage0_protocol_hash,
                    baseline.baseline_epoch_id,
                    source.source_hash AS baseline_source_hash,
                    hotspot.hotspot_id,
                    hotspot.intake_hash AS hotspot_intake_hash,
                    hotspot.evidence AS hotspot_evidence,
                    baseline.workload_id,
                    baseline.workload_hash,
                    baseline.configuration_hash,
                    baseline.image_digest,
                    baseline.adapter_profile
                FROM target_snapshots AS snapshot
                JOIN stage0_runs AS run
                  ON run.target_snapshot_id = snapshot.target_snapshot_id
                JOIN tasks AS stage0_task ON stage0_task.task_id = run.task_id
                JOIN baseline_epochs AS baseline
                  ON baseline.target_snapshot_id = snapshot.target_snapshot_id
                 AND baseline.stage0_run_id = run.stage0_run_id
                JOIN source_snapshots AS source
                  ON source.snapshot_id = baseline.source_snapshot_id
                JOIN hotspots AS hotspot
                  ON hotspot.baseline_epoch_id = baseline.baseline_epoch_id
                WHERE snapshot.target_id = %s
                  AND snapshot.target_fingerprint = %s
                  AND run.adapter_profile = %s
                  AND run.mode = 'dry_run'
                  AND run.state = 'finalized'
                  AND stage0_task.stage0_authority = 'synthetic'
                  AND baseline.frozen = TRUE
                  AND baseline.baseline_kind = 'search_round'
                  AND baseline.stage0_protocol_hash = %s
                  AND baseline.workload_id = %s
                  AND baseline.workload_hash = %s
                  AND baseline.configuration_hash = %s
                  AND baseline.adapter_profile = %s
                  AND source.synthetic = TRUE
                  AND hotspot.hotspot_id = %s
                  AND hotspot.candidate_kind = 'fixture'
                ORDER BY baseline.created_at DESC, baseline.baseline_epoch_id DESC
                LIMIT 1
                FOR SHARE OF snapshot, run, stage0_task, baseline, source, hotspot
                """,
                (
                    target.target_id,
                    target.target_spec_hash,
                    target.adapter_profile,
                    target.required_stage0_protocol_hash,
                    workload.workload_id,
                    workload.workload_hash,
                    workload.configuration_hash,
                    target.adapter_profile,
                    hotspot.hotspot_id,
                ),
            ).fetchone()
        if row is None:
            raise NotFound("matching Scripted Operator Authority not found")
        evidence = dict(row["hotspot_evidence"])
        expected_hotspot = (
            row["hotspot_id"],
            row["hotspot_intake_hash"],
            evidence.get("profiler_raw_output_uri"),
            evidence.get("profiler_raw_output_hash"),
            evidence.get("correctness_spec_uri"),
            evidence.get("correctness_spec_hash"),
            evidence.get("replacement_point"),
            row["workload_hash"],
            tuple(evidence.get("shape", ())),
            evidence.get("dtype"),
        )
        requested_hotspot = (
            hotspot.hotspot_id,
            hotspot.hotspot_intake_hash,
            hotspot.profiler_evidence_uri,
            hotspot.profiler_evidence_hash,
            hotspot.correctness_evidence_uri,
            hotspot.correctness_evidence_hash,
            hotspot.replacement_point,
            hotspot.workload_hash,
            hotspot.shape,
            hotspot.dtype,
        )
        if expected_hotspot != requested_hotspot:
            raise Conflict("Scripted Hotspot Authority bindings do not match")
        if isinstance(hotspot, ManualOperatorHotspotRef) and (
            evidence.get("manual_intake_uri"),
            evidence.get("manual_intake_hash"),
        ) != (hotspot.manual_intake_uri, hotspot.manual_intake_hash):
            raise Conflict("Manual Hotspot Intake Authority bindings do not match")
        return ResolvedOperatorAuthority(
            target_snapshot_id=row["target_snapshot_id"],
            stage0_run_id=row["stage0_run_id"],
            stage0_protocol_hash=row["stage0_protocol_hash"],
            baseline_epoch_id=row["baseline_epoch_id"],
            baseline_source_hash=row["baseline_source_hash"],
            hotspot_id=row["hotspot_id"],
            replacement_point=evidence["replacement_point"],
            workload_id=row["workload_id"],
            workload_hash=row["workload_hash"],
            configuration_hash=row["configuration_hash"],
            image_digest=row["image_digest"],
            adapter_profile=row["adapter_profile"],
            profiler_evidence_uri=evidence["profiler_raw_output_uri"],
            profiler_evidence_hash=evidence["profiler_raw_output_hash"],
            synthetic=True,
        )

    def resolve_formal_operator_authority(
        self,
        target: TargetOperatorProfileRefs,
        workload: WorkloadOperatorProfileRefs,
        candidate_family: BusinessCandidateFamilyManifest,
    ) -> FormalOperatorAuthoritySnapshot:
        """Reread one exact non-synthetic Authority selected by a frozen Family."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    snapshot.target_snapshot_id,
                    snapshot.target_id,
                    snapshot.target_fingerprint,
                    run.stage0_run_id,
                    run.mode AS stage0_mode,
                    run.state AS stage0_state,
                    run.protocol_version AS stage0_protocol_version,
                    stage0_task.stage0_authority,
                    stage0_task.project_mode AS stage0_project_mode,
                    stage0_task.automatic_release_allowed AS stage0_auto_release,
                    stage0_evidence.evidence AS stage0_evidence,
                    stage0_evidence.report AS stage0_report,
                    baseline.baseline_epoch_id,
                    baseline.frozen AS baseline_frozen,
                    baseline.baseline_kind,
                    baseline.target_snapshot_id AS baseline_target_snapshot_id,
                    baseline.stage0_run_id AS baseline_stage0_run_id,
                    baseline.stage0_protocol_hash,
                    baseline.workload_id,
                    baseline.workload_hash,
                    baseline.configuration_hash,
                    baseline.image_digest,
                    source.source_hash AS baseline_source_hash,
                    source.clean AS baseline_source_clean,
                    source.synthetic AS baseline_source_synthetic,
                    source.adapter_provenance AS baseline_source_provenance,
                    hotspot.hotspot_id,
                    hotspot.baseline_epoch_id AS hotspot_baseline_epoch_id,
                    hotspot.intake_hash AS hotspot_intake_hash,
                    hotspot.candidate_kind AS hotspot_candidate_kind,
                    hotspot.evidence AS hotspot_evidence
                FROM target_snapshots AS snapshot
                JOIN stage0_runs AS run
                  ON run.target_snapshot_id = snapshot.target_snapshot_id
                JOIN tasks AS stage0_task ON stage0_task.task_id = run.task_id
                JOIN stage0_evidence
                  ON stage0_evidence.stage0_run_id = run.stage0_run_id
                JOIN baseline_epochs AS baseline
                  ON baseline.target_snapshot_id = snapshot.target_snapshot_id
                 AND baseline.stage0_run_id = run.stage0_run_id
                JOIN source_snapshots AS source
                  ON source.snapshot_id = baseline.source_snapshot_id
                JOIN hotspots AS hotspot
                  ON hotspot.baseline_epoch_id = baseline.baseline_epoch_id
                WHERE snapshot.target_snapshot_id = %s
                  AND run.stage0_run_id = %s
                  AND baseline.baseline_epoch_id = %s
                  AND hotspot.hotspot_id = %s
                FOR SHARE OF snapshot, run, stage0_task, stage0_evidence,
                    baseline, source, hotspot
                """,
                (
                    candidate_family.target_snapshot_id,
                    candidate_family.stage0_run_id,
                    candidate_family.baseline_epoch_id,
                    candidate_family.hotspot_id,
                ),
            ).fetchone()
        if row is None:
            raise NotFound("matching Formal Operator Authority not found")

        stage0_evidence = dict(row["stage0_evidence"])
        stage0_report = dict(row["stage0_report"])
        source_provenance = row["baseline_source_provenance"]
        expected_authority = {
            "target_id": target.target_id,
            "target_fingerprint": target.target_spec_hash,
            "stage0_mode": Stage0RunMode.FORMAL.value,
            "stage0_state": Stage0RunState.FINALIZED.value,
            "stage0_authority": "formal",
            "stage0_project_mode": ProjectMode.DEGRADED_MANUAL_INTAKE.value,
            "stage0_auto_release": False,
            "baseline_frozen": True,
            "baseline_kind": WorkflowType.MANUAL_CANDIDATE.value,
            "baseline_target_snapshot_id": candidate_family.target_snapshot_id,
            "baseline_stage0_run_id": candidate_family.stage0_run_id,
            "stage0_protocol_hash": target.required_stage0_protocol_hash,
            "workload_id": workload.workload_id,
            "workload_hash": workload.workload_hash,
            "configuration_hash": workload.configuration_hash,
            "baseline_source_hash": candidate_family.baseline_source_hash,
            "baseline_source_clean": True,
            "baseline_source_synthetic": False,
            "hotspot_baseline_epoch_id": candidate_family.baseline_epoch_id,
            "hotspot_candidate_kind": ManualCandidateKind.BUSINESS.value,
        }
        if any(row[name] != value for name, value in expected_authority.items()):
            raise Conflict("Formal Operator Authority bindings do not match")
        if (
            stage0_evidence.get("synthetic") is not False
            or stage0_evidence.get("stage0_run_id")
            != str(candidate_family.stage0_run_id)
            or stage0_evidence.get("protocol_version")
            != row["stage0_protocol_version"]
            or stage0_evidence.get("protocol_hash")
            != target.required_stage0_protocol_hash
            or stage0_report.get("evidence_authority") != "formal"
            or stage0_report.get("automatic_release_allowed") is not False
            or not isinstance(source_provenance, list)
            or not source_provenance
            or any(
                not isinstance(item, dict)
                or item.get("implementation_kind") == "fake"
                for item in source_provenance
            )
        ):
            raise Conflict("Formal Operator Authority evidence is incomplete or synthetic")

        evidence = dict(row["hotspot_evidence"])
        hotspot_payload: dict[str, Any] = {
            "source": (
                "manual"
                if evidence.get("manual_intake_uri") is not None
                or evidence.get("manual_intake_hash") is not None
                else "profiler"
            ),
            "hotspot_id": row["hotspot_id"],
            "hotspot_intake_hash": row["hotspot_intake_hash"],
            "profiler_evidence_uri": evidence.get("profiler_raw_output_uri"),
            "profiler_evidence_hash": evidence.get("profiler_raw_output_hash"),
            "correctness_evidence_uri": evidence.get("correctness_spec_uri"),
            "correctness_evidence_hash": evidence.get("correctness_spec_hash"),
            "replacement_point": evidence.get("replacement_point"),
            "workload_hash": row["workload_hash"],
            "shape": tuple(evidence.get("shape", ())),
            "dtype": evidence.get("dtype"),
        }
        if hotspot_payload["source"] == "manual":
            hotspot_payload.update(
                manual_intake_uri=evidence.get("manual_intake_uri"),
                manual_intake_hash=evidence.get("manual_intake_hash"),
            )
        try:
            hotspot = (
                ManualOperatorHotspotRef.model_validate(hotspot_payload)
                if hotspot_payload["source"] == "manual"
                else ProfilerOperatorHotspotRef.model_validate(hotspot_payload)
            )
        except ValidationError as error:
            raise Conflict("stored Formal Hotspot Authority is incomplete or invalid") from error
        if (
            evidence.get("replacement_point") != candidate_family.replacement_point
            or evidence.get("profiler_raw_output_uri")
            != candidate_family.profiler_evidence_uri
            or evidence.get("profiler_raw_output_hash")
            != candidate_family.profiler_evidence_hash
        ):
            raise Conflict("Formal Hotspot Authority drifted from the Candidate Family")

        authority = ResolvedOperatorAuthority(
            target_snapshot_id=row["target_snapshot_id"],
            stage0_run_id=row["stage0_run_id"],
            stage0_protocol_hash=row["stage0_protocol_hash"],
            baseline_epoch_id=row["baseline_epoch_id"],
            baseline_source_hash=row["baseline_source_hash"],
            hotspot_id=row["hotspot_id"],
            replacement_point=evidence["replacement_point"],
            workload_id=row["workload_id"],
            workload_hash=row["workload_hash"],
            configuration_hash=row["configuration_hash"],
            image_digest=row["image_digest"],
            adapter_profile=target.adapter_profile,
            profiler_evidence_uri=evidence["profiler_raw_output_uri"],
            profiler_evidence_hash=evidence["profiler_raw_output_hash"],
            synthetic=False,
        )
        return FormalOperatorAuthoritySnapshot(authority=authority, hotspot=hotspot)

    def assert_operator_candidate_ids_available(
        self,
        candidate_ids: tuple[UUID, ...],
        *,
        allowed_round_id: UUID | None = None,
    ) -> None:
        if not candidate_ids:
            return
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT candidate_id, round_id FROM candidates
                WHERE candidate_id = ANY(%s)
                FOR SHARE
                """,
                (list(candidate_ids),),
            ).fetchall()
        if any(row["round_id"] != allowed_round_id for row in rows):
            raise Conflict("Operator Candidate identity is already bound")

    def get_operator_plan_preview_by_idempotency(
        self, idempotency_key: str
    ) -> RoundPlanPreviewView | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT payload FROM operator_plan_previews
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return RoundPlanPreviewView.model_validate(row["payload"])

    def get_operator_plan_preview(self, preview_id: UUID) -> RoundPlanPreviewView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload FROM operator_plan_previews WHERE preview_id = %s",
                (preview_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"Operator Plan Preview not found: {preview_id}")
        return RoundPlanPreviewView.model_validate(row["payload"])

    def create_operator_plan_preview(
        self,
        idempotency_key: str,
        preview: RoundPlanPreviewView,
    ) -> RoundPlanPreviewView:
        if not preview.synthetic or preview.automatic_release_allowed:
            raise Conflict("OX-1 persists only synthetic non-releasing Previews")
        if preview.preview_id != uuid5(
            NAMESPACE_URL, f"hcuopt:operator-preview:{idempotency_key}"
        ) or preview.resolved_plan_hash != self._m2_payload_hash(preview.resolved_plan):
            raise OperatorPlanHashMismatch(
                "Operator Preview identity or resolved Plan Hash does not match"
            )
        payload = preview.model_dump(mode="json")
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO operator_plan_previews (
                    preview_id, idempotency_key, preview_request_digest,
                    resolved_plan_hash, payload, expires_at, synthetic,
                    automatic_release_allowed, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, FALSE, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    preview.preview_id,
                    idempotency_key,
                    preview.preview_request_digest,
                    preview.resolved_plan_hash,
                    Jsonb(payload),
                    preview.expires_at,
                    preview.created_at,
                ),
            ).fetchone()
            if row is None:
                rows = connection.execute(
                    """
                    SELECT * FROM operator_plan_previews
                    WHERE preview_id = %s OR idempotency_key = %s
                    FOR SHARE
                    """,
                    (preview.preview_id, idempotency_key),
                ).fetchall()
                row = rows[0] if len(rows) == 1 else None
            if row is None or (
                row["preview_id"] != preview.preview_id
                or row["idempotency_key"] != idempotency_key
                or row["preview_request_digest"] != preview.preview_request_digest
                or row["resolved_plan_hash"] != preview.resolved_plan_hash
            ):
                raise OperatorPlanHashMismatch(
                    "Operator Preview idempotency key was reused with different inputs"
                )
        return RoundPlanPreviewView.model_validate(row["payload"])

    @staticmethod
    def _operator_start_intent(row: Mapping[str, Any]) -> OperatorStartIntentView:
        return OperatorStartIntentView.model_validate(
            {
                name: row[name]
                for name in OperatorStartIntentView.model_fields
                if name not in {"schema_version", "candidate_members"}
            }
            | {
                "schema_version": "m2-operator-start-v1",
                "candidate_members": row["candidate_members"],
            }
        )

    def create_operator_start_intent(
        self,
        intent: OperatorStartIntentView,
    ) -> tuple[OperatorStartIntentView, bool]:
        if intent.state != "preparing" or not intent.synthetic:
            raise Conflict("new Operator StartIntent must be synthetic and preparing")
        members = [item.model_dump(mode="json") for item in intent.candidate_members]
        service_identity = intent.service_identity.model_dump(mode="json")
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO operator_start_intents (
                    intent_id, preview_id, resolved_plan_hash, request_digest,
                    task_id, round_id, actor, idempotency_key, state,
                    candidate_members, service_identity, synthetic,
                    automatic_release_allowed, version, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 'preparing',
                    %s, %s, TRUE, FALSE, 1, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    intent.intent_id,
                    intent.preview_id,
                    intent.resolved_plan_hash,
                    intent.request_digest,
                    intent.task_id,
                    intent.round_id,
                    intent.actor,
                    intent.idempotency_key,
                    Jsonb(members),
                    Jsonb(service_identity),
                    intent.created_at,
                    intent.updated_at,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                rows = connection.execute(
                    """
                    SELECT * FROM operator_start_intents
                    WHERE intent_id = %s OR preview_id = %s OR idempotency_key = %s
                    FOR UPDATE
                    """,
                    (intent.intent_id, intent.preview_id, intent.idempotency_key),
                ).fetchall()
                row = rows[0] if len(rows) == 1 else None
            expected = {
                "intent_id": intent.intent_id,
                "preview_id": intent.preview_id,
                "resolved_plan_hash": intent.resolved_plan_hash,
                "request_digest": intent.request_digest,
                "task_id": intent.task_id,
                "round_id": intent.round_id,
                "actor": intent.actor,
                "idempotency_key": intent.idempotency_key,
                "candidate_members": members,
                "service_identity": service_identity,
                "synthetic": True,
                "automatic_release_allowed": False,
            }
            if row is None or any(row[name] != value for name, value in expected.items()):
                raise OperatorPlanHashMismatch(
                    "Operator Start idempotency or Preview identity was reused"
                )
        return self._operator_start_intent(row), created

    def get_operator_start_intent(self, intent_id: UUID) -> OperatorStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE intent_id = %s",
                (intent_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"Operator StartIntent not found: {intent_id}")
        return self._operator_start_intent(row)

    def get_operator_start_intent_by_idempotency(
        self,
        idempotency_key: str,
    ) -> OperatorStartIntentView | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return self._operator_start_intent(row)

    def get_operator_start_intent_by_round_id(
        self,
        round_id: UUID,
    ) -> OperatorStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE round_id = %s",
                (round_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"Operator StartIntent not found for Round: {round_id}")
        return self._operator_start_intent(row)

    def list_operator_round_ids(self, limit: int) -> tuple[UUID, ...]:
        """List recent executable Scripted Rounds without inventing UI state."""

        if limit < 1 or limit > 100:
            raise ValueError("Operator Round list limit must be between 1 and 100")
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT round_id
                FROM operator_start_intents
                WHERE state = 'finalized'
                  AND synthetic = TRUE
                  AND automatic_release_allowed = FALSE
                ORDER BY finalized_at DESC, round_id DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return tuple(row["round_id"] for row in rows)

    def record_operator_start_plans(
        self,
        intent_id: UUID,
        plans: FrozenScriptedPlans,
    ) -> OperatorStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Operator StartIntent not found: {intent_id}")
            expected = {
                "search_plan_hash": plans.search_plan_hash,
                "holdout_plan_commitment": plans.holdout_plan_commitment,
                "holdout_commitment_scheme": plans.holdout_commitment_scheme,
                "holdout_plan_authority_id": plans.holdout_plan_authority_id,
                "holdout_plan_authority_hash": plans.holdout_plan_authority_hash,
                "family_alpha": plans.family_alpha,
            }
            if row["state"] != "preparing":
                if any(row[name] != value for name, value in expected.items()):
                    raise Conflict("Operator Start Plan Authority changed during replay")
                return self._operator_start_intent(row)
            row = connection.execute(
                """
                UPDATE operator_start_intents
                SET state = 'plans_frozen', search_plan_hash = %s,
                    holdout_plan_commitment = %s, holdout_commitment_scheme = %s,
                    holdout_plan_authority_id = %s,
                    holdout_plan_authority_hash = %s, family_alpha = %s,
                    version = version + 1, updated_at = now()
                WHERE intent_id = %s
                RETURNING *
                """,
                (
                    plans.search_plan_hash,
                    plans.holdout_plan_commitment,
                    plans.holdout_commitment_scheme,
                    plans.holdout_plan_authority_id,
                    plans.holdout_plan_authority_hash,
                    plans.family_alpha,
                    intent_id,
                ),
            ).fetchone()
        assert row is not None
        return self._operator_start_intent(row)

    def record_operator_start_round_created(
        self, intent_id: UUID
    ) -> OperatorStartIntentView:
        return self._advance_operator_start_state(
            intent_id, expected="plans_frozen", target="round_created"
        )

    def record_operator_start_member_bound(
        self,
        intent_id: UUID,
        ordinal: int,
    ) -> OperatorStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Operator StartIntent not found: {intent_id}")
            members = tuple(
                OperatorStartCandidateMember.model_validate(item)
                for item in row["candidate_members"]
            )
            if not 0 <= ordinal < len(members) or members[ordinal].ordinal != ordinal:
                raise Conflict("Operator Start member ordinal is invalid")
            if members[ordinal].state == "round_member_bound":
                return self._operator_start_intent(row)
            if row["state"] != "round_created":
                raise Conflict("Operator StartIntent is not binding Candidate members")
            if members[ordinal].state != "pending":
                raise Conflict("Operator Start member is not pending")
            updated_members = list(members)
            updated_members[ordinal] = OperatorStartCandidateMember.model_validate(
                {**members[ordinal].model_dump(mode="json"), "state": "round_member_bound"}
            )
            row = connection.execute(
                """
                UPDATE operator_start_intents
                SET candidate_members = %s, version = version + 1, updated_at = now()
                WHERE intent_id = %s
                RETURNING *
                """,
                (
                    Jsonb([item.model_dump(mode="json") for item in updated_members]),
                    intent_id,
                ),
            ).fetchone()
        assert row is not None
        return self._operator_start_intent(row)

    def record_operator_start_intake_closed(
        self,
        intent_id: UUID,
        candidate_family_hash: str,
    ) -> OperatorStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Operator StartIntent not found: {intent_id}")
            if row["state"] in {"intake_closed", "finalized"}:
                if row["candidate_family_hash"] != candidate_family_hash:
                    raise Conflict("Operator Start Candidate Family changed during replay")
                return self._operator_start_intent(row)
            if row["state"] != "round_created" or any(
                item["state"] != "round_member_bound"
                for item in row["candidate_members"]
            ):
                raise Conflict("Operator Start Intake cannot close before all members bind")
            row = connection.execute(
                """
                UPDATE operator_start_intents
                SET state = 'intake_closed', candidate_family_hash = %s,
                    version = version + 1, updated_at = now()
                WHERE intent_id = %s
                RETURNING *
                """,
                (candidate_family_hash, intent_id),
            ).fetchone()
        assert row is not None
        return self._operator_start_intent(row)

    def finalize_operator_start_intent(
        self,
        intent_id: UUID,
    ) -> OperatorStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Operator StartIntent not found: {intent_id}")
            if row["state"] == "finalized":
                return self._operator_start_intent(row)
            if row["state"] != "intake_closed":
                raise Conflict("Operator StartIntent cannot finalize before Intake Close")
            row = connection.execute(
                """
                UPDATE operator_start_intents
                SET state = 'finalized', finalized_at = now(),
                    version = version + 1, updated_at = now()
                WHERE intent_id = %s
                RETURNING *
                """,
                (intent_id,),
            ).fetchone()
        assert row is not None
        return self._operator_start_intent(row)

    def fail_operator_start_intent(
        self,
        intent_id: UUID,
        *,
        error_code: str,
        error_message: str,
        member_ordinal: int | None = None,
    ) -> OperatorStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Operator StartIntent not found: {intent_id}")
            if row["state"] == "failed":
                if (row["error_code"], row["error_message"]) != (
                    error_code,
                    error_message,
                ):
                    raise Conflict("Operator StartIntent failed with another error")
                return self._operator_start_intent(row)
            if row["state"] in {"intake_closed", "finalized"}:
                raise Conflict("closed Operator StartIntent cannot be marked failed")
            members = tuple(
                OperatorStartCandidateMember.model_validate(item)
                for item in row["candidate_members"]
            )
            if member_ordinal is not None:
                if not 0 <= member_ordinal < len(members):
                    raise Conflict("Operator Start failure member ordinal is invalid")
                updated_members = list(members)
                updated_members[member_ordinal] = OperatorStartCandidateMember.model_validate(
                    {
                        **members[member_ordinal].model_dump(mode="json"),
                        "state": "failed",
                        "error_code": error_code,
                    }
                )
                members = tuple(updated_members)
            row = connection.execute(
                """
                UPDATE operator_start_intents
                SET state = 'failed', error_code = %s, error_message = %s,
                    candidate_members = %s, version = version + 1, updated_at = now()
                WHERE intent_id = %s
                RETURNING *
                """,
                (
                    error_code,
                    error_message,
                    Jsonb([item.model_dump(mode="json") for item in members]),
                    intent_id,
                ),
            ).fetchone()
        assert row is not None
        return self._operator_start_intent(row)

    def _advance_operator_start_state(
        self,
        intent_id: UUID,
        *,
        expected: str,
        target: str,
    ) -> OperatorStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM operator_start_intents WHERE intent_id = %s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Operator StartIntent not found: {intent_id}")
            progress = {
                "preparing": 0,
                "plans_frozen": 1,
                "round_created": 2,
                "intake_closed": 3,
                "finalized": 4,
            }
            if row["state"] != "failed" and progress.get(row["state"], -1) >= progress[target]:
                return self._operator_start_intent(row)
            if row["state"] != expected:
                raise Conflict(f"Operator StartIntent cannot advance from {row['state']}")
            row = connection.execute(
                """
                UPDATE operator_start_intents
                SET state = %s, version = version + 1, updated_at = now()
                WHERE intent_id = %s
                RETURNING *
                """,
                (target, intent_id),
            ).fetchone()
        assert row is not None
        return self._operator_start_intent(row)

    @staticmethod
    def _formal_start_intent(row: Mapping[str, Any]) -> FormalStartIntentView:
        return FormalStartIntentView.model_validate(
            {
                name: row[name]
                for name in FormalStartIntentView.model_fields
                if name != "schema_version"
            }
            | {"schema_version": "m2a-formal-start-intent-v1"}
        )

    def create_formal_start_intent(
        self,
        intent: FormalStartIntentView,
    ) -> tuple[FormalStartIntentView, bool]:
        if (
            intent.state != "awaiting_authority"
            or intent.synthetic
            or intent.round_creation_allowed
            or intent.hcu_accessed
            or intent.automatic_release_allowed
        ):
            raise Conflict("new Formal StartIntent must be non-executing and awaiting Authority")
        bindings = [item.model_dump(mode="json") for item in intent.candidate_bindings]
        identity = intent.service_identity.model_dump(mode="json")
        blockers = list(intent.blocker_codes)
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO formal_operator_start_intents (
                    intent_id, preview_id, resolved_plan_hash,
                    formal_authorization_hash, execution_authority_hash,
                    evaluation_authority_hash, request_digest, actor_id,
                    actor_assertion_hash, actor_signer_id, actor_signer_hash,
                    idempotency_key, task_id, round_id,
                    candidate_bindings, state, blocker_codes, service_identity,
                    authority_reconcile_count, version, created_at, updated_at,
                    authority_ready, round_creation_allowed, hcu_accessed,
                    synthetic, automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, 'awaiting_authority', %s, %s, 0, 1, %s, %s,
                    FALSE, FALSE, FALSE, FALSE, FALSE
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    intent.intent_id,
                    intent.preview_id,
                    intent.resolved_plan_hash,
                    intent.formal_authorization_hash,
                    intent.execution_authority_hash,
                    intent.evaluation_authority_hash,
                    intent.request_digest,
                    intent.actor_id,
                    intent.actor_assertion_hash,
                    intent.actor_signer_id,
                    intent.actor_signer_hash,
                    intent.idempotency_key,
                    intent.task_id,
                    intent.round_id,
                    Jsonb(bindings),
                    Jsonb(blockers),
                    Jsonb(identity),
                    intent.created_at,
                    intent.updated_at,
                ),
            ).fetchone()
            created = row is not None
            if created:
                connection.execute(
                    """
                    INSERT INTO formal_operator_start_intent_events (
                        intent_id, sequence, event_type, state, blocker_codes,
                        error_code, occurred_at
                    ) VALUES (%s, 1, 'created', 'awaiting_authority', %s, NULL, %s)
                    """,
                    (intent.intent_id, Jsonb(blockers), intent.created_at),
                )
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM formal_operator_start_intents
                    WHERE intent_id = %s OR preview_id = %s OR idempotency_key = %s
                    FOR UPDATE
                    """,
                    (intent.intent_id, intent.preview_id, intent.idempotency_key),
                ).fetchall()
                row = rows[0] if len(rows) == 1 else None
            expected = {
                "intent_id": intent.intent_id,
                "preview_id": intent.preview_id,
                "resolved_plan_hash": intent.resolved_plan_hash,
                "formal_authorization_hash": intent.formal_authorization_hash,
                "execution_authority_hash": intent.execution_authority_hash,
                "evaluation_authority_hash": intent.evaluation_authority_hash,
                "request_digest": intent.request_digest,
                "actor_id": intent.actor_id,
                "actor_assertion_hash": intent.actor_assertion_hash,
                "actor_signer_id": intent.actor_signer_id,
                "actor_signer_hash": intent.actor_signer_hash,
                "idempotency_key": intent.idempotency_key,
                "task_id": intent.task_id,
                "round_id": intent.round_id,
                "candidate_bindings": bindings,
                "service_identity": identity,
                "synthetic": False,
                "round_creation_allowed": False,
                "hcu_accessed": False,
                "automatic_release_allowed": False,
            }
            if row is None or any(row[name] != value for name, value in expected.items()):
                raise OperatorPlanHashMismatch(
                    "Formal Start idempotency or Preview identity was reused"
                )
        return self._formal_start_intent(row), created

    def get_formal_start_intent(self, intent_id: UUID) -> FormalStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM formal_operator_start_intents WHERE intent_id = %s",
                (intent_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"Formal StartIntent not found: {intent_id}")
        return self._formal_start_intent(row)

    def get_formal_start_intent_by_idempotency(
        self,
        idempotency_key: str,
    ) -> FormalStartIntentView | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM formal_operator_start_intents
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            ).fetchone()
        return None if row is None else self._formal_start_intent(row)

    def record_formal_start_reconciliation(
        self,
        intent_id: UUID,
        *,
        state: str,
        blocker_codes: tuple[str, ...],
        checked_at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> FormalStartIntentView:
        if state not in {"awaiting_authority", "ready_for_round_creation", "failed"}:
            raise ValueError("invalid Formal Start reconciliation state")
        if blocker_codes != tuple(sorted(set(blocker_codes))):
            raise ValueError("Formal Start blocker codes must be canonical")
        if state == "awaiting_authority" and not blocker_codes:
            raise ValueError("awaiting Formal Start reconciliation requires blockers")
        if state != "awaiting_authority" and blocker_codes:
            raise ValueError("terminal/ready Formal Start reconciliation cannot carry blockers")
        if (state == "failed") != (error_code is not None and error_message is not None):
            raise ValueError("failed Formal Start reconciliation requires one safe error")
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM formal_operator_start_intents
                WHERE intent_id = %s FOR UPDATE
                """,
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Formal StartIntent not found: {intent_id}")
            if row["state"] in {"cancelled", "failed"}:
                return self._formal_start_intent(row)
            row = connection.execute(
                """
                UPDATE formal_operator_start_intents
                SET state = %s, blocker_codes = %s,
                    error_code = %s, error_message = %s,
                    authority_reconcile_count = authority_reconcile_count + 1,
                    version = version + 1, updated_at = %s,
                    last_reconciled_at = %s,
                    ready_at = CASE
                        WHEN %s = 'ready_for_round_creation' THEN %s
                        ELSE NULL
                    END,
                    authority_ready = (%s = 'ready_for_round_creation')
                WHERE intent_id = %s
                RETURNING *
                """,
                (
                    state,
                    Jsonb(list(blocker_codes)),
                    error_code,
                    error_message,
                    checked_at,
                    checked_at,
                    state,
                    checked_at,
                    state,
                    intent_id,
                ),
            ).fetchone()
            assert row is not None
            connection.execute(
                """
                INSERT INTO formal_operator_start_intent_events (
                    intent_id, sequence, event_type, state, blocker_codes,
                    error_code, occurred_at
                ) VALUES (%s, %s, 'reconciled', %s, %s, %s, %s)
                """,
                (
                    intent_id,
                    row["version"],
                    state,
                    Jsonb(list(blocker_codes)),
                    error_code,
                    checked_at,
                ),
            )
        return self._formal_start_intent(row)

    def cancel_formal_start_intent(
        self,
        intent_id: UUID,
        *,
        cancelled_at: datetime,
    ) -> FormalStartIntentView:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM formal_operator_start_intents
                WHERE intent_id = %s FOR UPDATE
                """,
                (intent_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"Formal StartIntent not found: {intent_id}")
            if row["state"] == "cancelled":
                return self._formal_start_intent(row)
            if row["state"] == "failed":
                raise Conflict("failed Formal StartIntent cannot be cancelled")
            row = connection.execute(
                """
                UPDATE formal_operator_start_intents
                SET state = 'cancelled', blocker_codes = '[]'::jsonb,
                    ready_at = NULL, authority_ready = FALSE,
                    cancelled_at = %s, version = version + 1,
                    updated_at = %s
                WHERE intent_id = %s
                RETURNING *
                """,
                (cancelled_at, cancelled_at, intent_id),
            ).fetchone()
            assert row is not None
            connection.execute(
                """
                INSERT INTO formal_operator_start_intent_events (
                    intent_id, sequence, event_type, state, blocker_codes,
                    error_code, occurred_at
                ) VALUES (%s, %s, 'cancelled', 'cancelled', '[]'::jsonb, NULL, %s)
                """,
                (intent_id, row["version"], cancelled_at),
            )
        return self._formal_start_intent(row)

    def list_recoverable_formal_start_intent_ids(self, limit: int) -> tuple[UUID, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("Formal Start recovery limit must be between 1 and 1000")
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT intent_id FROM formal_operator_start_intents
                WHERE state IN ('awaiting_authority', 'ready_for_round_creation')
                ORDER BY updated_at, intent_id
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return tuple(row["intent_id"] for row in rows)

    def create_search_round(self, request: SearchRound) -> dict[str, Any]:
        """Create or replay one non-executable M2a Scripted Round authority."""

        if request.run_mode is not SearchRoundRunMode.SCRIPTED:
            raise Conflict("M2a Formal Round creation is not approved")
        if (
            request.state is not SearchRoundState.INTAKE_OPEN
            or request.version != 1
            or request.candidate_family_hash is not None
            or request.artifact_family_hash is not None
            or request.holdout_family_hash is not None
            or request.holdout_plan_hash is not None
            or request.intake_closed_at is not None
        ):
            raise Conflict("new M2a Round must be an unfrozen intake_open authority")

        budget = request.budget.model_dump(mode="json")
        with self.connection() as connection:
            authority = connection.execute(
                """
                SELECT
                    run.mode AS stage0_mode,
                    run.state AS stage0_state,
                    run.target_snapshot_id AS stage0_target_snapshot_id,
                    stage0_task.stage0_authority,
                    snapshot.target_id,
                    baseline.target_snapshot_id AS baseline_target_snapshot_id,
                    baseline.stage0_run_id AS baseline_stage0_run_id,
                    baseline.stage0_protocol_hash,
                    baseline.workload_id AS baseline_workload_id,
                    baseline.workload_hash AS baseline_workload_hash,
                    baseline.configuration_hash AS baseline_configuration_hash,
                    baseline.image_digest AS baseline_image_digest,
                    baseline.adapter_profile AS baseline_adapter_profile,
                    hotspot.baseline_epoch_id AS hotspot_baseline_epoch_id,
                    hotspot.candidate_kind AS hotspot_candidate_kind,
                    hotspot.evidence AS hotspot_evidence
                FROM stage0_runs AS run
                JOIN tasks AS stage0_task ON stage0_task.task_id = run.task_id
                JOIN target_snapshots AS snapshot
                  ON snapshot.target_snapshot_id = run.target_snapshot_id
                JOIN baseline_epochs AS baseline ON baseline.baseline_epoch_id = %s
                JOIN hotspots AS hotspot ON hotspot.hotspot_id = %s
                WHERE run.stage0_run_id = %s
                FOR SHARE OF run, stage0_task, snapshot, baseline, hotspot
                """,
                (request.baseline_epoch_id, request.hotspot_id, request.stage0_run_id),
            ).fetchone()
            if authority is None:
                raise NotFound("M2a Scripted Stage 0, Baseline, or Hotspot authority not found")
            expected = {
                "stage0_mode": Stage0RunMode.DRY_RUN.value,
                "stage0_state": Stage0RunState.FINALIZED.value,
                "stage0_authority": "synthetic",
                "stage0_target_snapshot_id": request.target_snapshot_id,
                "baseline_target_snapshot_id": request.target_snapshot_id,
                "baseline_stage0_run_id": request.stage0_run_id,
                "stage0_protocol_hash": request.stage0_protocol_hash,
                "baseline_workload_id": request.workload_id,
                "baseline_workload_hash": request.workload_hash,
                "baseline_configuration_hash": request.configuration_hash,
                "baseline_image_digest": request.image_digest,
                "baseline_adapter_profile": request.adapter_profile,
                "hotspot_baseline_epoch_id": request.baseline_epoch_id,
                "hotspot_candidate_kind": ManualCandidateKind.FIXTURE.value,
            }
            hotspot_evidence = dict(authority["hotspot_evidence"])
            if any(authority[name] != value for name, value in expected.items()) or (
                hotspot_evidence.get("replacement_point") != request.replacement_point
            ):
                raise Conflict("M2a Scripted Round authority bindings do not match")

            task = connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile, stage0_run_id,
                    stage0_authority, project_mode
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s, %s,
                    'synthetic', NULL
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    request.task_id,
                    f"M2a SearchRound {request.round_id}",
                    request.workload_id,
                    request.idempotency_key,
                    TaskState.CREATED.value,
                    Jsonb(budget),
                    WorkflowType.SEARCH_ROUND.value,
                    authority["target_id"],
                    request.target_snapshot_id,
                    request.adapter_profile,
                    request.stage0_run_id,
                ),
            ).fetchone()
            if task is None:
                task = connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE task_id = %s OR idempotency_key = %s
                    FOR UPDATE
                    """,
                    (request.task_id, request.idempotency_key),
                ).fetchone()
                if task is None:
                    raise Conflict("M2a task identity conflict could not be resolved")
            expected_task = {
                "task_id": request.task_id,
                "workload_id": request.workload_id,
                "idempotency_key": request.idempotency_key,
                "workflow_type": WorkflowType.SEARCH_ROUND.value,
                "target_id": authority["target_id"],
                "target_snapshot_id": request.target_snapshot_id,
                "adapter_profile": request.adapter_profile,
                "stage0_run_id": request.stage0_run_id,
                "stage0_authority": "synthetic",
                "project_mode": None,
                "automatic_release_allowed": False,
                "budget": budget,
            }
            if any(task[name] != value for name, value in expected_task.items()):
                raise Conflict("M2a Round idempotency_key was reused with different task inputs")

            payload = request.model_dump(mode="python")
            payload["budget"] = Jsonb(budget)
            round_row = connection.execute(
                """
                INSERT INTO search_rounds (
                    round_id, task_id, idempotency_key, schema_version, state,
                    run_mode, project_mode, target_snapshot_id, stage0_run_id,
                    stage0_protocol_hash, baseline_epoch_id, hotspot_id,
                    replacement_point, workload_id, workload_hash,
                    configuration_hash, image_digest, adapter_profile,
                    declared_candidate_count, max_promoted, family_alpha,
                    search_plan_hash, holdout_plan_commitment,
                    holdout_commitment_scheme, holdout_plan_authority_id,
                    holdout_plan_authority_hash, holdout_plan_hash,
                    holdout_reveal_lease_id, holdout_reveal_evidence_hash,
                    selection_rule_hash, budget, candidate_family_hash,
                    artifact_family_hash, holdout_family_hash,
                    automatic_release_allowed, version, created_at, intake_closed_at
                ) VALUES (
                    %(round_id)s, %(task_id)s, %(idempotency_key)s,
                    %(schema_version)s, %(state)s, %(run_mode)s, %(project_mode)s,
                    %(target_snapshot_id)s, %(stage0_run_id)s,
                    %(stage0_protocol_hash)s, %(baseline_epoch_id)s, %(hotspot_id)s,
                    %(replacement_point)s, %(workload_id)s, %(workload_hash)s,
                    %(configuration_hash)s, %(image_digest)s, %(adapter_profile)s,
                    %(declared_candidate_count)s, %(max_promoted)s, %(family_alpha)s,
                    %(search_plan_hash)s, %(holdout_plan_commitment)s,
                    %(holdout_commitment_scheme)s, %(holdout_plan_authority_id)s,
                    %(holdout_plan_authority_hash)s, %(holdout_plan_hash)s,
                    %(holdout_reveal_lease_id)s, %(holdout_reveal_evidence_hash)s,
                    %(selection_rule_hash)s, %(budget)s, %(candidate_family_hash)s,
                    %(artifact_family_hash)s, %(holdout_family_hash)s, FALSE,
                    %(version)s, %(created_at)s, %(intake_closed_at)s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                payload,
            ).fetchone()
            if round_row is None:
                round_row = connection.execute(
                    """
                    SELECT * FROM search_rounds
                    WHERE round_id = %s OR idempotency_key = %s
                    FOR UPDATE
                    """,
                    (request.round_id, request.idempotency_key),
                ).fetchone()
                if round_row is None:
                    raise Conflict("M2a Round identity conflict could not be resolved")
            creation_fields = (
                "round_id",
                "task_id",
                "idempotency_key",
                "schema_version",
                "run_mode",
                "project_mode",
                "target_snapshot_id",
                "stage0_run_id",
                "stage0_protocol_hash",
                "baseline_epoch_id",
                "hotspot_id",
                "replacement_point",
                "workload_id",
                "workload_hash",
                "configuration_hash",
                "image_digest",
                "adapter_profile",
                "declared_candidate_count",
                "max_promoted",
                "family_alpha",
                "search_plan_hash",
                "holdout_plan_commitment",
                "holdout_commitment_scheme",
                "holdout_plan_authority_id",
                "holdout_plan_authority_hash",
                "selection_rule_hash",
                "budget",
                "automatic_release_allowed",
                "created_at",
            )
            expected_round = request.model_dump(mode="python")
            if any(round_row[name] != expected_round[name] for name in creation_fields):
                raise Conflict("M2a Round idempotency_key was reused with different inputs")
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                SELECT %s, 'm2_round_created', %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM task_events
                    WHERE task_id = %s AND event_type = 'm2_round_created'
                )
                """,
                (
                    request.task_id,
                    Jsonb({"round_id": str(request.round_id), "run_mode": "scripted"}),
                    request.task_id,
                ),
            )
        return round_row

    def add_round_candidate(self, request: RoundCandidate) -> dict[str, Any]:
        """Atomically create/replay one generic Candidate and its Round membership."""

        if (
            request.state is not RoundCandidateState.INTAKE_ACCEPTED
            or request.artifact_id is not None
            or request.terminal_failure_code is not None
        ):
            raise Conflict("M2a intake accepts only unfrozen intake_accepted Candidates")
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (request.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {request.round_id}")

            existing = connection.execute(
                """
                SELECT * FROM round_candidates
                WHERE round_candidate_id = %s OR idempotency_key = %s
                   OR (round_id = %s AND candidate_id = %s)
                   OR (round_id = %s AND ordinal = %s)
                   OR (round_id = %s AND candidate_source_hash = %s)
                   OR (round_id = %s AND source_package_hash = %s)
                   OR (round_id = %s AND source_manifest_hash = %s)
                FOR UPDATE
                """,
                (
                    request.round_candidate_id,
                    request.idempotency_key,
                    request.round_id,
                    request.candidate_id,
                    request.round_id,
                    request.ordinal,
                    request.round_id,
                    request.candidate_source_hash,
                    request.round_id,
                    request.source_package_hash,
                    request.round_id,
                    request.source_manifest_hash,
                ),
            ).fetchone()
            if existing is not None:
                identity_fields = (
                    "round_candidate_id",
                    "round_id",
                    "candidate_id",
                    "ordinal",
                    "source_package_store_id",
                    "source_package_store_hash",
                    "source_package_hash",
                    "source_manifest_version",
                    "source_manifest_hash",
                    "baseline_source_hash",
                    "candidate_source_hash",
                    "optimization_intent",
                    "replacement_point",
                    "track",
                    "release_mode",
                    "candidate_kind",
                    "idempotency_key",
                )
                expected_member = request.model_dump(mode="python")
                if any(existing[name] != expected_member[name] for name in identity_fields):
                    raise Conflict("M2a Candidate identity was reused with different inputs")
                return existing

            if round_row["state"] != SearchRoundState.INTAKE_OPEN.value:
                raise Conflict("M2a Candidate intake is closed")
            if request.ordinal >= round_row["declared_candidate_count"]:
                raise Conflict("M2a Candidate ordinal exceeds the declared family")
            if (
                request.replacement_point != round_row["replacement_point"]
                or request.candidate_kind is not ManualCandidateKind.FIXTURE
            ):
                raise Conflict("M2a Scripted Candidate does not match its Round bindings")

            baseline = connection.execute(
                """
                SELECT baseline.*, source.source_hash AS baseline_source_hash
                FROM baseline_epochs AS baseline
                JOIN source_snapshots AS source
                  ON source.snapshot_id = baseline.source_snapshot_id
                WHERE baseline.baseline_epoch_id = %s
                FOR SHARE OF baseline, source
                """,
                (round_row["baseline_epoch_id"],),
            ).fetchone()
            if (
                baseline is None
                or baseline["baseline_source_hash"] != request.baseline_source_hash
            ):
                raise Conflict("M2a Candidate Baseline source binding does not match")

            metadata = {
                "workflow_type": WorkflowType.SEARCH_ROUND.value,
                "round_candidate_id": str(request.round_candidate_id),
                "source_package_store_id": request.source_package_store_id,
                "source_package_store_hash": request.source_package_store_hash,
                "source_package_hash": request.source_package_hash,
                "source_manifest_version": request.source_manifest_version,
                "source_manifest_hash": request.source_manifest_hash,
                "baseline_source_hash": request.baseline_source_hash,
            }
            candidate = connection.execute(
                """
                INSERT INTO candidates (
                    candidate_id, task_id, round_id, baseline_epoch_id,
                    source_hash, variant, state, ordinal, metadata,
                    track, release_mode, candidate_kind, optimization_intent,
                    replacement_point, idempotency_key, hotspot_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    request.candidate_id,
                    round_row["task_id"],
                    request.round_id,
                    round_row["baseline_epoch_id"],
                    request.candidate_source_hash,
                    "m2-scripted-fixture",
                    CandidateState.PROPOSED.value,
                    request.ordinal,
                    Jsonb(metadata),
                    request.track,
                    request.release_mode,
                    request.candidate_kind.value,
                    request.optimization_intent,
                    request.replacement_point,
                    request.idempotency_key,
                    round_row["hotspot_id"],
                ),
            ).fetchone()
            if candidate is None:
                candidate = connection.execute(
                    """
                    SELECT * FROM candidates
                    WHERE candidate_id = %s OR idempotency_key = %s
                    FOR UPDATE
                    """,
                    (request.candidate_id, request.idempotency_key),
                ).fetchone()
                if candidate is None:
                    raise Conflict("M2a generic Candidate identity conflict could not be resolved")
                expected_candidate = {
                    "candidate_id": request.candidate_id,
                    "task_id": round_row["task_id"],
                    "round_id": request.round_id,
                    "baseline_epoch_id": round_row["baseline_epoch_id"],
                    "source_hash": request.candidate_source_hash,
                    "ordinal": request.ordinal,
                    "track": request.track,
                    "release_mode": request.release_mode,
                    "candidate_kind": request.candidate_kind.value,
                    "optimization_intent": request.optimization_intent,
                    "replacement_point": request.replacement_point,
                    "idempotency_key": request.idempotency_key,
                    "hotspot_id": round_row["hotspot_id"],
                }
                if any(candidate[name] != value for name, value in expected_candidate.items()):
                    raise Conflict("M2a generic Candidate identity belongs to different inputs")

            payload = request.model_dump(mode="python")
            member = connection.execute(
                """
                INSERT INTO round_candidates (
                    round_candidate_id, round_id, candidate_id, ordinal,
                    source_package_store_id, source_package_store_hash,
                    source_package_hash, source_manifest_version,
                    source_manifest_hash, baseline_source_hash,
                    candidate_source_hash, optimization_intent,
                    replacement_point, track, release_mode, candidate_kind,
                    artifact_id, artifact_hash, terminal_failure_code,
                    failure_evidence_hash, state, idempotency_key
                ) VALUES (
                    %(round_candidate_id)s, %(round_id)s, %(candidate_id)s,
                    %(ordinal)s, %(source_package_store_id)s,
                    %(source_package_store_hash)s, %(source_package_hash)s,
                    %(source_manifest_version)s, %(source_manifest_hash)s,
                    %(baseline_source_hash)s, %(candidate_source_hash)s,
                    %(optimization_intent)s, %(replacement_point)s, %(track)s,
                    %(release_mode)s, %(candidate_kind)s, %(artifact_id)s,
                    %(artifact_hash)s, %(terminal_failure_code)s,
                    %(failure_evidence_hash)s, %(state)s, %(idempotency_key)s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                payload,
            ).fetchone()
            if member is None:
                raise Conflict("M2a Candidate unique identity conflicts with another member")
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_round_candidate_registered', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(request.round_id),
                            "round_candidate_id": str(request.round_candidate_id),
                            "candidate_id": str(request.candidate_id),
                            "ordinal": request.ordinal,
                        }
                    ),
                ),
            )
        assert member is not None
        return member

    def close_search_round_intake(self, round_id: UUID) -> dict[str, Any]:
        """Freeze the exact M2a Candidate family under the Round row lock."""

        from hcuopt.orchestrator.search_round import candidate_family_hash

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {round_id}")
            members = connection.execute(
                """
                SELECT * FROM round_candidates
                WHERE round_id = %s ORDER BY ordinal
                """,
                (round_id,),
            ).fetchall()
            if len(members) != round_row["declared_candidate_count"]:
                raise Conflict("M2a Intake Close requires the declared Candidate count")
            if any(
                member["state"] != RoundCandidateState.INTAKE_ACCEPTED.value
                for member in members
            ):
                raise Conflict("M2a Intake Close requires all Candidates intake_accepted")
            family_hash = candidate_family_hash(round_row, members)
            if round_row["state"] != SearchRoundState.INTAKE_OPEN.value:
                if (
                    round_row["candidate_family_hash"] == family_hash
                    and round_row["intake_closed_at"] is not None
                ):
                    return round_row
                raise Conflict("M2a Candidate Intake is already closed with different inputs")

            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = %s, candidate_family_hash = %s,
                    intake_closed_at = now(), version = version + 1, updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (SearchRoundState.INTAKE_CLOSED.value, family_hash, round_id),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_round_intake_closed', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(round_id),
                            "candidate_family_hash": family_hash,
                            "candidate_count": len(members),
                        }
                    ),
                ),
            )
        return round_row

    def record_round_candidate_build(
        self, request: RoundCandidateBuildTerminal
    ) -> dict[str, Any]:
        """Record one immutable Scripted Build success or failure under the Round lock."""

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (request.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {request.round_id}")
            member = connection.execute(
                "SELECT * FROM round_candidates WHERE round_candidate_id = %s FOR UPDATE",
                (request.round_candidate_id,),
            ).fetchone()
            if (
                member is None
                or member["round_id"] != request.round_id
                or member["candidate_id"] != request.candidate_id
            ):
                raise Conflict("M2a Build terminal does not match one Round member")

            terminal_fields = (
                "state",
                "artifact_id",
                "artifact_hash",
                "terminal_failure_code",
                "failure_evidence_hash",
            )
            expected = request.model_dump(mode="python")
            if member["state"] in {
                RoundCandidateState.BUILT.value,
                RoundCandidateState.BUILD_FAILED.value,
                RoundCandidateState.INVALID.value,
            }:
                if any(member[name] != expected[name] for name in terminal_fields):
                    raise Conflict("M2a Candidate already has another Build terminal")
                return member
            if (
                round_row["candidate_family_hash"] is None
                or round_row["artifact_family_hash"] is not None
                or round_row["state"]
                not in {
                    SearchRoundState.INTAKE_CLOSED.value,
                    SearchRoundState.BUILDING.value,
                }
            ):
                raise Conflict("M2a Round is not accepting Build terminals")

            if request.artifact_id is not None:
                artifact = connection.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE artifact_id = %s AND candidate_id = %s
                      AND task_id = %s AND content_hash = %s
                    FOR SHARE
                    """,
                    (
                        request.artifact_id,
                        request.candidate_id,
                        round_row["task_id"],
                        request.artifact_hash,
                    ),
                ).fetchone()
                if artifact is None or not artifact["synthetic"]:
                    raise Conflict(
                        "M2a Scripted Build terminal requires its synthetic Artifact"
                    )

            member = connection.execute(
                """
                UPDATE round_candidates
                SET state = %s, artifact_id = %s, artifact_hash = %s,
                    terminal_failure_code = %s, failure_evidence_hash = %s,
                    updated_at = now()
                WHERE round_candidate_id = %s
                RETURNING *
                """,
                (
                    request.state.value,
                    request.artifact_id,
                    request.artifact_hash,
                    request.terminal_failure_code,
                    request.failure_evidence_hash,
                    request.round_candidate_id,
                ),
            ).fetchone()
            assert member is not None
            generic_state = (
                CandidateState.BUILT
                if request.state is RoundCandidateState.BUILT
                else CandidateState.BUILD_FAILED
            )
            connection.execute(
                "UPDATE candidates SET state = %s, updated_at = now() WHERE candidate_id = %s",
                (generic_state.value, request.candidate_id),
            )
            connection.execute(
                """
                UPDATE search_rounds
                SET state = %s, version = version + 1, updated_at = now()
                WHERE round_id = %s
                """,
                (SearchRoundState.BUILDING.value, request.round_id),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_round_candidate_build_terminal', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(request.round_id),
                            "round_candidate_id": str(request.round_candidate_id),
                            "candidate_id": str(request.candidate_id),
                            "state": request.state.value,
                        }
                    ),
                ),
            )
        return member

    def freeze_search_round_artifact_family(
        self, request: ArtifactFamilyFreezeRequest
    ) -> dict[str, Any]:
        """Recompute and freeze the complete Artifact Family exactly once."""

        from hcuopt.orchestrator.search_round import artifact_family_hash

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (request.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {request.round_id}")
            if round_row["candidate_family_hash"] != request.candidate_family_hash:
                raise Conflict("M2a Artifact Family changed its Candidate Family binding")
            members = connection.execute(
                """
                SELECT * FROM round_candidates
                WHERE round_id = %s ORDER BY ordinal
                FOR UPDATE
                """,
                (request.round_id,),
            ).fetchall()
            try:
                computed = artifact_family_hash(round_row, members)
            except (KeyError, TypeError, ValueError) as error:
                raise Conflict(f"M2a Artifact Family is not ready: {error}") from error
            if computed != request.expected_artifact_family_hash:
                raise Conflict("M2a Artifact Family Hash does not match the frozen members")
            if round_row["artifact_family_hash"] is not None:
                if round_row["artifact_family_hash"] != computed:
                    raise Conflict("M2a Artifact Family is already frozen with other members")
                return round_row
            if round_row["state"] != SearchRoundState.BUILDING.value:
                raise Conflict("M2a Artifact Family requires a building Round")

            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = %s, artifact_family_hash = %s,
                    version = version + 1, updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (
                    SearchRoundState.CORRECTNESS.value,
                    computed,
                    request.round_id,
                ),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_round_artifact_family_frozen', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(request.round_id),
                            "candidate_family_hash": request.candidate_family_hash,
                            "artifact_family_hash": computed,
                        }
                    ),
                ),
            )
        return round_row

    def record_formal_authority_context(
        self, context: FormalAuthorityContextDescriptor
    ) -> dict[str, Any]:
        """Seal one independently verifiable non-Synthetic Formal authority root."""

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (context.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {context.round_id}")
            existing = connection.execute(
                "SELECT * FROM formal_round_authority_contexts WHERE round_id = %s",
                (context.round_id,),
            ).fetchone()
            if existing is not None:
                persisted = self._formal_authority_context(existing)
                if canonical_json_bytes(persisted) != canonical_json_bytes(context):
                    raise Conflict("M2a Formal Authority Context already has other inputs")
                return existing
            if (
                round_row["run_mode"] != SearchRoundRunMode.FORMAL.value
                or round_row["automatic_release_allowed"]
            ):
                raise Conflict("M2a Formal Authority requires a non-releasable Formal Round")
            store = context.evidence_store
            verifier = context.verifier
            row = connection.execute(
                """
                INSERT INTO formal_round_authority_contexts (
                    authority_context_id, context_hash, round_id, task_id,
                    target_snapshot_id, stage0_run_id, stage0_protocol_hash,
                    baseline_epoch_id, hotspot_id, target_profile_hash,
                    workload_profile_hash, measurement_profile_hash,
                    candidate_family_hash, artifact_family_hash, search_plan_hash,
                    holdout_plan_commitment, holdout_plan_authority_id,
                    holdout_plan_authority_hash, selection_rule_hash,
                    evidence_store_id, evidence_store_version, evidence_store_hash,
                    evidence_access_policy_hash, verifier_id, verifier_version,
                    verifier_hash, sealed_by, sealed_at, run_mode, project_mode,
                    synthetic, automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'formal', 'degraded_manual_intake',
                    FALSE, FALSE
                )
                RETURNING *
                """,
                (
                    context.authority_context_id,
                    context.context_hash,
                    context.round_id,
                    context.task_id,
                    context.target_snapshot_id,
                    context.stage0_run_id,
                    context.stage0_protocol_hash,
                    context.baseline_epoch_id,
                    context.hotspot_id,
                    context.target_profile_hash,
                    context.workload_profile_hash,
                    context.measurement_profile_hash,
                    context.candidate_family_hash,
                    context.artifact_family_hash,
                    context.search_plan_hash,
                    context.holdout_plan_commitment,
                    context.holdout_plan_authority_id,
                    context.holdout_plan_authority_hash,
                    context.selection_rule_hash,
                    store.store_id,
                    store.store_version,
                    store.store_hash,
                    store.access_policy_hash,
                    verifier.verifier_id,
                    verifier.verifier_version,
                    verifier.verifier_hash,
                    context.sealed_by,
                    context.sealed_at,
                ),
            ).fetchone()
            assert row is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_formal_authority_context_sealed', %s)
                """,
                (
                    context.task_id,
                    Jsonb(
                        {
                            "round_id": str(context.round_id),
                            "authority_context_id": str(context.authority_context_id),
                            "context_hash": context.context_hash,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return row

    def record_formal_barrier(self, record: FormalBarrierPersistence) -> dict[str, Any]:
        """Persist a D-owned Formal Barrier and its Round state atomically."""

        barrier = record.barrier
        payload = barrier.model_dump(mode="json")
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (barrier.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {barrier.round_id}")
            self._load_formal_authority_context(connection, record.context)
            existing = connection.execute(
                """
                SELECT * FROM formal_round_barriers
                WHERE round_id = %s AND phase = %s
                """,
                (barrier.round_id, barrier.phase.value),
            ).fetchone()
            if existing is not None:
                expected = {
                    "barrier_id": barrier.barrier_id,
                    "authority_context_id": record.context.authority_context_id,
                    "authority_context_hash": record.context.context_hash,
                    "parent_search_barrier_id": record.parent_search_barrier_id,
                    "holdout_family_hash": record.holdout_family_hash,
                    "payload_hash": record.payload_hash,
                    "idempotency_key": barrier.idempotency_key,
                    "payload": payload,
                }
                if any(existing[name] != value for name, value in expected.items()):
                    raise Conflict("M2a Formal Barrier already has other inputs")
                return round_row
            if (
                round_row["run_mode"] != SearchRoundRunMode.FORMAL.value
                or round_row["automatic_release_allowed"]
                or (
                    barrier.phase is RoundPhase.SEARCH
                    and barrier.rule_hash != record.context.selection_rule_hash
                )
            ):
                raise Conflict("M2a Formal Barrier changed its sealed Round authority")
            members = connection.execute(
                """
                SELECT * FROM round_candidates
                WHERE round_id = %s ORDER BY candidate_id
                FOR UPDATE
                """,
                (barrier.round_id,),
            ).fetchall()
            member_by_candidate = {row["candidate_id"]: row for row in members}
            expected_ids = tuple(item.candidate_id for item in barrier.members)
            if barrier.phase is RoundPhase.SEARCH:
                if (
                    round_row["state"]
                    not in {
                        SearchRoundState.CORRECTNESS.value,
                        SearchRoundState.SEARCH_MEASURING.value,
                    }
                    or len(members) != round_row["declared_candidate_count"]
                    or tuple(sorted(member_by_candidate, key=str)) != expected_ids
                ):
                    raise Conflict("M2a Formal Search Barrier is not ready")
            else:
                search_row = connection.execute(
                    """
                    SELECT * FROM formal_round_barriers
                    WHERE round_id = %s AND phase = 'search'
                    """,
                    (barrier.round_id,),
                ).fetchone()
                if search_row is None:
                    raise Conflict("M2a Formal Holdout Barrier requires its Search parent")
                search = RoundBarrierResult.model_validate(search_row["payload"])
                if (
                    round_row["state"] != SearchRoundState.HOLDOUT_MEASURING.value
                    or record.parent_search_barrier_id != search.barrier_id
                    or search.promoted_candidate_ids != expected_ids
                    or round_row["holdout_family_hash"] != record.holdout_family_hash
                ):
                    raise Conflict("M2a Formal Holdout Barrier changed its frozen Family")
            for item in barrier.members:
                stored = member_by_candidate.get(item.candidate_id)
                if (
                    stored is None
                    or stored["round_candidate_id"] != item.round_candidate_id
                    or stored["artifact_id"] != item.artifact_id
                    or stored["artifact_hash"] != item.artifact_hash
                ):
                    raise Conflict("M2a Formal Barrier Candidate or Artifact identity drifted")
                if (
                    stored["state"]
                    in {
                        RoundCandidateState.BUILD_FAILED.value,
                        RoundCandidateState.INVALID.value,
                    }
                    and stored["failure_evidence_hash"] != item.failure_evidence_hash
                ):
                    raise Conflict("M2a Formal Barrier changed frozen failure evidence")
            if barrier.phase is RoundPhase.SEARCH:
                if barrier.promoted_candidate_ids:
                    promoted = tuple(
                        item
                        for item in barrier.members
                        if item.candidate_id in barrier.promoted_candidate_ids
                    )
                    recomputed_family = holdout_family_hash(
                        round_authority=self._search_round_authority(round_row),
                        members=promoted,
                    )
                    if recomputed_family != record.holdout_family_hash:
                        raise Conflict(
                            "M2a Formal Search output Family Hash does not match members"
                        )
                elif record.holdout_family_hash is not None:
                    raise Conflict("M2a zero-promotion Search must not freeze a Holdout Family")
                round_row = connection.execute(
                    """
                    UPDATE search_rounds
                    SET state = 'search_barrier', holdout_family_hash = %s,
                        version = version + 1, updated_at = now()
                    WHERE round_id = %s
                    RETURNING *
                    """,
                    (record.holdout_family_hash, barrier.round_id),
                ).fetchone()
                assert round_row is not None
            connection.execute(
                """
                INSERT INTO formal_round_barriers (
                    barrier_id, round_id, authority_context_id,
                    authority_context_hash, phase, parent_search_barrier_id,
                    input_family_hash, holdout_family_hash, input_summary_hash,
                    rule_version, rule_hash, outcome, expected_member_count,
                    payload, payload_hash, idempotency_key, closed_by, closed_at,
                    run_mode, synthetic, automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, 'formal', FALSE, FALSE
                )
                """,
                (
                    barrier.barrier_id,
                    barrier.round_id,
                    record.context.authority_context_id,
                    record.context.context_hash,
                    barrier.phase.value,
                    record.parent_search_barrier_id,
                    barrier.input_family_hash,
                    record.holdout_family_hash,
                    barrier.input_summary_hash,
                    barrier.rule_version,
                    barrier.rule_hash,
                    barrier.outcome.value,
                    barrier.expected_member_count,
                    Jsonb(payload),
                    record.payload_hash,
                    barrier.idempotency_key,
                    barrier.closed_by,
                    barrier.closed_at,
                ),
            )
            for item in barrier.members:
                connection.execute(
                    """
                    UPDATE round_candidates SET state = %s, updated_at = now()
                    WHERE round_candidate_id = %s
                    """,
                    (item.candidate_state.value, item.round_candidate_id),
                )
            if barrier.phase is RoundPhase.HOLDOUT:
                round_row = connection.execute(
                    """
                    UPDATE search_rounds
                    SET state = 'holdout_barrier', version = version + 1,
                        updated_at = now()
                    WHERE round_id = %s
                    RETURNING *
                    """,
                    (barrier.round_id,),
                ).fetchone()
                assert round_row is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_formal_barrier_closed', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(barrier.round_id),
                            "barrier_id": str(barrier.barrier_id),
                            "phase": barrier.phase.value,
                            "outcome": barrier.outcome.value,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return round_row

    def close_scripted_search_barrier(self, decision: SearchBarrierDecision) -> dict[str, Any]:
        """Persist one D-owned Search Barrier and freeze its promoted family."""

        barrier = decision.barrier
        if (
            barrier.phase is not RoundPhase.SEARCH
            or barrier.run_mode is not SearchRoundRunMode.SCRIPTED
            or not barrier.synthetic
        ):
            raise Conflict("M2a Search Barrier requires scripted Search evidence")
        payload = decision.model_dump(mode="json")
        payload_hash = self._m2_payload_hash(decision)
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (barrier.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {barrier.round_id}")
            existing = connection.execute(
                """
                SELECT * FROM round_barriers
                WHERE round_id = %s AND phase = 'search'
                """,
                (barrier.round_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["barrier_id"] != barrier.barrier_id
                    or existing["idempotency_key"] != barrier.idempotency_key
                    or existing["payload_hash"] != payload_hash
                    or existing["payload"] != payload
                ):
                    raise Conflict("M2a Search Barrier is already closed with other inputs")
                return round_row
            if (
                round_row["run_mode"] != SearchRoundRunMode.SCRIPTED.value
                or round_row["state"]
                not in {
                    SearchRoundState.CORRECTNESS.value,
                    SearchRoundState.SEARCH_MEASURING.value,
                }
                or round_row["artifact_family_hash"] is None
            ):
                raise Conflict("M2a Search Barrier is not ready to close")
            members = connection.execute(
                """
                SELECT * FROM round_candidates
                WHERE round_id = %s ORDER BY candidate_id
                FOR UPDATE
                """,
                (barrier.round_id,),
            ).fetchall()
            if len(members) != round_row["declared_candidate_count"]:
                raise Conflict("M2a Search Barrier lost a frozen Candidate member")
            member_by_candidate = {row["candidate_id"]: row for row in members}
            for item in barrier.members:
                stored = member_by_candidate.get(item.candidate_id)
                if (
                    stored is None
                    or stored["round_candidate_id"] != item.round_candidate_id
                    or stored["artifact_id"] != item.artifact_id
                    or stored["artifact_hash"] != item.artifact_hash
                ):
                    raise Conflict("M2a Search Barrier Candidate or Artifact identity drifted")
                if (
                    stored["state"]
                    in {
                        RoundCandidateState.BUILD_FAILED.value,
                        RoundCandidateState.INVALID.value,
                    }
                    and stored["failure_evidence_hash"] != item.failure_evidence_hash
                ):
                    raise Conflict("M2a Search Barrier changed frozen Build failure evidence")

            authority = self._search_round_authority(round_row).model_copy(
                update={"state": SearchRoundState.SEARCH_BARRIER}
            )
            expected = close_scripted_search_barrier(
                round_authority=authority,
                expected_candidate_ids=tuple(member_by_candidate),
                members=barrier.members,
                statistics=decision.input_summary.statistics,
                closed_by=barrier.closed_by,
                closed_at=barrier.closed_at,
                idempotency_key=barrier.idempotency_key,
            )
            if canonical_json_bytes(expected) != canonical_json_bytes(decision):
                raise Conflict("M2a Search Barrier does not match the D selection authority")

            connection.execute(
                """
                INSERT INTO round_barriers (
                    barrier_id, round_id, phase, input_family_hash,
                    input_summary_hash, rule_version, rule_hash, outcome,
                    expected_member_count, payload, payload_hash,
                    idempotency_key, synthetic, closed_at
                ) VALUES (
                    %s, %s, 'search', %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, TRUE, %s
                )
                """,
                (
                    barrier.barrier_id,
                    barrier.round_id,
                    barrier.input_family_hash,
                    barrier.input_summary_hash,
                    barrier.rule_version,
                    barrier.rule_hash,
                    barrier.outcome.value,
                    barrier.expected_member_count,
                    Jsonb(payload),
                    payload_hash,
                    barrier.idempotency_key,
                    barrier.closed_at,
                ),
            )
            for item in barrier.members:
                connection.execute(
                    """
                    UPDATE round_candidates
                    SET state = %s, updated_at = now()
                    WHERE round_candidate_id = %s
                    """,
                    (item.candidate_state.value, item.round_candidate_id),
                )
            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = 'search_barrier', holdout_family_hash = %s,
                    version = version + 1, updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (decision.holdout_family_hash, barrier.round_id),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_search_barrier_closed', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(barrier.round_id),
                            "barrier_id": str(barrier.barrier_id),
                            "outcome": barrier.outcome.value,
                            "promoted_candidate_ids": [
                                str(value) for value in barrier.promoted_candidate_ids
                            ],
                        }
                    ),
                ),
            )
        return round_row

    def record_formal_holdout_reveal(
        self, reveal: FormalHoldoutRevealPersistence
    ) -> dict[str, Any]:
        """Bind one protected Formal Holdout reveal to its Search output."""

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (reveal.context.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {reveal.context.round_id}")
            _, context = self._load_formal_authority_context(connection, reveal.context)
            existing = connection.execute(
                "SELECT * FROM formal_round_holdout_reveals WHERE round_id = %s",
                (reveal.context.round_id,),
            ).fetchone()
            if existing is not None:
                expected = {
                    "reveal_lease_id": reveal.reveal_lease_id,
                    "authority_context_id": reveal.context.authority_context_id,
                    "authority_context_hash": reveal.context.context_hash,
                    "search_barrier_id": reveal.search_barrier_id,
                    "fencing_token": reveal.fencing_token,
                    "holdout_family_hash": reveal.holdout_family_hash,
                    "holdout_plan_hash": reveal.holdout_plan_hash,
                    "reveal_evidence_uri": reveal.reveal_evidence_uri,
                    "reveal_evidence_hash": reveal.reveal_evidence_hash,
                    "revealed_by": reveal.revealed_by,
                    "revealed_at": reveal.revealed_at,
                }
                if any(existing[name] != value for name, value in expected.items()):
                    raise Conflict("M2a Formal Holdout Plan was already revealed differently")
                return round_row
            search_row = connection.execute(
                """
                SELECT * FROM formal_round_barriers
                WHERE round_id = %s AND phase = 'search'
                """,
                (reveal.context.round_id,),
            ).fetchone()
            if search_row is None:
                raise Conflict("M2a Formal Holdout reveal requires a durable Search Barrier")
            search = RoundBarrierResult.model_validate(search_row["payload"])
            if (
                round_row["run_mode"] != SearchRoundRunMode.FORMAL.value
                or round_row["state"] != SearchRoundState.SEARCH_BARRIER.value
                or search.outcome is not RoundBarrierOutcome.MEMBERS_PROMOTED
                or search.barrier_id != reveal.search_barrier_id
                or search_row["holdout_family_hash"] != reveal.holdout_family_hash
                or round_row["holdout_family_hash"] != reveal.holdout_family_hash
                or reveal.holdout_plan_hash == round_row["search_plan_hash"]
                or reveal.revealed_by != context.verifier.verifier_id
            ):
                raise Conflict("M2a Formal Holdout reveal changed its sealed authority")
            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = 'holdout_measuring', holdout_plan_hash = %s,
                    holdout_reveal_lease_id = %s,
                    holdout_reveal_evidence_hash = %s,
                    version = version + 1, updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (
                    reveal.holdout_plan_hash,
                    reveal.reveal_lease_id,
                    reveal.reveal_evidence_hash,
                    reveal.context.round_id,
                ),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                INSERT INTO formal_round_holdout_reveals (
                    reveal_lease_id, round_id, authority_context_id,
                    authority_context_hash, search_barrier_id, fencing_token,
                    holdout_family_hash, holdout_plan_hash, reveal_evidence_uri,
                    reveal_evidence_hash, revealed_by, revealed_at,
                    run_mode, synthetic, automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'formal', FALSE, FALSE
                )
                """,
                (
                    reveal.reveal_lease_id,
                    reveal.context.round_id,
                    reveal.context.authority_context_id,
                    reveal.context.context_hash,
                    reveal.search_barrier_id,
                    reveal.fencing_token,
                    reveal.holdout_family_hash,
                    reveal.holdout_plan_hash,
                    reveal.reveal_evidence_uri,
                    reveal.reveal_evidence_hash,
                    reveal.revealed_by,
                    reveal.revealed_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_formal_holdout_revealed', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(reveal.context.round_id),
                            "reveal_lease_id": str(reveal.reveal_lease_id),
                            "holdout_family_hash": reveal.holdout_family_hash,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return round_row

    def record_scripted_holdout_reveal(self, reveal: HoldoutRevealResult) -> dict[str, Any]:
        """Persist one synthetic reveal only after the Search Barrier is durable."""

        payload = reveal.model_dump(mode="json")
        payload_hash = self._m2_payload_hash(reveal)
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (reveal.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {reveal.round_id}")
            existing = connection.execute(
                "SELECT * FROM round_holdout_reveals WHERE round_id = %s",
                (reveal.round_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["reveal_lease_id"] != reveal.reveal_lease_id
                    or existing["payload_hash"] != payload_hash
                    or existing["payload"] != payload
                ):
                    raise Conflict("M2a Holdout Plan was already revealed with other inputs")
                return round_row
            search_row = connection.execute(
                """
                SELECT * FROM round_barriers
                WHERE round_id = %s AND phase = 'search'
                """,
                (reveal.round_id,),
            ).fetchone()
            if search_row is None:
                raise Conflict("M2a Holdout reveal requires a durable Search Barrier")
            search = SearchBarrierDecision.model_validate(search_row["payload"])
            if (
                round_row["run_mode"] != SearchRoundRunMode.SCRIPTED.value
                or round_row["state"] != SearchRoundState.SEARCH_BARRIER.value
                or search.barrier.outcome is not RoundBarrierOutcome.MEMBERS_PROMOTED
                or round_row["holdout_family_hash"] != search.holdout_family_hash
                or reveal.holdout_family_hash != search.holdout_family_hash
                or reveal.commitment != round_row["holdout_plan_commitment"]
                or reveal.authority_id != round_row["holdout_plan_authority_id"]
                or reveal.authority_hash != round_row["holdout_plan_authority_hash"]
            ):
                raise Conflict("M2a Holdout reveal changed its frozen Round authority")
            connection.execute(
                """
                INSERT INTO round_holdout_reveals (
                    reveal_lease_id, round_id, holdout_family_hash,
                    plan_hash, reveal_evidence_hash, payload, payload_hash,
                    synthetic, revealed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                """,
                (
                    reveal.reveal_lease_id,
                    reveal.round_id,
                    reveal.holdout_family_hash,
                    reveal.plan_hash,
                    reveal.reveal_evidence_hash,
                    Jsonb(payload),
                    payload_hash,
                    reveal.revealed_at,
                ),
            )
            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = 'holdout_measuring', holdout_plan_hash = %s,
                    holdout_reveal_lease_id = %s,
                    holdout_reveal_evidence_hash = %s,
                    version = version + 1, updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (
                    reveal.plan_hash,
                    reveal.reveal_lease_id,
                    reveal.reveal_evidence_hash,
                    reveal.round_id,
                ),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_holdout_plan_revealed', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(reveal.round_id),
                            "reveal_lease_id": str(reveal.reveal_lease_id),
                            "holdout_family_hash": reveal.holdout_family_hash,
                        }
                    ),
                ),
            )
        return round_row

    def close_scripted_holdout_barrier(self, barrier: RoundBarrierResult) -> dict[str, Any]:
        """Persist the exact promoted family after its Holdout members terminate."""

        if (
            barrier.phase is not RoundPhase.HOLDOUT
            or barrier.run_mode is not SearchRoundRunMode.SCRIPTED
            or not barrier.synthetic
        ):
            raise Conflict("M2a Holdout Barrier requires scripted Holdout evidence")
        payload = barrier.model_dump(mode="json")
        payload_hash = self._m2_payload_hash(barrier)
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (barrier.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {barrier.round_id}")
            existing = connection.execute(
                """
                SELECT * FROM round_barriers
                WHERE round_id = %s AND phase = 'holdout'
                """,
                (barrier.round_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["barrier_id"] != barrier.barrier_id
                    or existing["idempotency_key"] != barrier.idempotency_key
                    or existing["payload_hash"] != payload_hash
                    or existing["payload"] != payload
                ):
                    raise Conflict("M2a Holdout Barrier is already closed with other inputs")
                return round_row
            search_row = connection.execute(
                """
                SELECT * FROM round_barriers
                WHERE round_id = %s AND phase = 'search'
                """,
                (barrier.round_id,),
            ).fetchone()
            reveal_row = connection.execute(
                "SELECT * FROM round_holdout_reveals WHERE round_id = %s",
                (barrier.round_id,),
            ).fetchone()
            if search_row is None or reveal_row is None:
                raise Conflict("M2a Holdout Barrier requires Search and Reveal evidence")
            search = SearchBarrierDecision.model_validate(search_row["payload"])
            if (
                round_row["state"] != SearchRoundState.HOLDOUT_MEASURING.value
                or search.barrier.outcome is not RoundBarrierOutcome.MEMBERS_PROMOTED
                or tuple(item.candidate_id for item in barrier.members)
                != search.barrier.promoted_candidate_ids
            ):
                raise Conflict("M2a Holdout Barrier family is not ready")
            members = connection.execute(
                """
                SELECT * FROM round_candidates
                WHERE round_id = %s FOR UPDATE
                """,
                (barrier.round_id,),
            ).fetchall()
            member_by_candidate = {row["candidate_id"]: row for row in members}
            for item in barrier.members:
                stored = member_by_candidate.get(item.candidate_id)
                if (
                    stored is None
                    or stored["round_candidate_id"] != item.round_candidate_id
                    or stored["artifact_id"] != item.artifact_id
                    or stored["artifact_hash"] != item.artifact_hash
                ):
                    raise Conflict("M2a Holdout Barrier Candidate or Artifact drifted")
            authority = self._search_round_authority(round_row).model_copy(
                update={"state": SearchRoundState.HOLDOUT_BARRIER}
            )
            expected = close_scripted_holdout_barrier(
                round_authority=authority,
                expected_candidate_ids=search.barrier.promoted_candidate_ids,
                members=barrier.members,
                closed_by=barrier.closed_by,
                closed_at=barrier.closed_at,
                idempotency_key=barrier.idempotency_key,
            )
            if canonical_json_bytes(expected) != canonical_json_bytes(barrier):
                raise Conflict("M2a Holdout Barrier does not match the D authority")
            connection.execute(
                """
                INSERT INTO round_barriers (
                    barrier_id, round_id, phase, input_family_hash,
                    input_summary_hash, rule_version, rule_hash, outcome,
                    expected_member_count, payload, payload_hash,
                    idempotency_key, synthetic, closed_at
                ) VALUES (
                    %s, %s, 'holdout', %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, TRUE, %s
                )
                """,
                (
                    barrier.barrier_id,
                    barrier.round_id,
                    barrier.input_family_hash,
                    barrier.input_summary_hash,
                    barrier.rule_version,
                    barrier.rule_hash,
                    barrier.outcome.value,
                    barrier.expected_member_count,
                    Jsonb(payload),
                    payload_hash,
                    barrier.idempotency_key,
                    barrier.closed_at,
                ),
            )
            for item in barrier.members:
                connection.execute(
                    """
                    UPDATE round_candidates SET state = %s, updated_at = now()
                    WHERE round_candidate_id = %s
                    """,
                    (item.candidate_state.value, item.round_candidate_id),
                )
            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = 'holdout_barrier', version = version + 1,
                    updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (barrier.round_id,),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_holdout_barrier_closed', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(barrier.round_id),
                            "barrier_id": str(barrier.barrier_id),
                            "member_count": barrier.expected_member_count,
                        }
                    ),
                ),
            )
        return round_row

    def record_formal_multiple_comparison(
        self, record: FormalMultipleComparisonPersistence
    ) -> MultipleComparisonResult:
        """Persist one verifier-owned Formal FWER result without release authority."""

        result = record.result
        payload = result.model_dump(mode="json")
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (result.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {result.round_id}")
            self._load_formal_authority_context(connection, record.context)
            existing = connection.execute(
                "SELECT * FROM formal_multiple_comparison_results WHERE round_id = %s",
                (result.round_id,),
            ).fetchone()
            if existing is not None:
                expected = {
                    "multiple_comparison_id": result.multiple_comparison_id,
                    "authority_context_id": record.context.authority_context_id,
                    "authority_context_hash": record.context.context_hash,
                    "holdout_barrier_id": result.holdout_barrier_id,
                    "holdout_family_hash": result.holdout_family_hash,
                    "result_hash": result.result_hash,
                    "payload_hash": record.payload_hash,
                    "payload": payload,
                }
                if any(existing[name] != value for name, value in expected.items()):
                    raise Conflict("M2a Formal Multiple Comparison already has other inputs")
                return result
            holdout_row = connection.execute(
                """
                SELECT * FROM formal_round_barriers
                WHERE round_id = %s AND phase = 'holdout'
                """,
                (result.round_id,),
            ).fetchone()
            if holdout_row is None:
                raise Conflict("M2a Formal FWER requires a durable Holdout Barrier")
            holdout = RoundBarrierResult.model_validate(holdout_row["payload"])
            if (
                round_row["run_mode"] != SearchRoundRunMode.FORMAL.value
                or round_row["state"] != SearchRoundState.HOLDOUT_BARRIER.value
                or holdout.barrier_id != result.holdout_barrier_id
                or holdout_row["holdout_family_hash"] != result.holdout_family_hash
                or round_row["holdout_family_hash"] != result.holdout_family_hash
                or float(round_row["family_alpha"]) != result.family_alpha
                or result.protocol_version != M2_FWER_PROTOCOL_VERSION
                or result.protocol_hash != m2_fwer_protocol_hash()
                or result.result_hash != recompute_multiple_comparison_result_hash(result)
            ):
                raise Conflict("M2a Formal FWER changed its Holdout authority")
            connection.execute(
                """
                INSERT INTO formal_multiple_comparison_results (
                    multiple_comparison_id, round_id, authority_context_id,
                    authority_context_hash, holdout_barrier_id,
                    holdout_family_hash, protocol_version, protocol_hash,
                    result_hash, payload, payload_hash, run_mode, synthetic,
                    automatic_release_allowed, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'formal', FALSE, FALSE, %s
                )
                """,
                (
                    result.multiple_comparison_id,
                    result.round_id,
                    record.context.authority_context_id,
                    record.context.context_hash,
                    result.holdout_barrier_id,
                    result.holdout_family_hash,
                    result.protocol_version,
                    result.protocol_hash,
                    result.result_hash,
                    Jsonb(payload),
                    record.payload_hash,
                    result.created_at,
                ),
            )
            connection.execute(
                """
                UPDATE search_rounds
                SET version = version + 1, updated_at = now()
                WHERE round_id = %s
                """,
                (result.round_id,),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_formal_multiple_comparison_recorded', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(result.round_id),
                            "multiple_comparison_id": str(result.multiple_comparison_id),
                            "result_hash": result.result_hash,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return result

    def record_scripted_multiple_comparison(
        self, result: MultipleComparisonResult
    ) -> MultipleComparisonResult:
        """Persist one immutable D-owned Bonferroni result after Holdout closes."""

        if (
            result.run_mode is not SearchRoundRunMode.SCRIPTED
            or not result.synthetic
            or result.protocol_hash != m2_fwer_protocol_hash()
            or result.result_hash != recompute_multiple_comparison_result_hash(result)
        ):
            raise Conflict("M2a Multiple Comparison is not a valid scripted FWER result")
        payload = result.model_dump(mode="json")
        payload_hash = self._m2_payload_hash(result)
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (result.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {result.round_id}")
            existing = connection.execute(
                "SELECT * FROM multiple_comparison_results WHERE round_id = %s",
                (result.round_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["multiple_comparison_id"] != result.multiple_comparison_id
                    or existing["result_hash"] != result.result_hash
                    or existing["payload_hash"] != payload_hash
                    or existing["payload"] != payload
                ):
                    raise Conflict("M2a Multiple Comparison already has other inputs")
                return result
            barrier_row = connection.execute(
                """
                SELECT * FROM round_barriers
                WHERE round_id = %s AND phase = 'holdout'
                """,
                (result.round_id,),
            ).fetchone()
            if (
                round_row["state"] != SearchRoundState.HOLDOUT_BARRIER.value
                or barrier_row is None
                or barrier_row["barrier_id"] != result.holdout_barrier_id
                or round_row["holdout_family_hash"] != result.holdout_family_hash
                or float(round_row["family_alpha"]) != result.family_alpha
            ):
                raise Conflict("M2a Multiple Comparison changed its Holdout authority")
            connection.execute(
                """
                INSERT INTO multiple_comparison_results (
                    multiple_comparison_id, round_id, holdout_barrier_id,
                    holdout_family_hash, protocol_version, protocol_hash,
                    result_hash, payload, payload_hash, synthetic, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                """,
                (
                    result.multiple_comparison_id,
                    result.round_id,
                    result.holdout_barrier_id,
                    result.holdout_family_hash,
                    result.protocol_version,
                    result.protocol_hash,
                    result.result_hash,
                    Jsonb(payload),
                    payload_hash,
                    result.created_at,
                ),
            )
            connection.execute(
                """
                UPDATE search_rounds
                SET version = version + 1, updated_at = now()
                WHERE round_id = %s
                """,
                (result.round_id,),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_multiple_comparison_recorded', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(result.round_id),
                            "multiple_comparison_id": str(result.multiple_comparison_id),
                            "result_hash": result.result_hash,
                        }
                    ),
                ),
            )
        return result

    def finalize_formal_search_round(
        self, record: FormalEvidenceBundlePersistence
    ) -> dict[str, Any]:
        """Rebuild protected Formal evidence and stop at human signoff atomically."""

        evidence = record.bundle
        payload = evidence.model_dump(mode="json")
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (evidence.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {evidence.round_id}")
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE",
                (round_row["task_id"],),
            ).fetchone()
            if task_row is None:
                raise Conflict("M2a Formal Round lost its Task")
            _, context = self._load_formal_authority_context(connection, record.context)
            existing = connection.execute(
                "SELECT * FROM formal_round_evidence_bundles WHERE round_id = %s",
                (evidence.round_id,),
            ).fetchone()
            if existing is not None:
                expected = {
                    "round_evidence_bundle_id": evidence.round_evidence_bundle_id,
                    "authority_context_id": record.context.authority_context_id,
                    "authority_context_hash": record.context.context_hash,
                    "evidence_store_id": record.context.evidence_store.store_id,
                    "evidence_store_hash": record.context.evidence_store.store_hash,
                    "payload_hash": record.payload_hash,
                    "payload": payload,
                }
                if any(existing[name] != value for name, value in expected.items()):
                    raise Conflict("M2a Formal Round already finalized with other evidence")
                if (
                    round_row["state"] != SearchRoundState.AWAITING_SIGNOFF.value
                    or task_row["state"] != TaskState.AWAITING_SIGNOFF.value
                ):
                    raise Conflict("M2a Formal Evidence exists without awaiting_signoff")
            if self.m2_formal_finalizer is None:
                raise Conflict("M2a Formal Finalizer is not configured")
            search_row = connection.execute(
                """
                SELECT * FROM formal_round_barriers
                WHERE round_id = %s AND phase = 'search'
                """,
                (evidence.round_id,),
            ).fetchone()
            if search_row is None:
                raise Conflict("M2a Formal Finalizer requires a durable Search Barrier")
            search = RoundBarrierResult.model_validate(search_row["payload"])
            reveal_row = connection.execute(
                "SELECT * FROM formal_round_holdout_reveals WHERE round_id = %s",
                (evidence.round_id,),
            ).fetchone()
            holdout_row = connection.execute(
                """
                SELECT * FROM formal_round_barriers
                WHERE round_id = %s AND phase = 'holdout'
                """,
                (evidence.round_id,),
            ).fetchone()
            comparison_row = connection.execute(
                "SELECT * FROM formal_multiple_comparison_results WHERE round_id = %s",
                (evidence.round_id,),
            ).fetchone()
            reveal = (
                FormalHoldoutRevealPersistence(
                    context=record.context,
                    search_barrier_id=reveal_row["search_barrier_id"],
                    reveal_lease_id=reveal_row["reveal_lease_id"],
                    fencing_token=reveal_row["fencing_token"],
                    holdout_family_hash=reveal_row["holdout_family_hash"],
                    holdout_plan_hash=reveal_row["holdout_plan_hash"],
                    reveal_evidence_uri=reveal_row["reveal_evidence_uri"],
                    reveal_evidence_hash=reveal_row["reveal_evidence_hash"],
                    revealed_by=reveal_row["revealed_by"],
                    revealed_at=reveal_row["revealed_at"],
                )
                if reveal_row is not None
                else None
            )
            holdout = (
                RoundBarrierResult.model_validate(holdout_row["payload"])
                if holdout_row is not None
                else None
            )
            comparison = (
                MultipleComparisonResult.model_validate(comparison_row["payload"])
                if comparison_row is not None
                else None
            )
            if evidence.terminal_reason is RoundTerminalReason.NO_PROMOTABLE_CANDIDATE:
                if (
                    round_row["state"]
                    != (
                        SearchRoundState.AWAITING_SIGNOFF.value
                        if existing is not None
                        else SearchRoundState.SEARCH_BARRIER.value
                    )
                    or search.outcome is not RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
                    or any(value is not None for value in (reveal, holdout, comparison))
                ):
                    raise Conflict("M2a Formal zero-promotion terminal path is incomplete")
            elif (
                round_row["state"]
                != (
                    SearchRoundState.AWAITING_SIGNOFF.value
                    if existing is not None
                    else SearchRoundState.HOLDOUT_BARRIER.value
                )
                or search.outcome is not RoundBarrierOutcome.MEMBERS_PROMOTED
                or reveal is None
                or holdout is None
                or comparison is None
            ):
                raise Conflict("M2a Formal Holdout terminal path is incomplete")
            reservations = connection.execute(
                """
                SELECT * FROM round_budget_reservations
                WHERE round_id = %s ORDER BY reservation_id
                FOR SHARE
                """,
                (evidence.round_id,),
            ).fetchall()
            if any(
                row["state"] == RoundBudgetReservationState.RESERVED.value
                for row in reservations
            ):
                raise Conflict("M2a Formal Finalizer requires every Budget reservation terminal")
            ledger = connection.execute(
                """
                SELECT * FROM round_budget_ledger
                WHERE round_id = %s ORDER BY ledger_entry_id
                FOR SHARE
                """,
                (evidence.round_id,),
            ).fetchall()
            from hcuopt.orchestrator.search_round import round_budget_ledger_hash

            budget_hash = round_budget_ledger_hash(evidence.round_id, reservations, ledger)
            if evidence.budget_ledger_hash != budget_hash:
                raise Conflict("M2a Formal Round Evidence changed the durable Budget Ledger")
            authority = self._search_round_authority(round_row)
            if existing is not None:
                authority = authority.model_copy(
                    update={
                        "state": (
                            SearchRoundState.SEARCH_BARRIER
                            if evidence.terminal_reason
                            is RoundTerminalReason.NO_PROMOTABLE_CANDIDATE
                            else SearchRoundState.HOLDOUT_BARRIER
                        )
                    }
                )
            try:
                self.m2_formal_finalizer.verify(
                    context=context,
                    round_authority=authority,
                    search_barrier=search,
                    holdout_reveal=reveal,
                    holdout_barrier=holdout,
                    multiple_comparison=comparison,
                    evidence_bundle=evidence,
                )
            except M2RoundEvidenceError as error:
                raise Conflict(f"{error.code}: {error}") from error
            if existing is not None:
                return round_row
            connection.execute(
                """
                INSERT INTO formal_round_evidence_bundles (
                    round_evidence_bundle_id, round_id, task_id,
                    authority_context_id, authority_context_hash,
                    evidence_store_id, evidence_store_hash, terminal_reason,
                    candidate_family_hash, artifact_family_hash,
                    holdout_family_hash, search_plan_hash,
                    holdout_plan_commitment, holdout_plan_hash,
                    holdout_reveal_evidence_hash, search_barrier_id,
                    holdout_barrier_id, multiple_comparison_id,
                    evidence_index_uri, evidence_index_hash, budget_ledger_hash,
                    payload, payload_hash, run_mode, synthetic,
                    automatic_release_allowed, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'formal', FALSE, FALSE, %s
                )
                """,
                (
                    evidence.round_evidence_bundle_id,
                    evidence.round_id,
                    evidence.task_id,
                    record.context.authority_context_id,
                    record.context.context_hash,
                    record.context.evidence_store.store_id,
                    record.context.evidence_store.store_hash,
                    evidence.terminal_reason.value,
                    evidence.candidate_family_hash,
                    evidence.artifact_family_hash,
                    evidence.holdout_family_hash,
                    evidence.search_plan_hash,
                    evidence.holdout_plan_commitment,
                    evidence.holdout_plan_hash,
                    evidence.holdout_reveal_evidence_hash,
                    evidence.search_barrier_id,
                    evidence.holdout_barrier_id,
                    evidence.multiple_comparison_id,
                    evidence.evidence_index_uri,
                    evidence.evidence_index_hash,
                    evidence.budget_ledger_hash,
                    Jsonb(payload),
                    record.payload_hash,
                    evidence.created_at,
                ),
            )
            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = 'awaiting_signoff', version = version + 1,
                    updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (evidence.round_id,),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.AWAITING_SIGNOFF.value, evidence.task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_formal_round_awaiting_signoff', %s)
                """,
                (
                    evidence.task_id,
                    Jsonb(
                        {
                            "round_id": str(evidence.round_id),
                            "round_evidence_bundle_id": str(
                                evidence.round_evidence_bundle_id
                            ),
                            "terminal_reason": evidence.terminal_reason.value,
                            "performance_scope": "formal_single_operation_only",
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return round_row

    def create_formal_round_signoff_intent(
        self,
        request: FormalRoundSignoffRequest,
    ) -> dict[str, Any]:
        """Atomically freeze one human decision and its Artifact outbox."""

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (request.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {request.round_id}")
            existing = connection.execute(
                """
                SELECT * FROM formal_round_signoff_intents
                WHERE idempotency_key = %s OR round_id = %s
                FOR UPDATE
                """,
                (request.idempotency_key, request.round_id),
            ).fetchone()
            if existing is not None:
                context_row = connection.execute(
                    """
                    SELECT * FROM formal_round_authority_contexts
                    WHERE authority_context_id = %s
                    """,
                    (existing["authority_context_id"],),
                ).fetchone()
                assert context_row is not None
                intent = self._formal_signoff_intent(
                    existing, self._formal_authority_context(context_row)
                )
                self._require_signoff_request_replay(request, intent)
                outbox = connection.execute(
                    """
                    SELECT * FROM formal_round_signoff_outbox
                    WHERE signoff_intent_id = %s
                    """,
                    (intent.signoff_intent_id,),
                ).fetchone()
                if outbox is None:
                    raise Conflict("Formal Signoff Intent lost its durable Outbox")
                return {**existing, "outbox": outbox}
            if (
                round_row["run_mode"] != SearchRoundRunMode.FORMAL.value
                or round_row["state"] != SearchRoundState.AWAITING_SIGNOFF.value
                or round_row["automatic_release_allowed"]
            ):
                raise Conflict("Formal Signoff requires a non-releasable awaiting Round")
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE",
                (round_row["task_id"],),
            ).fetchone()
            if task_row is None or (
                task_row["state"] != TaskState.AWAITING_SIGNOFF.value
                or task_row["workflow_type"] != WorkflowType.SEARCH_ROUND.value
                or task_row["automatic_release_allowed"]
            ):
                raise Conflict("Formal Signoff requires its awaiting SearchRound Task")
            evidence_row = connection.execute(
                """
                SELECT * FROM formal_round_evidence_bundles
                WHERE round_evidence_bundle_id = %s
                FOR SHARE
                """,
                (request.round_evidence_bundle_id,),
            ).fetchone()
            if evidence_row is None:
                raise Conflict("Formal Signoff requires the durable final EvidenceBundle")
            context_row = connection.execute(
                """
                SELECT * FROM formal_round_authority_contexts
                WHERE round_id = %s
                FOR SHARE
                """,
                (request.round_id,),
            ).fetchone()
            if context_row is None:
                raise Conflict("Formal Signoff requires durable Formal Authority")
            context = self._formal_authority_context(context_row)
            evidence = RoundEvidenceBundle.model_validate(evidence_row["payload"])
            try:
                require_formal_round_signoff(
                    round_authority=self._search_round_authority(round_row),
                    evidence=evidence,
                )
            except M2RoundEvidenceError as error:
                raise Conflict(f"{error.code}: {error}") from error
            if (
                evidence.round_evidence_bundle_id
                != request.round_evidence_bundle_id
                or evidence_row["round_id"] != request.round_id
                or evidence_row["authority_context_id"] != context.authority_context_id
                or evidence_row["authority_context_hash"] != context.context_hash
                or evidence_row["candidate_family_hash"] != context.candidate_family_hash
                or evidence_row["artifact_family_hash"] != context.artifact_family_hash
                or evidence_row["synthetic"]
                or evidence_row["automatic_release_allowed"]
            ):
                raise Conflict("round_signoff_evidence_mismatch: Formal Evidence identity drifted")
            timestamp = connection.execute(
                "SELECT transaction_timestamp() AS decision_at"
            ).fetchone()
            assert timestamp is not None
            intent = build_formal_round_signoff_intent(
                request=request,
                task_id=round_row["task_id"],
                authority_context=formal_authority_context_ref(context),
                evidence_bundle_hash=evidence_row["payload_hash"],
                candidate_family_hash=evidence_row["candidate_family_hash"],
                artifact_family_hash=evidence_row["artifact_family_hash"],
                holdout_family_hash=evidence_row["holdout_family_hash"],
                decision_at=timestamp["decision_at"],
            )
            intent_row = connection.execute(
                """
                INSERT INTO formal_round_signoff_intents (
                    signoff_intent_id, round_signoff_id, round_id, task_id,
                    authority_context_id, authority_context_hash,
                    round_evidence_bundle_id, evidence_bundle_hash,
                    candidate_family_hash, artifact_family_hash,
                    holdout_family_hash, decision, actor, actor_identity_hash,
                    reason, decision_at, input_digest, idempotency_key, state,
                    run_mode, synthetic, automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, 'preparing', 'formal', FALSE, FALSE
                )
                RETURNING *
                """,
                (
                    intent.signoff_intent_id,
                    intent.round_signoff_id,
                    intent.round_id,
                    intent.task_id,
                    intent.authority_context.authority_context_id,
                    intent.authority_context.context_hash,
                    intent.round_evidence_bundle_id,
                    intent.evidence_bundle_hash,
                    intent.candidate_family_hash,
                    intent.artifact_family_hash,
                    intent.holdout_family_hash,
                    intent.decision,
                    intent.actor,
                    intent.actor_identity_hash,
                    intent.reason,
                    intent.decision_at,
                    intent.input_digest,
                    intent.idempotency_key,
                ),
            ).fetchone()
            assert intent_row is not None
            outbox_event_id = uuid5(
                NAMESPACE_URL,
                f"hcuopt:m2-formal-signoff-outbox:{intent.input_digest}",
            )
            outbox_payload = {
                "schema_version": "m2a-formal-signoff-outbox-v1",
                "intent": intent.model_dump(mode="json"),
            }
            outbox = connection.execute(
                """
                INSERT INTO formal_round_signoff_outbox (
                    outbox_event_id, signoff_intent_id, round_id,
                    aggregate_id, payload, payload_hash, state
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                RETURNING *
                """,
                (
                    outbox_event_id,
                    intent.signoff_intent_id,
                    intent.round_id,
                    intent.round_signoff_id,
                    Jsonb(outbox_payload),
                    self._m2_payload_hash(outbox_payload),
                ),
            ).fetchone()
            assert outbox is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_formal_signoff_intent_created', %s)
                """,
                (
                    intent.task_id,
                    Jsonb(
                        {
                            "round_id": str(intent.round_id),
                            "signoff_intent_id": str(intent.signoff_intent_id),
                            "round_evidence_bundle_id": str(
                                intent.round_evidence_bundle_id
                            ),
                            "decision": intent.decision,
                            "actor": intent.actor,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return {**intent_row, "outbox": outbox}

    @staticmethod
    def _require_signoff_request_replay(
        request: FormalRoundSignoffRequest,
        intent: FormalRoundSignoffIntent,
    ) -> None:
        expected = {
            "round_id": request.round_id,
            "round_evidence_bundle_id": request.round_evidence_bundle_id,
            "decision": request.decision,
            "actor": request.actor,
            "actor_identity_hash": request.actor_identity_hash,
            "reason": request.reason,
            "idempotency_key": request.idempotency_key,
        }
        if any(getattr(intent, name) != value for name, value in expected.items()):
            raise Conflict("Formal Signoff idempotency key has different frozen inputs")

    def record_formal_round_signoff_artifact(
        self,
        publication: FormalRoundSignoffArtifactPublication,
    ) -> dict[str, Any]:
        """Record one immutable publisher result without advancing the Round."""

        with self.connection() as connection:
            intent_row = connection.execute(
                """
                SELECT * FROM formal_round_signoff_intents
                WHERE signoff_intent_id = %s
                FOR UPDATE
                """,
                (publication.signoff_intent_id,),
            ).fetchone()
            if intent_row is None:
                raise NotFound(
                    f"Formal Signoff Intent not found: {publication.signoff_intent_id}"
                )
            outbox = connection.execute(
                """
                SELECT * FROM formal_round_signoff_outbox
                WHERE signoff_intent_id = %s
                FOR UPDATE
                """,
                (publication.signoff_intent_id,),
            ).fetchone()
            if outbox is None:
                raise Conflict("Formal Signoff Intent lost its durable Outbox")
            if (
                intent_row["round_signoff_id"] != publication.round_signoff_id
                or outbox["aggregate_id"] != publication.round_signoff_id
            ):
                raise Conflict("Formal Signoff publication belongs to another Intent")
            signature = publication.signature.model_dump(mode="json")
            if outbox["state"] in {"artifact_published", "finalized"}:
                if (
                    outbox["decision_artifact_uri"]
                    != publication.decision_artifact_uri
                    or outbox["decision_artifact_hash"]
                    != publication.decision_artifact_hash
                    or outbox["signature"] != signature
                ):
                    raise Conflict("Formal Signoff Outbox already published other bytes")
                return outbox
            if intent_row["state"] != "preparing" or outbox["state"] != "pending":
                raise Conflict("Formal Signoff Outbox is not publishable")
            outbox = connection.execute(
                """
                UPDATE formal_round_signoff_outbox
                SET state = 'artifact_published', decision_artifact_uri = %s,
                    decision_artifact_hash = %s, signature = %s,
                    last_error = NULL, updated_at = now()
                WHERE outbox_event_id = %s
                RETURNING *
                """,
                (
                    publication.decision_artifact_uri,
                    publication.decision_artifact_hash,
                    Jsonb(signature),
                    outbox["outbox_event_id"],
                ),
            ).fetchone()
            assert outbox is not None
            connection.execute(
                """
                UPDATE formal_round_signoff_intents
                SET state = 'artifact_published', updated_at = now()
                WHERE signoff_intent_id = %s
                """,
                (publication.signoff_intent_id,),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_formal_signoff_artifact_published', %s)
                """,
                (
                    intent_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(intent_row["round_id"]),
                            "signoff_intent_id": str(publication.signoff_intent_id),
                            "decision_artifact_hash": publication.decision_artifact_hash,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return outbox

    def finalize_formal_round_signoff(
        self,
        publication: FormalRoundSignoffArtifactPublication,
    ) -> dict[str, Any]:
        """Authenticate the decision Artifact, then commit the human terminal state."""

        with self.connection() as connection:
            unlocked_intent = connection.execute(
                """
                SELECT * FROM formal_round_signoff_intents
                WHERE signoff_intent_id = %s
                """,
                (publication.signoff_intent_id,),
            ).fetchone()
            if unlocked_intent is None:
                raise NotFound(
                    f"Formal Signoff Intent not found: {publication.signoff_intent_id}"
                )
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (unlocked_intent["round_id"],),
            ).fetchone()
            intent_row = connection.execute(
                """
                SELECT * FROM formal_round_signoff_intents
                WHERE signoff_intent_id = %s
                FOR UPDATE
                """,
                (publication.signoff_intent_id,),
            ).fetchone()
            if intent_row is None or intent_row["round_id"] != unlocked_intent["round_id"]:
                raise Conflict("Formal Signoff Intent identity changed while locking")
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE",
                (intent_row["task_id"],),
            ).fetchone()
            context_row = connection.execute(
                """
                SELECT * FROM formal_round_authority_contexts
                WHERE authority_context_id = %s
                FOR SHARE
                """,
                (intent_row["authority_context_id"],),
            ).fetchone()
            outbox = connection.execute(
                """
                SELECT * FROM formal_round_signoff_outbox
                WHERE signoff_intent_id = %s
                FOR UPDATE
                """,
                (publication.signoff_intent_id,),
            ).fetchone()
            if any(value is None for value in (round_row, task_row, context_row, outbox)):
                raise Conflict("Formal Signoff lost Round, Task, Authority, or Outbox")
            assert round_row is not None
            assert task_row is not None
            assert context_row is not None
            assert outbox is not None
            context = self._formal_authority_context(context_row)
            intent = self._formal_signoff_intent(intent_row, context)
            if (
                intent_row["state"] not in {"artifact_published", "finalized"}
                or outbox["state"] not in {"artifact_published", "finalized"}
                or outbox["decision_artifact_uri"] is None
                or outbox["decision_artifact_hash"] is None
                or outbox["signature"] is None
            ):
                raise Conflict("Formal Signoff Artifact is not durably published")
            expected_publication = FormalRoundSignoffArtifactPublication(
                signoff_intent_id=intent.signoff_intent_id,
                round_signoff_id=intent.round_signoff_id,
                decision_artifact_uri=outbox["decision_artifact_uri"],
                decision_artifact_hash=outbox["decision_artifact_hash"],
                signature=FormalDecisionSignature.model_validate(outbox["signature"]),
            )
            if publication != expected_publication:
                raise Conflict("Formal Signoff Finalizer received another Artifact identity")
            existing = connection.execute(
                "SELECT * FROM formal_round_signoffs WHERE round_id = %s",
                (intent.round_id,),
            ).fetchone()
            if self.m2_formal_signoff_finalizer is None:
                raise Conflict("Formal Round Signoff Finalizer is not configured")
            try:
                self.m2_formal_signoff_finalizer.verify(
                    intent=intent,
                    publication=publication,
                )
            except M2FormalSignoffError as error:
                raise Conflict(f"{error.code}: {error}") from error
            if existing is not None:
                expected = {
                    "round_signoff_id": intent.round_signoff_id,
                    "signoff_intent_id": intent.signoff_intent_id,
                    "round_evidence_bundle_id": intent.round_evidence_bundle_id,
                    "input_digest": intent.input_digest,
                    "decision_artifact_hash": publication.decision_artifact_hash,
                }
                if any(existing[name] != value for name, value in expected.items()):
                    raise Conflict("Formal Round already has another durable Signoff")
                return {
                    **existing,
                    "round_state": round_row["state"],
                    "task_state": task_row["state"],
                }
            if (
                intent.state != "artifact_published"
                or outbox["state"] != "artifact_published"
                or round_row["state"] != SearchRoundState.AWAITING_SIGNOFF.value
                or task_row["state"] != TaskState.AWAITING_SIGNOFF.value
                or round_row["automatic_release_allowed"]
                or task_row["automatic_release_allowed"]
            ):
                raise Conflict("Formal Round Signoff is not ready to finalize")
            signoff = FormalRoundSignoff(
                round_signoff_id=intent.round_signoff_id,
                signoff_intent_id=intent.signoff_intent_id,
                round_id=intent.round_id,
                task_id=intent.task_id,
                authority_context=intent.authority_context,
                round_evidence_bundle_id=intent.round_evidence_bundle_id,
                evidence_bundle_hash=intent.evidence_bundle_hash,
                decision=intent.decision,
                actor=intent.actor,
                actor_identity_hash=intent.actor_identity_hash,
                reason=intent.reason,
                decision_at=intent.decision_at,
                input_digest=intent.input_digest,
                idempotency_key=intent.idempotency_key,
                decision_artifact_uri=publication.decision_artifact_uri,
                decision_artifact_hash=publication.decision_artifact_hash,
                signature=publication.signature,
            )
            signoff_row = connection.execute(
                """
                INSERT INTO formal_round_signoffs (
                    round_signoff_id, signoff_intent_id, round_id, task_id,
                    authority_context_id, authority_context_hash,
                    round_evidence_bundle_id, evidence_bundle_hash,
                    decision, actor, actor_identity_hash, reason, decision_at,
                    input_digest, idempotency_key, decision_artifact_uri,
                    decision_artifact_hash, signature, run_mode, synthetic,
                    automatic_release_allowed
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, 'formal', FALSE, FALSE
                )
                RETURNING *
                """,
                (
                    signoff.round_signoff_id,
                    signoff.signoff_intent_id,
                    signoff.round_id,
                    signoff.task_id,
                    signoff.authority_context.authority_context_id,
                    signoff.authority_context.context_hash,
                    signoff.round_evidence_bundle_id,
                    signoff.evidence_bundle_hash,
                    signoff.decision,
                    signoff.actor,
                    signoff.actor_identity_hash,
                    signoff.reason,
                    signoff.decision_at,
                    signoff.input_digest,
                    signoff.idempotency_key,
                    signoff.decision_artifact_uri,
                    signoff.decision_artifact_hash,
                    Jsonb(signoff.signature.model_dump(mode="json")),
                ),
            ).fetchone()
            assert signoff_row is not None
            target_round_state = (
                SearchRoundState.COMPLETED
                if signoff.decision == "approved"
                else SearchRoundState.REJECTED
            )
            target_task_state = (
                TaskState.COMPLETED if signoff.decision == "approved" else TaskState.REJECTED
            )
            transition_task(TaskState(task_row["state"]), target_task_state)
            connection.execute(
                """
                UPDATE formal_round_signoff_intents
                SET state = 'finalized', updated_at = now()
                WHERE signoff_intent_id = %s
                """,
                (intent.signoff_intent_id,),
            )
            connection.execute(
                """
                UPDATE formal_round_signoff_outbox
                SET state = 'finalized', updated_at = now()
                WHERE signoff_intent_id = %s
                """,
                (intent.signoff_intent_id,),
            )
            connection.execute(
                """
                UPDATE search_rounds
                SET state = %s, version = version + 1, updated_at = now()
                WHERE round_id = %s
                """,
                (target_round_state.value, intent.round_id),
            )
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (target_task_state.value, intent.task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_formal_round_signed_off', %s)
                """,
                (
                    intent.task_id,
                    Jsonb(
                        {
                            "round_id": str(intent.round_id),
                            "round_signoff_id": str(intent.round_signoff_id),
                            "round_evidence_bundle_id": str(
                                intent.round_evidence_bundle_id
                            ),
                            "decision": intent.decision,
                            "actor": intent.actor,
                            "decision_artifact_hash": publication.decision_artifact_hash,
                            "performance_scope": "formal_single_operation_only",
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return {
            **signoff_row,
            "round_state": target_round_state.value,
            "task_state": target_task_state.value,
        }

    def reconcile_formal_round_signoff(self, round_id: UUID) -> dict[str, Any]:
        """Return the only safe next action without mutating Signoff authority."""

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s",
                (round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {round_id}")
            intent = connection.execute(
                "SELECT * FROM formal_round_signoff_intents WHERE round_id = %s",
                (round_id,),
            ).fetchone()
            outbox = connection.execute(
                "SELECT * FROM formal_round_signoff_outbox WHERE round_id = %s",
                (round_id,),
            ).fetchone()
            signoff = connection.execute(
                "SELECT * FROM formal_round_signoffs WHERE round_id = %s",
                (round_id,),
            ).fetchone()
        if signoff is not None:
            next_action = "complete"
        elif intent is None:
            next_action = (
                "create_intent"
                if round_row["state"] == SearchRoundState.AWAITING_SIGNOFF.value
                else "not_signable"
            )
        elif intent["state"] == "failed":
            next_action = "manual_review"
        elif outbox is None:
            next_action = "repair_outbox"
        elif outbox["state"] == "pending":
            next_action = "publish_artifact"
        elif outbox["state"] == "artifact_published":
            next_action = "finalize_signoff"
        else:
            next_action = "manual_review"
        return {
            "round_id": round_id,
            "round_state": round_row["state"],
            "signoff_intent_id": intent["signoff_intent_id"] if intent else None,
            "intent_state": intent["state"] if intent else None,
            "outbox_event_id": outbox["outbox_event_id"] if outbox else None,
            "outbox_state": outbox["state"] if outbox else None,
            "round_signoff_id": signoff["round_signoff_id"] if signoff else None,
            "next_action": next_action,
            "automatic_release_allowed": False,
        }

    def finalize_scripted_search_round(self, evidence: RoundEvidenceBundle) -> dict[str, Any]:
        """Verify persisted D evidence and enter scripted_completed atomically."""

        if (
            evidence.run_mode is not SearchRoundRunMode.SCRIPTED
            or not evidence.synthetic
            or evidence.automatic_release_allowed
        ):
            raise Conflict("M2a Scripted Finalizer accepts only non-releasable synthetic evidence")
        payload = evidence.model_dump(mode="json")
        payload_hash = self._m2_payload_hash(evidence)
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (evidence.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {evidence.round_id}")
            existing = connection.execute(
                "SELECT * FROM round_evidence_bundles WHERE round_id = %s",
                (evidence.round_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["round_evidence_bundle_id"] != evidence.round_evidence_bundle_id
                    or existing["payload_hash"] != payload_hash
                    or existing["payload"] != payload
                ):
                    raise Conflict("M2a Scripted Round already finalized with other evidence")
                if round_row["state"] != SearchRoundState.SCRIPTED_COMPLETED.value:
                    raise Conflict("M2a Scripted Evidence exists without its terminal state")
                return round_row
            if self.m2_scripted_finalizer is None:
                raise Conflict("M2a Scripted Finalizer is not configured")
            search_row = connection.execute(
                """
                SELECT * FROM round_barriers
                WHERE round_id = %s AND phase = 'search'
                """,
                (evidence.round_id,),
            ).fetchone()
            if search_row is None:
                raise Conflict("M2a Scripted Finalizer requires a durable Search Barrier")
            search = SearchBarrierDecision.model_validate(search_row["payload"])
            holdout_row = connection.execute(
                """
                SELECT * FROM round_barriers
                WHERE round_id = %s AND phase = 'holdout'
                """,
                (evidence.round_id,),
            ).fetchone()
            comparison_row = connection.execute(
                "SELECT * FROM multiple_comparison_results WHERE round_id = %s",
                (evidence.round_id,),
            ).fetchone()
            holdout = (
                RoundBarrierResult.model_validate(holdout_row["payload"])
                if holdout_row is not None
                else None
            )
            comparison = (
                MultipleComparisonResult.model_validate(comparison_row["payload"])
                if comparison_row is not None
                else None
            )
            if evidence.terminal_reason is RoundTerminalReason.NO_PROMOTABLE_CANDIDATE:
                if (
                    round_row["state"] != SearchRoundState.SEARCH_BARRIER.value
                    or search.barrier.outcome is not RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
                    or holdout is not None
                    or comparison is not None
                ):
                    raise Conflict("M2a zero-promotion terminal path is incomplete")
            elif (
                round_row["state"] != SearchRoundState.HOLDOUT_BARRIER.value
                or search.barrier.outcome is not RoundBarrierOutcome.MEMBERS_PROMOTED
                or holdout is None
                or comparison is None
            ):
                raise Conflict("M2a Holdout terminal path is incomplete")
            reservations = connection.execute(
                """
                SELECT * FROM round_budget_reservations
                WHERE round_id = %s ORDER BY reservation_id
                FOR SHARE
                """,
                (evidence.round_id,),
            ).fetchall()
            if any(
                row["state"] == RoundBudgetReservationState.RESERVED.value for row in reservations
            ):
                raise Conflict("M2a Scripted Finalizer requires every Budget reservation terminal")
            ledger = connection.execute(
                """
                SELECT * FROM round_budget_ledger
                WHERE round_id = %s ORDER BY ledger_entry_id
                FOR SHARE
                """,
                (evidence.round_id,),
            ).fetchall()
            from hcuopt.orchestrator.search_round import round_budget_ledger_hash

            budget_hash = round_budget_ledger_hash(evidence.round_id, reservations, ledger)
            if evidence.budget_ledger_hash != budget_hash:
                raise Conflict("M2a Round Evidence changed the durable Budget Ledger")
            authority = self._search_round_authority(round_row)
            try:
                self.m2_scripted_finalizer.verify(
                    round_authority=authority,
                    search_decision=search,
                    evidence_bundle=evidence,
                    holdout_barrier=holdout,
                    multiple_comparison=comparison,
                )
            except M2RoundEvidenceError as error:
                raise Conflict(f"{error.code}: {error}") from error
            connection.execute(
                """
                INSERT INTO round_evidence_bundles (
                    round_evidence_bundle_id, round_id, terminal_reason,
                    evidence_index_uri, evidence_index_hash, budget_ledger_hash,
                    payload, payload_hash, synthetic,
                    automatic_release_allowed, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, FALSE, %s)
                """,
                (
                    evidence.round_evidence_bundle_id,
                    evidence.round_id,
                    evidence.terminal_reason.value,
                    evidence.evidence_index_uri,
                    evidence.evidence_index_hash,
                    evidence.budget_ledger_hash,
                    Jsonb(payload),
                    payload_hash,
                    evidence.created_at,
                ),
            )
            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = 'scripted_completed', version = version + 1,
                    updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (evidence.round_id,),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.COMPLETED.value, round_row["task_id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_scripted_round_completed', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(evidence.round_id),
                            "round_evidence_bundle_id": str(evidence.round_evidence_bundle_id),
                            "terminal_reason": evidence.terminal_reason.value,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return round_row

    def cancel_scripted_search_round(
        self, round_id: UUID, reason: str
    ) -> dict[str, Any]:
        """Stop an idle Scripted Round without inventing cleanup or Budget evidence."""

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {round_id}")
            if round_row["run_mode"] != SearchRoundRunMode.SCRIPTED.value:
                raise Conflict("M2a cancel accepts only a Scripted Round")
            if round_row["state"] == SearchRoundState.CANCELLED.value:
                return round_row
            if round_row["state"] in {
                SearchRoundState.SCRIPTED_COMPLETED.value,
                SearchRoundState.COMPLETED.value,
                SearchRoundState.REJECTED.value,
            }:
                raise Conflict("terminal M2a SearchRound cannot be cancelled")
            finalized_evidence = connection.execute(
                "SELECT 1 FROM round_evidence_bundles WHERE round_id = %s",
                (round_id,),
            ).fetchone()
            if finalized_evidence is not None:
                raise Conflict("M2a Round Evidence exists without a terminal Round state")
            outstanding = connection.execute(
                """
                SELECT reservation_id FROM round_budget_reservations
                WHERE round_id = %s AND state = 'reserved'
                FOR UPDATE
                """,
                (round_id,),
            ).fetchall()
            active_jobs = connection.execute(
                """
                SELECT job_id, state FROM jobs
                WHERE task_id = %s AND state IN ('queued', 'running')
                ORDER BY job_id
                FOR UPDATE
                """,
                (round_row["task_id"],),
            ).fetchall()
            if outstanding or any(
                job["state"] == JobState.RUNNING.value for job in active_jobs
            ):
                raise Conflict(
                    "M2a cancel requires running Jobs cleaned and Budget reservations terminal"
                )
            queued_jobs = connection.execute(
                """
                UPDATE jobs
                SET state = 'cancelled',
                    last_error = %s,
                    finished_at = now(), updated_at = now()
                WHERE task_id = %s AND state = 'queued'
                RETURNING job_id
                """,
                (
                    Jsonb({"code": "round_cancelled", "message": reason}),
                    round_row["task_id"],
                ),
            ).fetchall()
            for job in queued_jobs:
                connection.execute(
                    """
                    INSERT INTO job_events (job_id, event_type, details)
                    VALUES (%s, 'cancelled', %s)
                    """,
                    (job["job_id"], Jsonb({"reason": reason, "scope": "search_round"})),
                )
            connection.execute(
                """
                UPDATE candidates SET state = %s, updated_at = now()
                WHERE round_id = %s
                  AND state NOT IN (%s, %s)
                """,
                (
                    CandidateState.REJECTED.value,
                    round_id,
                    CandidateState.REJECTED.value,
                    CandidateState.BUILD_FAILED.value,
                ),
            )
            round_row = connection.execute(
                """
                UPDATE search_rounds
                SET state = 'cancelled', version = version + 1,
                    updated_at = now()
                WHERE round_id = %s
                RETURNING *
                """,
                (round_id,),
            ).fetchone()
            assert round_row is not None
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.CANCELLED.value, round_row["task_id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_search_round_cancelled', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(round_id),
                            "reason": reason,
                            "queued_job_count": len(queued_jobs),
                        }
                    ),
                ),
            )
        return round_row

    def reconcile_scripted_search_round(self, round_id: UUID) -> dict[str, Any]:
        """Audit durable M2 state and return the only safe next control-plane action."""

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR SHARE",
                (round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {round_id}")
            if round_row["run_mode"] != SearchRoundRunMode.SCRIPTED.value:
                raise Conflict("M2a reconcile accepts only a Scripted Round")
            task_row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR SHARE",
                (round_row["task_id"],),
            ).fetchone()
            if task_row is None:
                raise Conflict("M2a SearchRound lost its durable Task")
            if round_row["state"] == SearchRoundState.CANCELLED.value:
                if task_row["state"] != TaskState.CANCELLED.value:
                    raise Conflict("cancelled M2a Round has a non-cancelled Task")
                return {
                    "round": round_row,
                    "consistent": True,
                    "next_action": "none",
                    "reason": "cancelled Round is terminal",
                    "automatic_release_allowed": False,
                }
            members = connection.execute(
                "SELECT * FROM round_candidates WHERE round_id = %s",
                (round_id,),
            ).fetchall()
            barrier_rows = connection.execute(
                "SELECT * FROM round_barriers WHERE round_id = %s",
                (round_id,),
            ).fetchall()
            barriers = {row["phase"]: row for row in barrier_rows}
            reveal_row = connection.execute(
                "SELECT * FROM round_holdout_reveals WHERE round_id = %s",
                (round_id,),
            ).fetchone()
            comparison_row = connection.execute(
                "SELECT * FROM multiple_comparison_results WHERE round_id = %s",
                (round_id,),
            ).fetchone()
            evidence_row = connection.execute(
                "SELECT * FROM round_evidence_bundles WHERE round_id = %s",
                (round_id,),
            ).fetchone()

        search = (
            SearchBarrierDecision.model_validate(barriers["search"]["payload"])
            if "search" in barriers
            else None
        )
        holdout = (
            RoundBarrierResult.model_validate(barriers["holdout"]["payload"])
            if "holdout" in barriers
            else None
        )
        reveal = (
            HoldoutRevealResult.model_validate(reveal_row["payload"])
            if reveal_row is not None
            else None
        )
        comparison = (
            MultipleComparisonResult.model_validate(comparison_row["payload"])
            if comparison_row is not None
            else None
        )
        evidence = (
            RoundEvidenceBundle.model_validate(evidence_row["payload"])
            if evidence_row is not None
            else None
        )
        if (
            (holdout is not None and (search is None or reveal is None))
            or (
                reveal is not None
                and (search is None or not search.barrier.promoted_candidate_ids)
            )
            or (comparison is not None and holdout is None)
            or (evidence is not None and search is None)
        ):
            raise Conflict("M2a durable authority graph is incomplete")

        if evidence is not None:
            expected_state = SearchRoundState.SCRIPTED_COMPLETED
            next_action = "none"
            reason = "Scripted Round Evidence is finalized"
        elif comparison is not None:
            expected_state = SearchRoundState.HOLDOUT_BARRIER
            next_action = "finalize_scripted_round"
            reason = "FWER is durable; Round Evidence can be finalized"
        elif holdout is not None:
            expected_state = SearchRoundState.HOLDOUT_BARRIER
            next_action = "record_multiple_comparison"
            reason = "Holdout Barrier is durable; FWER is required"
        elif reveal is not None:
            expected_state = SearchRoundState.HOLDOUT_MEASURING
            next_action = "close_holdout_barrier"
            reason = "Holdout Plan is revealed; wait for every Holdout member"
        elif search is not None:
            expected_state = SearchRoundState.SEARCH_BARRIER
            if search.barrier.promoted_candidate_ids:
                next_action = "record_holdout_reveal"
                reason = "Search promoted Candidates; reveal Holdout Plan"
            else:
                next_action = "finalize_scripted_round"
                reason = "Search promoted no Candidate; finalize without Holdout"
        elif round_row["artifact_family_hash"] is not None:
            expected_state = SearchRoundState.CORRECTNESS
            next_action = "close_search_barrier"
            reason = "Artifact Family is frozen; Search Barrier evidence is required"
        elif round_row["candidate_family_hash"] is not None:
            build_terminal = {
                RoundCandidateState.BUILT.value,
                RoundCandidateState.BUILD_FAILED.value,
                RoundCandidateState.INVALID.value,
            }
            terminal_count = sum(row["state"] in build_terminal for row in members)
            if terminal_count:
                expected_state = SearchRoundState.BUILDING
            else:
                expected_state = SearchRoundState.INTAKE_CLOSED
            if terminal_count == round_row["declared_candidate_count"]:
                next_action = "freeze_artifact_family"
                reason = "all Build members are terminal; freeze Artifact Family"
            else:
                next_action = "await_build_terminals"
                reason = "Candidate Family is frozen; wait for Build terminals"
        else:
            expected_state = SearchRoundState.INTAKE_OPEN
            if len(members) == round_row["declared_candidate_count"]:
                next_action = "close_intake"
                reason = "declared Candidate count is present; close Intake"
            else:
                next_action = "await_candidate_intake"
                reason = "Candidate Intake is still incomplete"

        if round_row["state"] != expected_state.value:
            raise Conflict(
                "M2a Round state disagrees with its durable authority graph: "
                f"state={round_row['state']}, expected={expected_state.value}"
            )
        expected_task_state = (
            TaskState.COMPLETED
            if expected_state is SearchRoundState.SCRIPTED_COMPLETED
            else TaskState.CREATED
        )
        if task_row["state"] != expected_task_state.value:
            raise Conflict(
                "M2a Task state disagrees with its Round authority: "
                f"state={task_row['state']}, expected={expected_task_state.value}"
            )
        return {
            "round": round_row,
            "consistent": True,
            "next_action": next_action,
            "reason": reason,
            "automatic_release_allowed": False,
        }

    def get_search_round(self, round_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s", (round_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"M2a SearchRound not found: {round_id}")
        return row

    def search_round_summary(self, round_id: UUID) -> dict[str, Any]:
        round_row = self.get_search_round(round_id)
        with self.connection() as connection:
            members = connection.execute(
                """
                SELECT member.*, candidate.state AS candidate_state,
                       candidate.metadata AS candidate_metadata
                FROM round_candidates AS member
                JOIN candidates AS candidate
                  ON candidate.candidate_id = member.candidate_id
                WHERE member.round_id = %s
                ORDER BY member.ordinal
                """,
                (round_id,),
            ).fetchall()
            reservations = connection.execute(
                """
                SELECT * FROM round_budget_reservations
                WHERE round_id = %s ORDER BY created_at, reservation_id
                """,
                (round_id,),
            ).fetchall()
            ledger = connection.execute(
                """
                SELECT * FROM round_budget_ledger
                WHERE round_id = %s ORDER BY created_at, ledger_entry_id
                """,
                (round_id,),
            ).fetchall()
            barriers = connection.execute(
                """
                SELECT * FROM round_barriers
                WHERE round_id = %s
                ORDER BY CASE phase WHEN 'search' THEN 0 ELSE 1 END, barrier_id
                """,
                (round_id,),
            ).fetchall()
            holdout_reveal = connection.execute(
                "SELECT * FROM round_holdout_reveals WHERE round_id = %s",
                (round_id,),
            ).fetchone()
            multiple_comparison = connection.execute(
                "SELECT * FROM multiple_comparison_results WHERE round_id = %s",
                (round_id,),
            ).fetchone()
            evidence_bundle = connection.execute(
                "SELECT * FROM round_evidence_bundles WHERE round_id = %s",
                (round_id,),
            ).fetchone()
        return {
            "round": round_row,
            "candidates": members,
            "budget_reservations": reservations,
            "budget_ledger": ledger,
            "barriers": [row["payload"] for row in barriers],
            "holdout_reveal": (holdout_reveal["payload"] if holdout_reveal is not None else None),
            "multiple_comparison": (
                multiple_comparison["payload"] if multiple_comparison is not None else None
            ),
            "evidence_bundle": (
                evidence_bundle["payload"] if evidence_bundle is not None else None
            ),
            "automatic_release_allowed": False,
        }

    @staticmethod
    def _round_budget_violation(
        budget: RoundBudget, usage: BudgetUsage
    ) -> str | None:
        limits = {
            "candidates": budget.max_candidates,
            "build_attempts": budget.max_build_attempts,
            "correctness_attempts": budget.max_correctness_attempts,
            "search_samples": budget.max_search_samples,
            "holdout_samples": budget.max_holdout_samples,
            "wall_seconds": budget.max_wall_seconds,
            "exclusive_lease_seconds": budget.max_exclusive_lease_seconds,
        }
        return next(
            (name for name, limit in limits.items() if getattr(usage, name) > limit),
            None,
        )

    def reserve_round_budget(
        self,
        request: RoundBudgetReservation,
        entry: RoundBudgetLedgerEntry,
    ) -> dict[str, Any]:
        """Atomically reserve declared budget before one queued Job Attempt."""

        if request.state is not RoundBudgetReservationState.RESERVED:
            raise Conflict("new M2a Budget reservation must start reserved")
        if request.planned.is_zero():
            raise Conflict("M2a Budget reservation cannot be empty")
        if (
            entry.entry_type is not RoundBudgetEntryType.RESERVE
            or entry.reservation_id != request.reservation_id
            or entry.round_id != request.round_id
            or entry.reserved != request.planned
        ):
            raise Conflict("M2a Budget reserve entry does not match its reservation")

        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (request.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {request.round_id}")
            existing = connection.execute(
                """
                SELECT * FROM round_budget_reservations
                WHERE reservation_id = %s OR idempotency_key = %s
                   OR (job_id = %s AND attempt = %s)
                FOR UPDATE
                """,
                (
                    request.reservation_id,
                    request.idempotency_key,
                    request.job_id,
                    request.attempt,
                ),
            ).fetchone()
            if existing is not None:
                expected = request.model_dump(mode="python")
                immutable_fields = (
                    "reservation_id",
                    "round_id",
                    "job_id",
                    "attempt",
                    "candidate_id",
                    "phase",
                    "planned",
                    "idempotency_key",
                )
                if any(existing[name] != expected[name] for name in immutable_fields):
                    raise Conflict("M2a Budget reservation identity belongs to other inputs")
                reserve_entry = connection.execute(
                    """
                    SELECT * FROM round_budget_ledger
                    WHERE reservation_id = %s AND entry_type = 'reserve'
                    """,
                    (request.reservation_id,),
                ).fetchone()
                if reserve_entry is None:
                    raise Conflict("M2a Budget reservation is missing its reserve ledger event")
                persisted_entry = RoundBudgetLedgerEntry.model_validate(reserve_entry)
                if persisted_entry.model_dump(mode="json") != entry.model_dump(mode="json"):
                    raise Conflict("M2a Budget reserve event was replayed with other inputs")
                return {"reservation": existing, "ledger_entry": reserve_entry}

            if round_row["state"] in {
                SearchRoundState.INTAKE_OPEN.value,
                SearchRoundState.SCRIPTED_COMPLETED.value,
                SearchRoundState.COMPLETED.value,
                SearchRoundState.REJECTED.value,
                SearchRoundState.CANCELLED.value,
            }:
                raise Conflict("M2a Round is not accepting new Budget reservations")
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_id = %s FOR SHARE", (request.job_id,)
            ).fetchone()
            if (
                job is None
                or job["task_id"] != round_row["task_id"]
                or job["state"] != JobState.QUEUED.value
                or request.attempt != job["attempts"] + 1
            ):
                raise Conflict("M2a Budget reservation Job binding is not reservable")
            if request.candidate_id is not None:
                member = connection.execute(
                    """
                    SELECT 1 FROM round_candidates
                    WHERE round_id = %s AND candidate_id = %s
                    """,
                    (request.round_id, request.candidate_id),
                ).fetchone()
                if member is None:
                    raise Conflict("M2a Budget reservation Candidate is not a Round member")

            candidate_count = connection.execute(
                "SELECT count(*) AS count FROM round_candidates WHERE round_id = %s",
                (request.round_id,),
            ).fetchone()
            assert candidate_count is not None
            usage = BudgetUsage(candidates=candidate_count["count"])
            outstanding = connection.execute(
                """
                SELECT planned FROM round_budget_reservations
                WHERE round_id = %s AND state = 'reserved'
                """,
                (request.round_id,),
            ).fetchall()
            settled = connection.execute(
                """
                SELECT actual FROM round_budget_ledger
                WHERE round_id = %s AND entry_type = 'settle'
                """,
                (request.round_id,),
            ).fetchall()
            for row in outstanding:
                usage = usage.plus(BudgetUsage.model_validate(row["planned"]))
            for row in settled:
                usage = usage.plus(BudgetUsage.model_validate(row["actual"]))
            proposed = usage.plus(request.planned)
            violation = self._round_budget_violation(
                RoundBudget.model_validate(round_row["budget"]), proposed
            )
            if violation is not None:
                raise Conflict(f"M2a Round Budget exhausted: {violation}")

            reservation = connection.execute(
                """
                INSERT INTO round_budget_reservations (
                    reservation_id, round_id, job_id, attempt, candidate_id,
                    phase, planned, state, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'reserved', %s)
                RETURNING *
                """,
                (
                    request.reservation_id,
                    request.round_id,
                    request.job_id,
                    request.attempt,
                    request.candidate_id,
                    request.phase.value if request.phase is not None else None,
                    Jsonb(request.planned.model_dump(mode="json")),
                    request.idempotency_key,
                ),
            ).fetchone()
            ledger_entry = connection.execute(
                """
                INSERT INTO round_budget_ledger (
                    ledger_entry_id, reservation_id, round_id, entry_type,
                    reserved, actual, lease_held_seconds, harness_active_seconds,
                    raw_usage_evidence_hash, idempotency_key, created_at
                ) VALUES (%s, %s, %s, 'reserve', %s, %s, 0, 0, %s, %s, %s)
                RETURNING *
                """,
                (
                    entry.ledger_entry_id,
                    entry.reservation_id,
                    entry.round_id,
                    Jsonb(entry.reserved.model_dump(mode="json")),
                    Jsonb(entry.actual.model_dump(mode="json")),
                    entry.raw_usage_evidence_hash,
                    entry.idempotency_key,
                    entry.created_at,
                ),
            ).fetchone()
            assert reservation is not None and ledger_entry is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'm2_round_budget_reserved', %s)
                """,
                (
                    round_row["task_id"],
                    Jsonb(
                        {
                            "round_id": str(request.round_id),
                            "reservation_id": str(request.reservation_id),
                            "job_id": str(request.job_id),
                            "attempt": request.attempt,
                        }
                    ),
                ),
            )
        return {"reservation": reservation, "ledger_entry": ledger_entry}

    def finalize_round_budget(
        self, entry: RoundBudgetLedgerEntry
    ) -> dict[str, Any]:
        """Append one settle/release event and close its reservation exactly once."""

        if entry.entry_type not in {
            RoundBudgetEntryType.SETTLE,
            RoundBudgetEntryType.RELEASE,
        }:
            raise Conflict("M2a Budget terminal event must settle or release")
        terminal_state = (
            RoundBudgetReservationState.SETTLED
            if entry.entry_type is RoundBudgetEntryType.SETTLE
            else RoundBudgetReservationState.RELEASED
        )
        with self.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (entry.round_id,),
            ).fetchone()
            if round_row is None:
                raise NotFound(f"M2a SearchRound not found: {entry.round_id}")
            reservation = connection.execute(
                """
                SELECT * FROM round_budget_reservations
                WHERE reservation_id = %s FOR UPDATE
                """,
                (entry.reservation_id,),
            ).fetchone()
            if reservation is None or reservation["round_id"] != entry.round_id:
                raise Conflict("M2a Budget terminal event has no matching reservation")
            existing = connection.execute(
                """
                SELECT * FROM round_budget_ledger
                WHERE reservation_id = %s AND entry_type IN ('settle', 'release')
                """,
                (entry.reservation_id,),
            ).fetchone()
            if existing is not None:
                persisted = RoundBudgetLedgerEntry.model_validate(existing)
                if persisted.model_dump(mode="json") != entry.model_dump(mode="json"):
                    raise Conflict("M2a Budget reservation already has another terminal event")
                return {"reservation": reservation, "ledger_entry": existing}
            if reservation["state"] != RoundBudgetReservationState.RESERVED.value:
                raise Conflict("M2a Budget reservation is already terminal")
            if BudgetUsage.model_validate(reservation["planned"]) != entry.reserved:
                raise Conflict("M2a Budget terminal event changed the reserved amount")

            ledger_entry = connection.execute(
                """
                INSERT INTO round_budget_ledger (
                    ledger_entry_id, reservation_id, round_id, entry_type,
                    reserved, actual, lease_held_seconds, harness_active_seconds,
                    raw_usage_evidence_hash, idempotency_key, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    entry.ledger_entry_id,
                    entry.reservation_id,
                    entry.round_id,
                    entry.entry_type.value,
                    Jsonb(entry.reserved.model_dump(mode="json")),
                    Jsonb(entry.actual.model_dump(mode="json")),
                    entry.lease_held_seconds,
                    entry.harness_active_seconds,
                    entry.raw_usage_evidence_hash,
                    entry.idempotency_key,
                    entry.created_at,
                ),
            ).fetchone()
            reservation = connection.execute(
                """
                UPDATE round_budget_reservations
                SET state = %s, updated_at = now()
                WHERE reservation_id = %s
                RETURNING *
                """,
                (terminal_state.value, entry.reservation_id),
            ).fetchone()
            assert ledger_entry is not None and reservation is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, %s, %s)
                """,
                (
                    round_row["task_id"],
                    f"m2_round_budget_{entry.entry_type.value}",
                    Jsonb(
                        {
                            "round_id": str(entry.round_id),
                            "reservation_id": str(entry.reservation_id),
                            "ledger_entry_id": str(entry.ledger_entry_id),
                        }
                    ),
                ),
            )
        return {"reservation": reservation, "ledger_entry": ledger_entry}

    def _upsert_target_snapshot(
        self,
        connection: Connection[dict[str, Any]],
        target: TargetSpec,
        source_path: str,
    ) -> dict[str, Any]:
        fingerprint = self._target_fingerprint(target)
        snapshot_id = uuid5(NAMESPACE_URL, f"hcuopt:target:{target.target_id}:{fingerprint}")
        specification = target.model_dump(mode="json")
        row = connection.execute(
            """
            INSERT INTO target_snapshots (
                target_snapshot_id, target_id, target_fingerprint,
                specification, source_path
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING *
            """,
            (
                snapshot_id,
                target.target_id,
                fingerprint,
                Jsonb(specification),
                source_path,
            ),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT *
                FROM target_snapshots
                WHERE target_snapshot_id = %s
                   OR (target_id = %s AND target_fingerprint = %s)
                """,
                (snapshot_id, target.target_id, fingerprint),
            ).fetchone()
        if row is None:
            raise Conflict("target snapshot conflict could not be resolved")
        if (
            row["target_snapshot_id"] != snapshot_id
            or row["target_id"] != target.target_id
            or row["target_fingerprint"] != fingerprint
            or row["specification"] != specification
        ):
            raise Conflict("target snapshot identity was reused with different content")
        return row

    def upsert_target_snapshot(self, target: TargetSpec, source_path: str) -> dict[str, Any]:
        with self.connection() as connection:
            return self._upsert_target_snapshot(connection, target, source_path)

    def create_stage0_run(
        self,
        request: Stage0RunCreate,
        target: TargetSpec,
        source_path: str,
    ) -> dict[str, Any]:
        if request.mode is Stage0RunMode.FORMAL:
            try:
                load_registered_stage0_protocol(request.protocol_version)
            except Stage0ProtocolError as exc:
                raise Conflict(
                    "formal Stage 0 requires a repository-registered protocol: "
                    f"{request.protocol_version}"
                ) from exc
        task_id = uuid5(NAMESPACE_URL, f"hcuopt:stage0-task:{request.idempotency_key}")
        run_id = uuid5(NAMESPACE_URL, f"hcuopt:stage0-run:{request.idempotency_key}")
        budget = request.budget.model_dump(mode="json", exclude_none=True)
        with self.connection() as connection:
            snapshot = self._upsert_target_snapshot(connection, target, source_path)
            task = connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    task_id,
                    request.name,
                    request.workload_id,
                    request.idempotency_key,
                    TaskState.STAGE0_PENDING.value,
                    Jsonb(budget),
                    WorkflowType.STAGE0.value,
                    target.target_id,
                    snapshot["target_snapshot_id"],
                    request.adapter_profile,
                ),
            ).fetchone()
            if task is None:
                task = connection.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = %s FOR UPDATE",
                    (request.idempotency_key,),
                ).fetchone()
                assert task is not None
            expected_task = {
                "name": request.name,
                "workload_id": request.workload_id,
                "budget": budget,
                "workflow_type": WorkflowType.STAGE0.value,
                "target_id": target.target_id,
                "target_snapshot_id": snapshot["target_snapshot_id"],
                "adapter_profile": request.adapter_profile,
            }
            if any(task[name] != value for name, value in expected_task.items()):
                raise Conflict("Stage 0 idempotency_key was reused with different inputs")

            run = connection.execute(
                """
                INSERT INTO stage0_runs (
                    stage0_run_id, task_id, target_snapshot_id, adapter_profile,
                    mode, protocol_version, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    run_id,
                    task["task_id"],
                    snapshot["target_snapshot_id"],
                    request.adapter_profile,
                    request.mode.value,
                    request.protocol_version,
                    request.idempotency_key,
                ),
            ).fetchone()
            if run is None:
                run = connection.execute(
                    "SELECT * FROM stage0_runs WHERE idempotency_key = %s FOR UPDATE",
                    (request.idempotency_key,),
                ).fetchone()
                assert run is not None
            expected_run = {
                "task_id": task["task_id"],
                "target_snapshot_id": snapshot["target_snapshot_id"],
                "adapter_profile": request.adapter_profile,
                "mode": request.mode.value,
                "protocol_version": request.protocol_version,
            }
            if any(run[name] != value for name, value in expected_run.items()):
                raise Conflict("Stage 0 idempotency_key was reused with a different run")

            for probe_type in sorted(REQUIRED_STAGE0_PROBES, key=lambda item: item.value):
                lease_scope = (
                    LeaseScope.EXCLUSIVE
                    if request.mode is Stage0RunMode.FORMAL
                    or probe_type is Stage0ProbeType.HOTPATCH
                    else LeaseScope.NONE
                )
                job_id = uuid5(
                    NAMESPACE_URL,
                    f"hcuopt:{run['stage0_run_id']}:{probe_type.value}:v1",
                )
                payload = {
                    "stage0_run_id": str(run["stage0_run_id"]),
                    "task_id": str(task["task_id"]),
                    "probe_type": probe_type.value,
                    "target_snapshot_id": str(snapshot["target_snapshot_id"]),
                    "target_fingerprint": snapshot["target_fingerprint"],
                    "target": target.model_dump(mode="json"),
                    "workload_id": task["workload_id"],
                    "adapter_profile": run["adapter_profile"],
                    "protocol_version": request.protocol_version,
                    "mode": request.mode.value,
                    "budget": budget,
                }
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, task_id, job_type, accepted_worker_type,
                        adapter_profile, lease_scope, payload, idempotency_key,
                        priority, max_attempts
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 3)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        job_id,
                        task["task_id"],
                        JobType.STAGE0_PROBE.value,
                        WorkerType.GPU.value,
                        request.adapter_profile,
                        lease_scope.value,
                        Jsonb(payload),
                        f"{run['stage0_run_id']}:{probe_type.value}:v1",
                    ),
                )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                SELECT %s, 'stage0_run_created', %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM task_events
                    WHERE task_id = %s AND event_type = 'stage0_run_created'
                )
                """,
                (
                    task["task_id"],
                    Jsonb(
                        {
                            "stage0_run_id": str(run["stage0_run_id"]),
                            "target_snapshot_id": str(snapshot["target_snapshot_id"]),
                            "mode": request.mode.value,
                            "protocol_version": request.protocol_version,
                            "probe_types": sorted(item.value for item in REQUIRED_STAGE0_PROBES),
                        }
                    ),
                    task["task_id"],
                ),
            )
        return run

    def create_manual_candidate_task(
        self,
        request: ManualCandidateTaskCreate,
    ) -> dict[str, Any]:
        """Create the M1 task and its immutable Baseline Epoch atomically."""

        task_id = uuid5(
            NAMESPACE_URL, f"hcuopt:m1-task:{request.idempotency_key}"
        )
        baseline_epoch_id = uuid5(
            NAMESPACE_URL, f"hcuopt:{task_id}:m1-baseline:v1"
        )
        budget = request.budget.model_dump(mode="json", exclude_none=True)
        with self.connection() as connection:
            stage0 = connection.execute(
                """
                SELECT
                    run.stage0_run_id,
                    run.mode AS run_mode,
                    run.state AS run_state,
                    run.protocol_version,
                    run.target_snapshot_id,
                    task.task_id AS stage0_task_id,
                    task.workload_id AS stage0_workload_id,
                    task.stage0_authority,
                    task.project_mode,
                    task.automatic_release_allowed,
                    stage0_evidence.evidence AS formal_evidence,
                    stage0_evidence.report AS formal_report,
                    snapshot.target_id,
                    snapshot.target_fingerprint,
                    snapshot.specification
                FROM stage0_runs AS run
                JOIN tasks AS task ON task.task_id = run.task_id
                JOIN stage0_evidence
                  ON stage0_evidence.stage0_run_id = run.stage0_run_id
                JOIN target_snapshots AS snapshot
                  ON snapshot.target_snapshot_id = run.target_snapshot_id
                WHERE run.stage0_run_id = %s
                FOR SHARE OF run, task, snapshot
                """,
                (request.stage0_run_id,),
            ).fetchone()
            if stage0 is None:
                raise NotFound(f"Stage 0 run not found: {request.stage0_run_id}")
            if (
                stage0["run_mode"] != Stage0RunMode.FORMAL.value
                or stage0["run_state"] != Stage0RunState.FINALIZED.value
                or stage0["stage0_authority"] != "formal"
            ):
                raise Conflict("M1 requires a finalized Formal Stage 0 run")
            if stage0["project_mode"] != ProjectMode.DEGRADED_MANUAL_INTAKE.value:
                raise Conflict(
                    "M1 currently requires degraded_manual_intake from Formal Stage 0"
                )
            if stage0["automatic_release_allowed"]:
                raise Conflict("M1 cannot inherit automatic release authority")
            stage0_protocol_hash = stage0["formal_evidence"].get("protocol_hash")
            if (
                stage0["formal_report"].get("evidence_authority") != "formal"
                or stage0["formal_report"].get("automatic_release_allowed") is not False
                or stage0["formal_evidence"].get("synthetic") is not False
                or stage0["formal_evidence"].get("stage0_run_id")
                != str(request.stage0_run_id)
                or stage0["formal_evidence"].get("protocol_version")
                != stage0["protocol_version"]
                or not isinstance(stage0_protocol_hash, str)
                or re.fullmatch(SHA256_PATTERN, stage0_protocol_hash) is None
            ):
                raise Conflict("M1 requires the independently verified Stage 0 report")

            target = TargetSpec.model_validate(stage0["specification"])
            source = connection.execute(
                """
                SELECT * FROM source_snapshots
                WHERE snapshot_id = %s
                FOR SHARE
                """,
                (request.baseline_source_snapshot_id,),
            ).fetchone()
            if source is None:
                raise NotFound(
                    "baseline SourceSnapshot not found: "
                    f"{request.baseline_source_snapshot_id}"
                )
            provenance = source["adapter_provenance"]
            if (
                source["kind"] != "baseline"
                or not source["clean"]
                or source["synthetic"]
                or not isinstance(provenance, list)
                or not provenance
                or any(
                    not isinstance(item, dict)
                    or item.get("implementation_kind") == "fake"
                    for item in provenance
                )
            ):
                raise Conflict("M1 requires a clean, real Baseline SourceSnapshot")
            if (
                source["repository"] != target.source_baseline.repository
                or source["commit"] != target.source_baseline.commit
            ):
                raise Conflict("Baseline SourceSnapshot does not match the Target Lock")

            hardware_fingerprint = self._digest_json(
                {
                    "host": target.execution_host.name,
                    "accelerator": target.execution_host.accelerator.model,
                    "architecture": target.execution_host.accelerator.architecture,
                    "device": target.execution_host.accelerator.device_index,
                    "target_fingerprint": stage0["target_fingerprint"],
                }
            )
            software_fingerprint = self._digest_json(
                {
                    "image_digest": target.inference_image.registry_digest,
                    "source_hash": source["source_hash"],
                    "source_commit": source["commit"],
                    "dtk": target.inference_image.dtk_version,
                    "sglang": target.inference_image.sglang_package_version,
                }
            )
            task = connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile, stage0_run_id,
                    stage0_authority, project_mode
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s, %s,
                    'formal', %s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    task_id,
                    request.name,
                    request.workload_id,
                    request.idempotency_key,
                    TaskState.MANUAL_CANDIDATE_PENDING.value,
                    Jsonb(budget),
                    WorkflowType.MANUAL_CANDIDATE.value,
                    stage0["target_id"],
                    stage0["target_snapshot_id"],
                    request.adapter_profile,
                    request.stage0_run_id,
                    stage0["project_mode"],
                ),
            ).fetchone()
            if task is None:
                task = connection.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = %s FOR UPDATE",
                    (request.idempotency_key,),
                ).fetchone()
                assert task is not None
            expected_task = {
                "name": request.name,
                "workload_id": request.workload_id,
                "budget": budget,
                "workflow_type": WorkflowType.MANUAL_CANDIDATE.value,
                "target_id": stage0["target_id"],
                "target_snapshot_id": stage0["target_snapshot_id"],
                "adapter_profile": request.adapter_profile,
                "stage0_run_id": request.stage0_run_id,
                "automatic_release_allowed": False,
            }
            if any(task[name] != value for name, value in expected_task.items()):
                raise Conflict("M1 idempotency_key was reused with different task inputs")

            baseline = connection.execute(
                """
                INSERT INTO baseline_epochs (
                    baseline_epoch_id, task_id, hardware_fingerprint,
                    software_fingerprint, workload_id, configuration_hash,
                    baseline_kind, target_snapshot_id, stage0_run_id,
                    stage0_protocol_hash, source_snapshot_id, workload_hash, image_digest,
                    adapter_profile
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (task_id) DO NOTHING
                RETURNING *
                """,
                (
                    baseline_epoch_id,
                    task["task_id"],
                    hardware_fingerprint,
                    software_fingerprint,
                    request.workload_id,
                    request.configuration_hash,
                    WorkflowType.MANUAL_CANDIDATE.value,
                    stage0["target_snapshot_id"],
                    request.stage0_run_id,
                    stage0_protocol_hash,
                    request.baseline_source_snapshot_id,
                    request.workload_hash,
                    target.inference_image.registry_digest,
                    request.adapter_profile,
                ),
            ).fetchone()
            if baseline is None:
                baseline = connection.execute(
                    "SELECT * FROM baseline_epochs WHERE task_id = %s",
                    (task["task_id"],),
                ).fetchone()
                assert baseline is not None
            expected_baseline = {
                "baseline_epoch_id": baseline_epoch_id,
                "workload_id": request.workload_id,
                "target_snapshot_id": stage0["target_snapshot_id"],
                "stage0_run_id": request.stage0_run_id,
                "stage0_protocol_hash": stage0_protocol_hash,
                "source_snapshot_id": request.baseline_source_snapshot_id,
                "workload_hash": request.workload_hash,
                "configuration_hash": request.configuration_hash,
                "image_digest": target.inference_image.registry_digest,
                "adapter_profile": request.adapter_profile,
                "baseline_kind": WorkflowType.MANUAL_CANDIDATE.value,
            }
            if any(
                baseline[name] != value for name, value in expected_baseline.items()
            ):
                raise Conflict("M1 task already has a different immutable Baseline Epoch")

            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                SELECT %s, 'manual_candidate_task_created', %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM task_events
                    WHERE task_id = %s
                      AND event_type = 'manual_candidate_task_created'
                )
                """,
                (
                    task["task_id"],
                    Jsonb(
                        {
                            "stage0_run_id": str(request.stage0_run_id),
                            "baseline_epoch_id": str(baseline_epoch_id),
                            "baseline_source_snapshot_id": str(
                                request.baseline_source_snapshot_id
                            ),
                            "target_snapshot_id": str(stage0["target_snapshot_id"]),
                            "stage0_task_id": str(stage0["stage0_task_id"]),
                            "stage0_workload_id": stage0["stage0_workload_id"],
                            "workload_id": request.workload_id,
                            "project_mode": stage0["project_mode"],
                            "automatic_release_allowed": False,
                        }
                    ),
                    task["task_id"],
                ),
            )
        return task

    def create_framework_smoke_task(
        self,
        request: FrameworkSmokeCreate,
        target: TargetSpec,
        source_path: str,
    ) -> dict[str, Any]:
        task_id = uuid4()
        with self.connection() as connection:
            snapshot = self._upsert_target_snapshot(connection, target, source_path)
            row = connection.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile
                ) VALUES (
                    %s, %s, %s, %s, %s, '{}'::jsonb, FALSE,
                    %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    task_id,
                    request.name,
                    f"framework-smoke:{target.target_id}",
                    request.idempotency_key,
                    TaskState.SOURCE_PREPARING.value,
                    WorkflowType.FRAMEWORK_SMOKE.value,
                    target.target_id,
                    snapshot["target_snapshot_id"],
                    request.adapter_profile,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = %s FOR UPDATE",
                    (request.idempotency_key,),
                ).fetchone()
                assert row is not None
                expected = {
                    "name": request.name,
                    "workflow_type": WorkflowType.FRAMEWORK_SMOKE.value,
                    "target_id": target.target_id,
                    "target_snapshot_id": snapshot["target_snapshot_id"],
                    "adapter_profile": request.adapter_profile,
                }
                if any(row[name] != value for name, value in expected.items()):
                    raise Conflict(
                        "framework smoke idempotency_key was reused with different inputs"
                    )
                return row

            job_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:source-prepare:v1")
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    adapter_profile, lease_scope, payload, idempotency_key,
                    priority, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, 'none', %s, %s, 0, 3)
                """,
                (
                    job_id,
                    task_id,
                    JobType.SOURCE_PREPARE.value,
                    WorkerType.BUILD.value,
                    request.adapter_profile,
                    Jsonb(
                        {
                            "task_id": str(task_id),
                            "target": target.model_dump(mode="json"),
                            "target_snapshot_id": str(snapshot["target_snapshot_id"]),
                        }
                    ),
                    f"{task_id}:source-prepare:v1",
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'framework_smoke_created', %s)
                """,
                (
                    task_id,
                    Jsonb(
                        {
                            "target_id": target.target_id,
                            "target_fingerprint": snapshot["target_fingerprint"],
                            "adapter_profile": request.adapter_profile,
                            "first_job_id": str(job_id),
                        }
                    ),
                ),
            )
        return row

    def get_task(self, task_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s", (task_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"task not found: {task_id}")
        return row

    def get_job(self, job_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"job not found: {job_id}")
        return row

    def get_candidate(self, candidate_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = %s", (candidate_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"candidate not found: {candidate_id}")
        return row

    def _transition_task(
        self,
        connection: Connection[dict[str, Any]],
        task_id: UUID,
        target: TaskState,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"task not found: {task_id}")
        current = TaskState(row["state"])
        transition_task(current, target)
        updated = connection.execute(
            """
            UPDATE tasks
            SET state = %s, version = version + 1, updated_at = now()
            WHERE task_id = %s AND version = %s
            RETURNING *
            """,
            (target.value, task_id, row["version"]),
        ).fetchone()
        if updated is None:
            raise Conflict("task was changed concurrently")
        return updated

    def save_stage0(
        self,
        task_id: UUID,
        evidence: Stage0EvidenceRequest,
        mode: ProjectMode,
        reasons: tuple[str, ...],
    ) -> dict[str, Any]:
        if mode is ProjectMode.STOPPED_MEASUREMENT:
            target = TaskState.STOPPED_MEASUREMENT
        elif mode in {ProjectMode.DEGRADED_MANUAL_INTAKE, ProjectMode.CONFIG_ONLY}:
            target = TaskState.DEGRADED
        else:
            target = TaskState.BASELINE_PENDING
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["state"] != TaskState.STAGE0_PENDING.value:
                existing = connection.execute(
                    "SELECT report FROM stage0_evidence WHERE task_id = %s", (task_id,)
                ).fetchone()
                if existing is not None:
                    return existing["report"]
                raise Conflict("Stage 0 can only run from stage0_pending")
            transition_task(TaskState(task["state"]), target)
            report = {
                "task_id": str(task_id),
                "mode": mode.value,
                "reasons": list(reasons),
                "automatic_release_allowed": False,
                "evidence_authority": "synthetic_control_flow_only",
            }
            connection.execute(
                """
                INSERT INTO stage0_evidence (task_id, evidence, report)
                VALUES (%s, %s, %s)
                """,
                (
                    task_id,
                    Jsonb(evidence.model_dump(mode="json")),
                    Jsonb(report),
                ),
            )
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, project_mode = %s, stage0_authority = 'synthetic',
                    version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (target.value, mode.value, task_id),
            )
        return report

    def get_stage0_run(self, stage0_run_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM stage0_runs WHERE stage0_run_id = %s",
                (stage0_run_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"Stage 0 run not found: {stage0_run_id}")
        return row

    def list_stage0_probe_records(self, stage0_run_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM stage0_probe_records
                WHERE stage0_run_id = %s ORDER BY created_at, probe_type
                """,
                (stage0_run_id,),
            ).fetchall()

    def stage0_run_summary(self, stage0_run_id: UUID) -> dict[str, Any]:
        run = self.get_stage0_run(stage0_run_id)
        task = self.get_task(run["task_id"])
        target = self.get_target_snapshot(task["task_id"])
        with self.connection() as connection:
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE task_id = %s ORDER BY created_at",
                (task["task_id"],),
            ).fetchall()
        return {
            "run": run,
            "task": task,
            "target": target["specification"],
            "probes": self.list_stage0_probe_records(stage0_run_id),
            "jobs": jobs,
            "events": self.list_task_events(task["task_id"]),
        }

    def record_stage0_probe(
        self,
        job: Mapping[str, Any],
        result: Stage0ProbeResult,
    ) -> dict[str, Any]:
        job_id = UUID(str(job["job_id"]))
        task_id = UUID(str(job["task_id"]))
        payload = job["payload"]
        if job["job_type"] != JobType.STAGE0_PROBE.value:
            raise Conflict("only Stage 0 probe jobs can produce probe records")
        if job["state"] != JobState.SUCCEEDED.value:
            raise Conflict("Stage 0 probe evidence requires a succeeded job")
        if Stage0ProbeResult.model_validate(job.get("result")) != result:
            raise Conflict("Stage 0 probe record must match the completed job result")
        with self.connection() as connection:
            run = connection.execute(
                "SELECT * FROM stage0_runs WHERE stage0_run_id = %s FOR UPDATE",
                (result.stage0_run_id,),
            ).fetchone()
            if run is None:
                raise NotFound(f"Stage 0 run not found: {result.stage0_run_id}")
            expected = {
                "stage0_run_id": str(run["stage0_run_id"]),
                "task_id": str(run["task_id"]),
                "target_snapshot_id": str(run["target_snapshot_id"]),
                "probe_type": result.probe_type.value,
                "protocol_version": run["protocol_version"],
            }
            if any(str(payload.get(name)) != value for name, value in expected.items()):
                raise Conflict("Stage 0 probe job payload does not match its run")
            if task_id != run["task_id"]:
                raise Conflict("Stage 0 probe job belongs to a different task")
            if result.target_snapshot_id != run["target_snapshot_id"]:
                raise Conflict("Stage 0 probe result targets a different snapshot")
            if result.protocol_version != run["protocol_version"]:
                raise Conflict("Stage 0 probe protocol version changed during the run")
            if run["state"] in {
                Stage0RunState.FINALIZED.value,
                Stage0RunState.FAILED.value,
            }:
                raise Conflict("terminal Stage 0 runs cannot accept probe records")

            if run["mode"] == Stage0RunMode.FORMAL.value:
                if result.synthetic or any(
                    item.implementation_kind != "real" for item in result.adapter_provenance
                ):
                    raise Conflict("formal Stage 0 requires real probe provenance")
                if job["lease_scope"] != LeaseScope.EXCLUSIVE.value:
                    raise Conflict("formal Stage 0 probes require an exclusive lease")
                if any(
                    job.get(name) is None for name in ("lease_id", "resource_id", "fencing_token")
                ):
                    raise Conflict("formal Stage 0 probes require lease and fencing identity")
                if not _cleanup_is_healthy(result.cleanup_evidence):
                    raise Conflict("formal Stage 0 probes require healthy fenced cleanup")
            if any(item.profile != run["adapter_profile"] for item in result.adapter_provenance):
                raise Conflict("Stage 0 probe provenance does not match the run profile")

            provenance = [item.model_dump(mode="json") for item in result.adapter_provenance]
            row = connection.execute(
                """
                INSERT INTO stage0_probe_records (
                    probe_record_id, stage0_run_id, job_id, task_id,
                    target_snapshot_id, probe_type, protocol_version,
                    raw_evidence_uri, raw_evidence_hash, summary,
                    adapter_provenance, synthetic, lease_id, resource_id,
                    fencing_token, cleanup_evidence
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (stage0_run_id, probe_type) DO NOTHING
                RETURNING *
                """,
                (
                    uuid4(),
                    result.stage0_run_id,
                    job_id,
                    task_id,
                    result.target_snapshot_id,
                    result.probe_type.value,
                    result.protocol_version,
                    result.raw_evidence_uri,
                    result.raw_evidence_hash,
                    Jsonb(result.summary),
                    Jsonb(provenance),
                    result.synthetic,
                    job.get("lease_id"),
                    job.get("resource_id"),
                    job.get("fencing_token"),
                    Jsonb(result.cleanup_evidence) if result.cleanup_evidence is not None else None,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM stage0_probe_records
                    WHERE stage0_run_id = %s AND probe_type = %s
                    """,
                    (result.stage0_run_id, result.probe_type.value),
                ).fetchone()
                assert row is not None
            expected_row = {
                "job_id": job_id,
                "task_id": task_id,
                "target_snapshot_id": result.target_snapshot_id,
                "protocol_version": result.protocol_version,
                "raw_evidence_uri": result.raw_evidence_uri,
                "raw_evidence_hash": result.raw_evidence_hash,
                "summary": result.summary,
                "adapter_provenance": provenance,
                "synthetic": result.synthetic,
                "lease_id": job.get("lease_id"),
                "resource_id": job.get("resource_id"),
                "fencing_token": job.get("fencing_token"),
                "cleanup_evidence": result.cleanup_evidence,
            }
            if any(row[name] != value for name, value in expected_row.items()):
                raise Conflict("Stage 0 probe type was replayed with different evidence")

            if created:
                connection.execute(
                    """
                    INSERT INTO task_events (task_id, event_type, details)
                    VALUES (%s, 'stage0_probe_recorded', %s)
                    """,
                    (
                        task_id,
                        Jsonb(
                            {
                                "stage0_run_id": str(result.stage0_run_id),
                                "job_id": str(job_id),
                                "probe_type": result.probe_type.value,
                                "synthetic": result.synthetic,
                            }
                        ),
                    ),
                )
            recorded = connection.execute(
                """
                SELECT count(*) AS count FROM stage0_probe_records
                WHERE stage0_run_id = %s
                """,
                (result.stage0_run_id,),
            ).fetchone()
            assert recorded is not None
            if int(recorded["count"]) == len(REQUIRED_STAGE0_PROBES):
                connection.execute(
                    """
                    UPDATE stage0_runs SET state = %s
                    WHERE stage0_run_id = %s AND state = %s
                    """,
                    (
                        Stage0RunState.READY.value,
                        result.stage0_run_id,
                        Stage0RunState.COLLECTING.value,
                    ),
                )
        return row

    def finalize_stage0_run(self, stage0_run_id: UUID) -> dict[str, Any]:
        initial = self._load_stage0_verification_input(stage0_run_id)
        run = initial["run"]
        if run["state"] == Stage0RunState.FINALIZED.value:
            assert run["report"] is not None
            return run["report"]
        self._require_stage0_ready_for_verification(initial)
        if self.stage0_finalizer is None:
            raise Conflict(
                "formal Stage 0 verifier is not configured; set the deployment evidence root"
            )
        context = initial["context"]
        references = initial["references"]
        try:
            verification = self.stage0_finalizer.verify(
                context,
                references,
                protocol_version=run["protocol_version"],
            )
        except (Stage0EvidenceError, Stage0ProtocolError, ValidationError, ValueError) as exc:
            code = getattr(exc, "code", "formal_evidence_invalid")
            raise Conflict(f"formal Stage 0 evidence verification failed [{code}]: {exc}") from exc

        evidence = verification.to_stage0_evidence(
            evidence_uri=(
                f"stage0://runs/{stage0_run_id}/verification/"
                f"{verification.input_digest.removeprefix('sha256:')}"
            )
        )
        decision = evaluate_stage0(evidence)
        accepted_target_risks = tuple(
            blocker.id for blocker in context.target.blockers if blocker.status == "accepted"
        )
        artifacts = self.stage0_finalizer.publish_report(
            context,
            verification,
            mode=decision.mode,
            reasons=tuple(decision.reasons),
            accepted_target_risks=accepted_target_risks,
        )

        with self.connection() as connection:
            run = connection.execute(
                "SELECT * FROM stage0_runs WHERE stage0_run_id = %s FOR UPDATE",
                (stage0_run_id,),
            ).fetchone()
            if run is None:
                raise NotFound(f"Stage 0 run not found: {stage0_run_id}")
            if run["state"] == Stage0RunState.FINALIZED.value:
                assert run["report"] is not None
                return run["report"]
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE",
                (run["task_id"],),
            ).fetchone()
            assert task is not None
            target_snapshot = connection.execute(
                """
                SELECT * FROM target_snapshots
                WHERE target_snapshot_id = %s
                """,
                (run["target_snapshot_id"],),
            ).fetchone()
            assert target_snapshot is not None
            records = connection.execute(
                """
                SELECT * FROM stage0_probe_records
                WHERE stage0_run_id = %s ORDER BY probe_type
                """,
                (stage0_run_id,),
            ).fetchall()
            current = self._project_stage0_verification_input(
                run,
                task,
                target_snapshot,
                records,
            )
            self._require_stage0_ready_for_verification(current)
            protocol = load_registered_stage0_protocol(run["protocol_version"])
            current_references = {
                reference.probe_type: reference for reference in current["references"]
            }
            current_digest = verification_input_digest(
                current["context"], current_references, protocol
            )
            if current_digest != verification.input_digest:
                raise Conflict("Stage 0 evidence changed during independent verification")
            if (
                verification.protocol_version != run["protocol_version"]
                or verification.protocol_hash != protocol.protocol_hash
            ):
                raise Conflict("Stage 0 verifier used a different registered protocol")
            if current["context"] != context:
                raise Conflict("Stage 0 verification context changed before commit")

            target_state = (
                TaskState.STOPPED_MEASUREMENT
                if decision.mode is ProjectMode.STOPPED_MEASUREMENT
                else TaskState.DEGRADED
                if decision.mode in {ProjectMode.DEGRADED_MANUAL_INTAKE, ProjectMode.CONFIG_ONLY}
                else TaskState.BASELINE_PENDING
            )
            transition_task(TaskState(task["state"]), target_state)
            report = {
                "task_id": str(task["task_id"]),
                "mode": decision.mode.value,
                "reasons": list(decision.reasons),
                "automatic_release_allowed": False,
                "evidence_authority": "formal",
                "protocol_version": verification.protocol_version,
                "protocol_hash": verification.protocol_hash,
                "input_digest": verification.input_digest,
                "machine_report_uri": artifacts.machine_report_uri,
                "machine_report_hash": artifacts.machine_report_hash,
                "markdown_report_uri": artifacts.markdown_report_uri,
                "markdown_report_hash": artifacts.markdown_report_hash,
                "accepted_target_risks": list(accepted_target_risks),
            }
            aggregate = {
                "source": "independently-verified-raw-evidence",
                "stage0_run_id": str(stage0_run_id),
                "target_snapshot_id": str(run["target_snapshot_id"]),
                "protocol_version": verification.protocol_version,
                "protocol_hash": verification.protocol_hash,
                "input_digest": verification.input_digest,
                "measurement": verification.measurement.value,
                "profiler": verification.profiler.value,
                "hot_patch": verification.hot_patch.value,
                "hardware_fingerprint": verification.hardware_fingerprint,
                "software_fingerprint": verification.software_fingerprint,
                "timer_resolution_ns": verification.timer_resolution_ns,
                "noise_sigma_ns": verification.noise_sigma_ns,
                "noise_cv": verification.noise_cv,
                "mde_ratio": verification.mde_ratio,
                "failure_codes": list(verification.failure_codes),
                "verification_reasons": list(verification.reasons),
                "statistics": verification.statistics,
                "input_evidence": list(verification.input_evidence),
                "verifier_provenance": verification.verifier_provenance.model_dump(mode="json"),
                "probe_record_ids": [str(row["probe_record_id"]) for row in records],
                "accepted_target_risks": list(accepted_target_risks),
                "reports": {
                    "machine": {
                        "uri": artifacts.machine_report_uri,
                        "sha256": artifacts.machine_report_hash,
                    },
                    "markdown": {
                        "uri": artifacts.markdown_report_uri,
                        "sha256": artifacts.markdown_report_hash,
                    },
                },
                "synthetic": False,
            }
            connection.execute(
                """
                INSERT INTO stage0_evidence (task_id, stage0_run_id, evidence, report)
                VALUES (%s, %s, %s, %s)
                """,
                (task["task_id"], stage0_run_id, Jsonb(aggregate), Jsonb(report)),
            )
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, project_mode = %s, stage0_authority = 'formal',
                    version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (target_state.value, decision.mode.value, task["task_id"]),
            )
            connection.execute(
                """
                UPDATE stage0_runs
                SET state = %s, report = %s, finalized_at = now()
                WHERE stage0_run_id = %s
                """,
                (Stage0RunState.FINALIZED.value, Jsonb(report), stage0_run_id),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'stage0_finalized', %s)
                """,
                (
                    task["task_id"],
                    Jsonb(
                        {
                            "stage0_run_id": str(stage0_run_id),
                            "mode": decision.mode.value,
                            "input_digest": verification.input_digest,
                            "accepted_target_risks": list(accepted_target_risks),
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        return report

    def _load_stage0_verification_input(self, stage0_run_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            run = connection.execute(
                "SELECT * FROM stage0_runs WHERE stage0_run_id = %s",
                (stage0_run_id,),
            ).fetchone()
            if run is None:
                raise NotFound(f"Stage 0 run not found: {stage0_run_id}")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s",
                (run["task_id"],),
            ).fetchone()
            assert task is not None
            target_snapshot = connection.execute(
                """
                SELECT * FROM target_snapshots
                WHERE target_snapshot_id = %s
                """,
                (run["target_snapshot_id"],),
            ).fetchone()
            assert target_snapshot is not None
            records = connection.execute(
                """
                SELECT * FROM stage0_probe_records
                WHERE stage0_run_id = %s ORDER BY probe_type
                """,
                (stage0_run_id,),
            ).fetchall()
        return self._project_stage0_verification_input(
            run,
            task,
            target_snapshot,
            records,
        )

    @staticmethod
    def _project_stage0_verification_input(
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        target_snapshot: Mapping[str, Any],
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Evidence mode is the outer authority boundary.  Reject Dry Run input
        # before applying Formal-only resource identity requirements so callers
        # receive the authoritative failure reason and cannot mistake a Dry Run
        # for malformed Formal evidence.
        if run.get("mode") != Stage0RunMode.FORMAL.value:
            raise Conflict("Dry Run evidence cannot be finalized as formal Stage 0")
        try:
            target = TargetSpec.model_validate(target_snapshot["specification"])
            expected_resource_id = PostgresRepository._stage0_expected_resource_id(
                target, records
            )
            context = Stage0VerificationContext(
                task_id=task["task_id"],
                stage0_run_id=run["stage0_run_id"],
                target_snapshot_id=run["target_snapshot_id"],
                target=target,
                target_fingerprint=target_snapshot["target_fingerprint"],
                workload_id=task["workload_id"],
                adapter_profile=run["adapter_profile"],
                expected_resource_id=expected_resource_id,
            )
            references = tuple(
                Stage0ProbeEvidenceReference(
                    probe_record_id=row["probe_record_id"],
                    probe_type=Stage0ProbeType(row["probe_type"]),
                    raw_evidence_uri=row["raw_evidence_uri"],
                    raw_evidence_hash=row["raw_evidence_hash"],
                    adapter_provenance=tuple(row["adapter_provenance"]),
                    synthetic=row["synthetic"],
                    lease_id=row["lease_id"],
                    resource_id=row["resource_id"],
                    fencing_token=row["fencing_token"],
                    cleanup_evidence=row["cleanup_evidence"],
                )
                for row in records
            )
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            raise Conflict(f"Stage 0 persisted evidence references are invalid: {exc}") from exc
        return {
            "run": run,
            "task": task,
            "target_snapshot": target_snapshot,
            "records": records,
            "context": context,
            "references": references,
        }

    @staticmethod
    def _stage0_expected_resource_id(
        target: TargetSpec,
        records: list[dict[str, Any]],
    ) -> str:
        """Resolve the run's accelerator identity without discarding Target scope.

        ``hcu-N`` is retained for existing single-host deployments.  New deployments
        may use ``<target_id>:hcu:N`` so the same physical index on two Targets does
        not collapse to one scheduler resource.  The selected spelling must be the
        one actually persisted by every control-plane lease record.
        """

        device_index = target.execution_host.accelerator.device_index
        allowed = {
            f"hcu-{device_index}",
            f"{target.target_id}:hcu:{device_index}",
        }
        recorded = {row.get("resource_id") for row in records}
        if len(recorded) != 1 or None in recorded:
            raise Conflict(
                "Stage 0 persisted probe records do not bind one accelerator resource"
            )
        resource_id = recorded.pop()
        if not isinstance(resource_id, str) or resource_id not in allowed:
            raise Conflict(
                "Stage 0 accelerator resource does not match the frozen Target"
            )
        return resource_id

    @staticmethod
    def _require_stage0_ready_for_verification(value: Mapping[str, Any]) -> None:
        run = value["run"]
        task = value["task"]
        references = value["references"]
        if run["mode"] != Stage0RunMode.FORMAL.value:
            raise Conflict("Dry Run evidence cannot be finalized as formal Stage 0")
        if run["state"] != Stage0RunState.READY.value:
            raise Conflict("Stage 0 probe barrier is not ready")
        if task["state"] != TaskState.STAGE0_PENDING.value:
            raise Conflict("formal Stage 0 requires a stage0_pending task")
        if len(references) != len(REQUIRED_STAGE0_PROBES):
            raise Conflict("Stage 0 seven-probe barrier is incomplete")

    def freeze_baseline(self, task_id: UUID, request: BaselineCreate) -> dict[str, Any]:
        epoch_id = uuid4()
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            existing = connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s", (task_id,)
            ).fetchone()
            if existing is not None:
                same = all(
                    existing[name] == getattr(request, name)
                    for name in (
                        "hardware_fingerprint",
                        "software_fingerprint",
                        "workload_id",
                        "configuration_hash",
                    )
                )
                if not same:
                    raise Conflict("task already has a different frozen baseline")
                return existing
            current = TaskState(task["state"])
            if current is TaskState.DEGRADED:
                transition_task(current, TaskState.BASELINE_PENDING)
                current = TaskState.BASELINE_PENDING
            if current is not TaskState.BASELINE_PENDING:
                raise Conflict("baseline requires a passed or degraded Stage 0")
            transition_task(current, TaskState.PROFILING)
            row = connection.execute(
                """
                INSERT INTO baseline_epochs (
                    baseline_epoch_id, task_id, hardware_fingerprint,
                    software_fingerprint, workload_id, configuration_hash
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    epoch_id,
                    task_id,
                    request.hardware_fingerprint,
                    request.software_fingerprint,
                    request.workload_id,
                    request.configuration_hash,
                ),
            ).fetchone()
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.PROFILING.value, task_id),
            )
        assert row is not None
        return row

    def get_baseline(self, task_id: UUID) -> dict[str, Any] | None:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s", (task_id,)
            ).fetchone()

    def get_target_snapshot(self, task_id: UUID) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT snapshot.*
                FROM tasks AS task
                JOIN target_snapshots AS snapshot
                  ON snapshot.target_snapshot_id = task.target_snapshot_id
                WHERE task.task_id = %s
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"target snapshot not found for task: {task_id}")
        return row

    def ensure_framework_baseline(
        self,
        task_id: UUID,
        target: TargetSpec,
        source: SourceSnapshot,
    ) -> dict[str, Any]:
        epoch_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:framework-baseline:v1")
        hardware_payload = {
            "host": target.execution_host.name,
            "accelerator": target.execution_host.accelerator.model,
            "architecture": target.execution_host.accelerator.architecture,
            "device": target.execution_host.accelerator.device_index,
        }
        software_payload = {
            "image": target.inference_image.registry_digest,
            "source": source.source_hash,
            "dtk": target.inference_image.dtk_version,
            "sglang": target.inference_image.sglang_package_version,
        }

        def digest(value: dict[str, Any]) -> str:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            return "sha256:" + hashlib.sha256(encoded).hexdigest()

        expected = {
            "hardware_fingerprint": digest(hardware_payload),
            "software_fingerprint": digest(software_payload),
            "workload_id": f"framework-smoke:{target.target_id}",
            "configuration_hash": self._target_fingerprint(target),
            "baseline_kind": WorkflowType.FRAMEWORK_SMOKE.value,
        }
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO baseline_epochs (
                    baseline_epoch_id, task_id, hardware_fingerprint,
                    software_fingerprint, workload_id, configuration_hash,
                    baseline_kind
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id) DO NOTHING
                RETURNING *
                """,
                (
                    epoch_id,
                    task_id,
                    expected["hardware_fingerprint"],
                    expected["software_fingerprint"],
                    expected["workload_id"],
                    expected["configuration_hash"],
                    expected["baseline_kind"],
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM baseline_epochs WHERE task_id = %s", (task_id,)
                ).fetchone()
        assert row is not None
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("task already has a different immutable framework baseline")
        return row

    def record_source_snapshot(
        self,
        task_id: UUID,
        source: SourceSnapshot,
        provenance: list[dict[str, Any]],
        synthetic: bool,
        idempotency_key: str,
        candidate_id: UUID | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO source_snapshots (
                    snapshot_id, task_id, candidate_id, kind, repository,
                    commit, tree_hash, source_hash, worktree_uri, clean,
                    parent_snapshot_id, idempotency_key, adapter_provenance,
                    synthetic, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    source.snapshot_id,
                    task_id,
                    candidate_id,
                    source.kind,
                    source.repository,
                    source.commit,
                    source.tree_hash,
                    source.source_hash,
                    source.worktree_uri,
                    source.clean,
                    source.parent_snapshot_id,
                    idempotency_key,
                    Jsonb(provenance),
                    synthetic,
                    source.created_at,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "snapshot_id": source.snapshot_id,
            "task_id": task_id,
            "candidate_id": candidate_id,
            "source_hash": source.source_hash,
            "adapter_provenance": provenance,
            "synthetic": synthetic,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("source snapshot idempotency_key was reused with different inputs")
        return row

    def create_noop_candidate(
        self,
        task_id: UUID,
        baseline_epoch_id: UUID,
        source: SourceSnapshot,
    ) -> dict[str, Any]:
        candidate_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:framework-noop:v1")
        round_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:framework-round:v1")
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO candidates (
                    candidate_id, task_id, round_id, baseline_epoch_id,
                    source_hash, variant, state, ordinal, metadata
                ) VALUES (%s, %s, %s, %s, %s, 'framework-noop', %s, 0, %s)
                ON CONFLICT (candidate_id) DO UPDATE
                SET candidate_id = EXCLUDED.candidate_id
                RETURNING *
                """,
                (
                    candidate_id,
                    task_id,
                    round_id,
                    baseline_epoch_id,
                    source.source_hash,
                    CandidateState.PROPOSED.value,
                    Jsonb(
                        {
                            "workflow_type": WorkflowType.FRAMEWORK_SMOKE.value,
                            "baseline_source_snapshot_id": str(source.snapshot_id),
                            "no_op": True,
                        }
                    ),
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "task_id": task_id,
            "baseline_epoch_id": baseline_epoch_id,
            "source_hash": source.source_hash,
            "variant": "framework-noop",
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("deterministic no-op candidate conflicts with persisted state")
        return row

    def register_worker(self, request: WorkerRegister) -> dict[str, Any]:
        declared_profile = request.adapter_profile
        capability_profile = request.capabilities.get("adapter_profile")
        if declared_profile is not None and capability_profile not in {None, declared_profile}:
            raise Conflict("worker adapter_profile conflicts with capabilities metadata")
        adapter_profile = declared_profile or capability_profile
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO workers (
                    worker_id, worker_type, contract_version, adapter_profile, capabilities
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (worker_id) DO UPDATE
                SET worker_type = EXCLUDED.worker_type,
                    contract_version = EXCLUDED.contract_version,
                    adapter_profile = EXCLUDED.adapter_profile,
                    capabilities = EXCLUDED.capabilities,
                    state = 'online',
                    last_heartbeat_at = now()
                RETURNING *
                """,
                (
                    request.worker_id,
                    request.worker_type.value,
                    request.contract_version,
                    adapter_profile,
                    Jsonb(request.capabilities),
                ),
            ).fetchone()
            if request.worker_type is WorkerType.GPU:
                resource_id = str(request.capabilities.get("resource_id", "fake-hcu-0"))
                connection.execute(
                    """
                    INSERT INTO resources (resource_id)
                    VALUES (%s)
                    ON CONFLICT (resource_id) DO NOTHING
                    """,
                    (resource_id,),
                )
        assert row is not None
        return row

    def enqueue_job(self, request: JobCreate) -> dict[str, Any]:
        job_id = uuid4()
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type, adapter_profile, lease_scope,
                    payload, idempotency_key, priority, max_attempts
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    job_id,
                    request.task_id,
                    request.job_type.value,
                    request.accepted_worker_type.value,
                    request.adapter_profile,
                    request.lease_scope.value,
                    Jsonb(request.payload),
                    request.idempotency_key,
                    request.priority,
                    request.max_attempts,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "task_id": request.task_id,
            "job_type": request.job_type.value,
            "accepted_worker_type": request.accepted_worker_type.value,
            "adapter_profile": request.adapter_profile,
            "lease_scope": request.lease_scope.value,
            "payload": request.payload,
            "priority": request.priority,
            "max_attempts": request.max_attempts,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("idempotency_key was already used with a different job")
        return row

    def claim_job(self, worker_id: str) -> dict[str, Any] | None:
        claim_token = uuid4()
        with self.connection() as connection:
            worker = connection.execute(
                "SELECT * FROM workers WHERE worker_id = %s FOR UPDATE", (worker_id,)
            ).fetchone()
            if worker is None:
                raise NotFound(f"worker not registered: {worker_id}")
            worker_type = WorkerType(worker["worker_type"])
            job = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'queued'
                  AND available_at <= now()
                  AND accepted_worker_type = %s
                  AND (adapter_profile IS NULL OR adapter_profile = %s)
                ORDER BY priority DESC, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (worker_type.value, worker["adapter_profile"]),
            ).fetchone()
            if job is None:
                connection.execute(
                    "UPDATE workers SET last_heartbeat_at = now() WHERE worker_id = %s",
                    (worker_id,),
                )
                return None

            lease_id: UUID | None = None
            resource_id: str | None = None
            fencing_token: int | None = None
            if worker_type is WorkerType.GPU and job["lease_scope"] in {
                LeaseScope.SHARED.value,
                LeaseScope.EXCLUSIVE.value,
            }:
                requested_resource = worker["capabilities"].get("resource_id")
                if requested_resource:
                    resource = connection.execute(
                        """
                        SELECT * FROM resources
                        WHERE resource_id = %s AND state = 'available'
                        FOR UPDATE SKIP LOCKED
                        """,
                        (requested_resource,),
                    ).fetchone()
                else:
                    resource = connection.execute(
                        """
                        SELECT * FROM resources WHERE state = 'available'
                        ORDER BY resource_id FOR UPDATE SKIP LOCKED LIMIT 1
                        """
                    ).fetchone()
                if resource is None:
                    return None
                lease_id = uuid4()
                resource_id = resource["resource_id"]
                fencing_token = int(resource["fencing_token"]) + 1
                connection.execute(
                    """
                    UPDATE resources
                    SET state = 'active', owner_job_id = %s, lease_id = %s,
                        fencing_token = %s, expires_at = now() + interval '90 seconds',
                        updated_at = now()
                    WHERE resource_id = %s
                    """,
                    (job["job_id"], lease_id, fencing_token, resource_id),
                )
            claimed = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', claimed_by = %s, claim_token = %s,
                    claimed_at = now(), heartbeat_at = now(), attempts = attempts + 1,
                    lease_id = %s, resource_id = %s, fencing_token = %s,
                    updated_at = now()
                WHERE job_id = %s AND state = 'queued'
                RETURNING *
                """,
                (
                    worker_id,
                    claim_token,
                    lease_id,
                    resource_id,
                    fencing_token,
                    job["job_id"],
                ),
            ).fetchone()
            connection.execute(
                "UPDATE workers SET last_heartbeat_at = now() WHERE worker_id = %s",
                (worker_id,),
            )
            connection.execute(
                """
                INSERT INTO job_events (job_id, event_type, details)
                VALUES (%s, 'claimed', %s)
                """,
                (job["job_id"], Jsonb({"worker_id": worker_id})),
            )
        return claimed

    def heartbeat_job(
        self,
        worker_id: str,
        job_id: UUID,
        claim_token: UUID,
        fencing_token: int | None,
    ) -> None:
        with self.connection() as connection:
            self._assert_job_owner(connection, job_id, claim_token, fencing_token)
            updated = connection.execute(
                """
                UPDATE jobs SET heartbeat_at = now(), updated_at = now()
                WHERE job_id = %s AND claimed_by = %s AND state = 'running'
                """,
                (job_id, worker_id),
            )
            if updated.rowcount != 1:
                raise StaleClaimToken("worker no longer owns this running job")
            connection.execute(
                "UPDATE workers SET last_heartbeat_at = now() WHERE worker_id = %s",
                (worker_id,),
            )
            if fencing_token is not None:
                connection.execute(
                    """
                    UPDATE resources
                    SET expires_at = now() + interval '90 seconds', updated_at = now()
                    WHERE owner_job_id = %s AND fencing_token = %s
                    """,
                    (job_id, fencing_token),
                )

    def assert_live_job_lease(
        self, worker_id: str, job_id: UUID, claim_token: UUID,
        fencing_token: int | None,
    ) -> None:
        """Check the original locked job/resource rows without renewing a lease."""
        with self.connection() as connection:
            job = self._assert_job_owner(connection, job_id, claim_token, fencing_token)
            if job["claimed_by"] != worker_id:
                raise StaleClaimToken("worker no longer owns this running job")
            if job["resource_id"] is None or job["lease_id"] is None:
                raise StaleFencingToken("live resource lease required")
            resource = connection.execute(
                """
                SELECT *, expires_at > clock_timestamp() AS live_now
                FROM resources WHERE resource_id = %s FOR UPDATE
                """, (job["resource_id"],),
            ).fetchone()
            if (resource is None or resource["state"] != "active"
                    or resource["live_now"] is not True
                    or resource["owner_job_id"] != job_id
                    or resource["lease_id"] != job["lease_id"]
                    or resource["fencing_token"] != fencing_token):
                raise StaleFencingToken("resource lease is inactive, expired or changed")

    def _assert_job_owner(
        self,
        connection: Connection[dict[str, Any]],
        job_id: UUID,
        claim_token: UUID,
        fencing_token: int | None,
    ) -> dict[str, Any]:
        job = connection.execute(
            "SELECT * FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,)
        ).fetchone()
        if job is None:
            raise NotFound(f"job not found: {job_id}")
        if job["state"] != JobState.RUNNING.value or job["claim_token"] != claim_token:
            raise StaleClaimToken("claim token is stale or job is not running")
        if job["resource_id"] is not None:
            resource = connection.execute(
                "SELECT * FROM resources WHERE resource_id = %s FOR UPDATE",
                (job["resource_id"],),
            ).fetchone()
            if (
                resource is None
                or resource["owner_job_id"] != job_id
                or resource["fencing_token"] != fencing_token
            ):
                raise StaleFencingToken("GPU lease fencing token is stale")
        return job

    def _release_resource(
        self,
        connection: Connection[dict[str, Any]],
        job: dict[str, Any],
        reason: str,
        cleanup_evidence: dict[str, Any] | None = None,
    ) -> None:
        resource_id = job["resource_id"]
        if resource_id is None:
            return
        evidence = {
            "reason": reason,
            **(cleanup_evidence or {}),
            "healthy": _cleanup_is_healthy(cleanup_evidence),
        }
        fenced = connection.execute(
            """
            UPDATE resources SET state = 'fencing', updated_at = now()
            WHERE resource_id = %s AND owner_job_id = %s AND fencing_token = %s
            """,
            (resource_id, job["job_id"], job["fencing_token"]),
        )
        if fenced.rowcount != 1:
            return
        connection.execute(
            """
            UPDATE resources SET state = 'health_check', cleanup_evidence = %s,
                updated_at = now()
            WHERE resource_id = %s
            """,
            (Jsonb(evidence), resource_id),
        )
        connection.execute(
            """
            UPDATE resources SET state = %s, owner_job_id = NULL,
                lease_id = NULL, expires_at = NULL, updated_at = now()
            WHERE resource_id = %s AND fencing_token = %s
            """,
            (
                "available" if evidence["healthy"] else "quarantined",
                resource_id,
                job["fencing_token"],
            ),
        )

    def complete_job(
        self,
        job_id: UUID,
        claim_token: UUID,
        fencing_token: int | None,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if existing is None:
                raise NotFound(f"job not found: {job_id}")
            if existing["state"] == JobState.SUCCEEDED.value:
                if existing["claim_token"] != claim_token:
                    raise StaleClaimToken("completion replay used a stale claim token")
                if existing["fencing_token"] != fencing_token:
                    raise StaleFencingToken("completion replay used a stale fencing token")
                if existing["result"] != result:
                    raise Conflict("completed job was replayed with a different result")
                return existing
            job = self._assert_job_owner(connection, job_id, claim_token, fencing_token)
            row = connection.execute(
                """
                UPDATE jobs SET state = 'succeeded', result = %s, finished_at = now(),
                    updated_at = now()
                WHERE job_id = %s
                RETURNING *
                """,
                (Jsonb(result), job_id),
            ).fetchone()
            cleanup_evidence = result.get("cleanup_evidence")
            if not isinstance(cleanup_evidence, dict):
                cleanup_evidence = None
            self._release_resource(
                connection,
                job,
                "job_completed",
                cleanup_evidence,
            )
            connection.execute(
                "INSERT INTO job_events (job_id, event_type) VALUES (%s, 'succeeded')",
                (job_id,),
            )
        assert row is not None
        return row

    def fail_job(
        self,
        job_id: UUID,
        claim_token: UUID,
        fencing_token: int | None,
        error: dict[str, Any],
        retryable: bool,
        cleanup_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            job_reference = connection.execute(
                "SELECT task_id FROM jobs WHERE job_id = %s", (job_id,)
            ).fetchone()
            if job_reference is None:
                raise NotFound(f"job not found: {job_id}")
            connection.execute(
                "SELECT task_id FROM tasks WHERE task_id = %s FOR UPDATE",
                (job_reference["task_id"],),
            ).fetchone()
            job = self._assert_job_owner(connection, job_id, claim_token, fencing_token)
            retry = retryable and job["attempts"] < job["max_attempts"]
            state = JobState.QUEUED.value if retry else JobState.FAILED.value
            row = connection.execute(
                """
                UPDATE jobs
                SET state = %s, last_error = %s,
                    available_at = CASE
                        WHEN %s THEN now() + interval '1 second' ELSE available_at
                    END,
                    claimed_by = NULL, claim_token = NULL, claimed_at = NULL,
                    heartbeat_at = NULL, lease_id = NULL, resource_id = NULL,
                    fencing_token = NULL,
                    finished_at = CASE WHEN %s THEN NULL ELSE now() END, updated_at = now()
                WHERE job_id = %s
                RETURNING *
                """,
                (state, Jsonb(error), retry, retry, job_id),
            ).fetchone()
            self._release_resource(
                connection,
                job,
                "job_failed",
                cleanup_evidence,
            )
            connection.execute(
                """
                INSERT INTO job_events (job_id, event_type, details)
                VALUES (%s, %s, %s)
                """,
                (job_id, "requeued" if retry else "failed", Jsonb(error)),
            )
            if not retry:
                self._reject_framework_task_after_job_failure(
                    connection,
                    job,
                    error,
                )
                self._fail_stage0_run_after_job_failure(connection, job, error)
                self._reject_manual_candidate_task_after_job_failure(
                    connection, job, error
                )
        assert row is not None
        return row

    def _fail_stage0_run_after_job_failure(
        self,
        connection: Connection[dict[str, Any]],
        job: dict[str, Any],
        error: dict[str, Any],
    ) -> None:
        if job["job_type"] != JobType.STAGE0_PROBE.value:
            return
        run_id = job["payload"].get("stage0_run_id")
        if run_id is None:
            return
        connection.execute(
            """
            UPDATE stage0_runs SET state = %s
            WHERE stage0_run_id = %s AND state IN ('collecting', 'ready')
            """,
            (Stage0RunState.FAILED.value, run_id),
        )
        connection.execute(
            """
            INSERT INTO task_events (task_id, event_type, details)
            VALUES (%s, 'stage0_probe_job_failed', %s)
            """,
            (
                job["task_id"],
                Jsonb(
                    {
                        "stage0_run_id": str(run_id),
                        "job_id": str(job["job_id"]),
                        "probe_type": job["payload"].get("probe_type"),
                        "error": error,
                    }
                ),
            ),
        )

    def _reject_framework_task_after_job_failure(
        self,
        connection: Connection[dict[str, Any]],
        job: dict[str, Any],
        error: dict[str, Any],
    ) -> None:
        """Atomically converge a terminal Framework Smoke job failure."""

        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE",
            (job["task_id"],),
        ).fetchone()
        if task is None:
            raise NotFound(f"task not found: {job['task_id']}")
        if task["workflow_type"] != WorkflowType.FRAMEWORK_SMOKE.value:
            return

        current_task_state = TaskState(task["state"])
        if current_task_state not in {
            TaskState.REJECTED,
            TaskState.CANCELLED,
            TaskState.COMPLETED,
        }:
            transition_task(current_task_state, TaskState.REJECTED)
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.REJECTED.value, job["task_id"]),
            )

        candidate_id: UUID | None = None
        candidate_target: CandidateState | None = None
        if job["job_type"] == JobType.NOOP_BUILD.value:
            candidate_target = CandidateState.BUILD_FAILED
        elif job["job_type"] == JobType.FRAMEWORK_SMOKE.value:
            candidate_target = CandidateState.REJECTED
        if candidate_target is not None:
            raw_candidate_id = job["payload"].get("candidate_id")
            if raw_candidate_id is None:
                raise Conflict(f"{job['job_type']} job is missing its candidate_id binding")
            candidate_id = UUID(str(raw_candidate_id))
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = %s FOR UPDATE",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise NotFound(f"candidate not found: {candidate_id}")
            current_candidate_state = CandidateState(candidate["state"])
            if current_candidate_state not in {
                candidate_target,
                CandidateState.BUILD_FAILED,
                CandidateState.REJECTED,
            }:
                transition_candidate(current_candidate_state, candidate_target)
                connection.execute(
                    """
                    UPDATE candidates SET state = %s, updated_at = now()
                    WHERE candidate_id = %s
                    """,
                    (candidate_target.value, candidate_id),
                )

        connection.execute(
            """
            INSERT INTO task_events (task_id, event_type, details)
            VALUES (%s, 'framework_smoke_job_failed', %s)
            """,
            (
                job["task_id"],
                Jsonb(
                    {
                        "job_id": str(job["job_id"]),
                        "job_type": job["job_type"],
                        "candidate_id": (str(candidate_id) if candidate_id is not None else None),
                        "attempts": job["attempts"],
                        "max_attempts": job["max_attempts"],
                        "error": error,
                    }
                ),
            ),
        )

    def _reject_manual_candidate_task_after_job_failure(
        self,
        connection: Connection[dict[str, Any]],
        job: dict[str, Any],
        error: dict[str, Any],
    ) -> None:
        manual_job_types = {
            JobType.MANUAL_BUILD.value,
            JobType.MANUAL_CORRECTNESS.value,
            JobType.MANUAL_PERFORMANCE.value,
            JobType.MANUAL_ADJUDICATE.value,
        }
        if job["job_type"] not in manual_job_types:
            return
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE",
            (job["task_id"],),
        ).fetchone()
        if task is None:
            raise NotFound(f"task not found: {job['task_id']}")
        if task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value:
            return
        current_task = TaskState(task["state"])
        if current_task not in {
            TaskState.REJECTED,
            TaskState.CANCELLED,
            TaskState.COMPLETED,
        }:
            transition_task(current_task, TaskState.REJECTED)
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.REJECTED.value, job["task_id"]),
            )
        raw_candidate_id = job["payload"].get("candidate_id")
        if raw_candidate_id is None:
            raise Conflict(f"{job['job_type']} job is missing its candidate_id binding")
        candidate_id = UUID(str(raw_candidate_id))
        candidate = connection.execute(
            "SELECT * FROM candidates WHERE candidate_id = %s FOR UPDATE",
            (candidate_id,),
        ).fetchone()
        if candidate is None:
            raise NotFound(f"candidate not found: {candidate_id}")
        target = (
            CandidateState.BUILD_FAILED
            if job["job_type"] == JobType.MANUAL_BUILD.value
            else CandidateState.REJECTED
        )
        current_candidate = CandidateState(candidate["state"])
        if current_candidate not in {
            target,
            CandidateState.BUILD_FAILED,
            CandidateState.REJECTED,
            CandidateState.ACCEPTED,
        }:
            transition_candidate(current_candidate, target)
            connection.execute(
                """
                UPDATE candidates SET state = %s, updated_at = now()
                WHERE candidate_id = %s
                """,
                (target.value, candidate_id),
            )
        connection.execute(
            """
            INSERT INTO task_events (task_id, event_type, details)
            VALUES (%s, 'manual_candidate_job_failed', %s)
            """,
            (
                job["task_id"],
                Jsonb(
                    {
                        "job_id": str(job["job_id"]),
                        "job_type": job["job_type"],
                        "candidate_id": str(candidate_id),
                        "attempts": job["attempts"],
                        "max_attempts": job["max_attempts"],
                        "error": error,
                    }
                ),
            ),
        )

    def recover_stale_jobs(self, stale_after_seconds: int = 120) -> list[UUID]:
        recovered: list[UUID] = []
        with self.connection() as connection:
            # Keep the same task -> job lock order as fail_job(). This prevents a
            # worker-loss recovery from deadlocking with a late worker failure.
            connection.execute(
                """
                SELECT task_id FROM tasks
                WHERE task_id IN (
                    SELECT task_id FROM jobs
                    WHERE state = 'running'
                      AND heartbeat_at <= now() - make_interval(secs => %s)
                )
                ORDER BY task_id
                FOR UPDATE
                """,
                (stale_after_seconds,),
            ).fetchall()
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'running'
                  AND heartbeat_at <= now() - make_interval(secs => %s)
                ORDER BY heartbeat_at
                FOR UPDATE SKIP LOCKED
                """,
                (stale_after_seconds,),
            ).fetchall()
            for job in rows:
                self._release_resource(connection, job, "worker_heartbeat_expired")
                retry = job["attempts"] < job["max_attempts"]
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = %s, claimed_by = NULL, claim_token = NULL,
                        claimed_at = NULL, heartbeat_at = NULL, lease_id = NULL,
                        resource_id = NULL, fencing_token = NULL, available_at = now(),
                        last_error = %s, finished_at = CASE WHEN %s THEN NULL ELSE now() END,
                        updated_at = now()
                    WHERE job_id = %s
                    """,
                    (
                        JobState.QUEUED.value if retry else JobState.FAILED.value,
                        Jsonb({"code": "worker_lost", "message": "heartbeat expired"}),
                        retry,
                        job["job_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO job_events (job_id, event_type, details)
                    VALUES (%s, 'fenced_after_worker_loss', %s)
                    """,
                    (job["job_id"], Jsonb({"old_fencing_token": job["fencing_token"]})),
                )
                if not retry:
                    error = {"code": "worker_lost", "message": "heartbeat expired"}
                    self._reject_framework_task_after_job_failure(connection, job, error)
                    self._fail_stage0_run_after_job_failure(connection, job, error)
                    self._reject_manual_candidate_task_after_job_failure(
                        connection, job, error
                    )
                recovered.append(job["job_id"])
        return recovered

    def record_hotspot(
        self, task_id: UUID, baseline_epoch_id: UUID, result: dict[str, Any]
    ) -> dict[str, Any]:
        hotspot_id = uuid4()
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO hotspots (
                    hotspot_id, task_id, baseline_epoch_id, symbol, share_ratio,
                    opportunity_score, patchability, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, symbol) DO UPDATE
                SET evidence = EXCLUDED.evidence
                RETURNING *
                """,
                (
                    hotspot_id,
                    task_id,
                    baseline_epoch_id,
                    result["symbol"],
                    result["share_ratio"],
                    result["opportunity_score"],
                    result["patchability"],
                    Jsonb(result),
                ),
            ).fetchone()
        assert row is not None
        return row

    def create_manual_hotspot_intake(
        self,
        task_id: UUID,
        request: ManualHotspotIntakeCreate,
    ) -> dict[str, Any]:
        """Persist one immutable, provenance-bearing manual Profiler interpretation."""

        payload = request.model_dump(mode="json", exclude={"idempotency_key"})
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        intake_hash = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        hotspot_id = uuid5(
            NAMESPACE_URL, f"hcuopt:m1-hotspot:{request.idempotency_key}"
        )
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value:
                raise Conflict("Hotspot Intake requires an M1 Manual Candidate task")
            existing = connection.execute(
                """
                SELECT * FROM hotspots
                WHERE idempotency_key = %s OR (task_id = %s AND symbol = %s)
                FOR UPDATE
                """,
                (request.idempotency_key, task_id, request.symbol),
            ).fetchone()
            if existing is not None:
                if (
                    existing["hotspot_id"] != hotspot_id
                    or existing["task_id"] != task_id
                    or existing["intake_hash"] != intake_hash
                ):
                    raise Conflict(
                        "Hotspot Intake identity or symbol belongs to different evidence"
                    )
                return self._manual_hotspot_view(existing)

            if task["state"] != TaskState.MANUAL_CANDIDATE_PENDING.value:
                raise Conflict("Hotspot Intake must finish before Candidate registration")
            baseline = connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s FOR SHARE",
                (task_id,),
            ).fetchone()
            if baseline is None or baseline["baseline_epoch_id"] != request.baseline_epoch_id:
                raise Conflict("Hotspot Intake must bind the task's immutable Baseline Epoch")

            row = connection.execute(
                """
                INSERT INTO hotspots (
                    hotspot_id, task_id, baseline_epoch_id, symbol, share_ratio,
                    opportunity_score, patchability, evidence, candidate_kind,
                    actor, intake_hash, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    hotspot_id,
                    task_id,
                    request.baseline_epoch_id,
                    request.symbol,
                    request.share_ratio,
                    request.opportunity_score,
                    request.patchability,
                    Jsonb(payload),
                    request.candidate_kind.value,
                    request.actor,
                    intake_hash,
                    request.idempotency_key,
                ),
            ).fetchone()
            assert row is not None
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'manual_hotspot_intake_recorded', %s)
                """,
                (
                    task_id,
                    Jsonb(
                        {
                            "hotspot_id": str(hotspot_id),
                            "baseline_epoch_id": str(request.baseline_epoch_id),
                            "symbol": request.symbol,
                            "replacement_point": request.replacement_point,
                            "candidate_kind": request.candidate_kind.value,
                            "profiler_raw_output_hash": request.profiler_raw_output_hash,
                            "intake_hash": intake_hash,
                        }
                    ),
                ),
            )
        return self._manual_hotspot_view(row)

    def list_manual_hotspot_intakes(self, task_id: UUID) -> list[dict[str, Any]]:
        task = self.get_task(task_id)
        if task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value:
            raise Conflict("task is not an M1 Manual Candidate workflow")
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM hotspots WHERE task_id = %s ORDER BY created_at, hotspot_id",
                (task_id,),
            ).fetchall()
        return [self._manual_hotspot_view(row) for row in rows]

    @staticmethod
    def _manual_hotspot_view(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **dict(row["evidence"]),
            "hotspot_id": row["hotspot_id"],
            "task_id": row["task_id"],
            "idempotency_key": row["idempotency_key"],
            "intake_hash": row["intake_hash"],
            "created_at": row["created_at"],
        }

    def create_manual_candidate(
        self,
        task_id: UUID,
        request: ManualCandidateCreate,
    ) -> dict[str, Any]:
        """Register the single M1 Candidate and enqueue its durable build Job."""

        candidate_id = uuid5(
            NAMESPACE_URL, f"hcuopt:m1-candidate:{request.idempotency_key}"
        )
        round_id = uuid5(NAMESPACE_URL, f"hcuopt:{task_id}:m1-single-round:v1")
        job_id = uuid5(NAMESPACE_URL, f"hcuopt:{candidate_id}:m1-build:v1")
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value:
                raise Conflict("task is not an M1 Manual Candidate workflow")
            existing = connection.execute(
                "SELECT * FROM candidates WHERE task_id = %s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if existing is not None:
                expected = {
                    "candidate_id": candidate_id,
                    "baseline_epoch_id": request.baseline_epoch_id,
                    "hotspot_id": request.hotspot_id,
                    "source_hash": request.source_hash,
                    "optimization_intent": request.optimization_intent,
                    "replacement_point": request.replacement_point,
                    "track": request.track.value,
                    "release_mode": request.release_mode.value,
                    "candidate_kind": request.candidate_kind.value,
                    "parent_candidate_id": request.parent_candidate_id,
                    "idempotency_key": request.idempotency_key,
                }
                if any(existing[name] != value for name, value in expected.items()):
                    raise Conflict("M1 task already owns a different single Candidate")
                return existing
            if task["state"] != TaskState.MANUAL_CANDIDATE_PENDING.value:
                raise Conflict("M1 Candidate intake requires manual_candidate_pending")
            if request.parent_candidate_id is not None:
                raise Conflict("M1 single-Candidate intake does not support Candidate branching")

            baseline = connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s FOR SHARE",
                (task_id,),
            ).fetchone()
            if baseline is None or baseline["baseline_kind"] != WorkflowType.MANUAL_CANDIDATE.value:
                raise Conflict("M1 Candidate requires an immutable Manual Candidate baseline")
            if request.baseline_epoch_id != baseline["baseline_epoch_id"]:
                raise Conflict("M1 Candidate intake must bind the task's Baseline Epoch")
            source = connection.execute(
                "SELECT * FROM source_snapshots WHERE snapshot_id = %s FOR SHARE",
                (baseline["source_snapshot_id"],),
            ).fetchone()
            if source is None:
                raise NotFound("M1 Baseline SourceSnapshot no longer exists")
            target = connection.execute(
                "SELECT * FROM target_snapshots WHERE target_snapshot_id = %s FOR SHARE",
                (baseline["target_snapshot_id"],),
            ).fetchone()
            if target is None:
                raise NotFound("M1 Target Snapshot no longer exists")
            stage0_authority = connection.execute(
                """
                SELECT
                    evidence.evidence,
                    evidence.report,
                    stage0_task.task_id AS stage0_task_id,
                    stage0_task.workload_id AS stage0_workload_id,
                    run.adapter_profile AS stage0_adapter_profile
                FROM stage0_evidence AS evidence
                JOIN stage0_runs AS run
                  ON run.stage0_run_id = evidence.stage0_run_id
                JOIN tasks AS stage0_task
                  ON stage0_task.task_id = run.task_id
                WHERE evidence.stage0_run_id = %s
                FOR SHARE
                """,
                (task["stage0_run_id"],),
            ).fetchone()
            if stage0_authority is None:
                raise Conflict("M1 Candidate cannot find its Formal Stage 0 authority")
            formal_evidence = stage0_authority["evidence"]
            formal_report = stage0_authority["report"]
            stage0_report = {
                "uri": formal_report.get("machine_report_uri"),
                "sha256": formal_report.get("machine_report_hash"),
                "input_digest": formal_evidence.get("input_digest"),
                "protocol_version": formal_evidence.get("protocol_version"),
                "protocol_hash": formal_evidence.get("protocol_hash"),
                "stage0_task_id": str(stage0_authority["stage0_task_id"]),
                "stage0_workload_id": stage0_authority["stage0_workload_id"],
                "stage0_adapter_profile": stage0_authority["stage0_adapter_profile"],
            }
            if (
                any(not isinstance(value, str) for value in stage0_report.values())
                or stage0_report["protocol_hash"] != baseline["stage0_protocol_hash"]
                or stage0_report["protocol_version"] != formal_report.get("protocol_version")
                or re.fullmatch(SHA256_PATTERN, stage0_report["sha256"]) is None
                or re.fullmatch(SHA256_PATTERN, stage0_report["input_digest"]) is None
            ):
                raise Conflict("M1 Candidate requires a hashed Formal Stage 0 machine report")

            hotspot = None
            if request.hotspot_id is not None:
                hotspot = connection.execute(
                    "SELECT * FROM hotspots WHERE hotspot_id = %s FOR SHARE",
                    (request.hotspot_id,),
                ).fetchone()
                if hotspot is None:
                    raise NotFound(f"M1 Hotspot Intake not found: {request.hotspot_id}")
                hotspot_evidence = dict(hotspot["evidence"])
                if (
                    hotspot["task_id"] != task_id
                    or hotspot["baseline_epoch_id"] != baseline["baseline_epoch_id"]
                    or hotspot_evidence.get("replacement_point")
                    != request.replacement_point
                    or hotspot["candidate_kind"] != request.candidate_kind.value
                    or not isinstance(
                        hotspot_evidence.get("correctness_spec_uri"), str
                    )
                    or re.fullmatch(
                        SHA256_PATTERN,
                        str(hotspot_evidence.get("correctness_spec_hash")),
                    )
                    is None
                ):
                    raise Conflict(
                        "M1 Candidate does not match its Hotspot Intake bindings"
                    )

            metadata = {
                "workflow_type": WorkflowType.MANUAL_CANDIDATE.value,
                "adapter_profile": task["adapter_profile"],
                "stage0_run_id": str(task["stage0_run_id"]),
                "stage0_protocol_hash": baseline["stage0_protocol_hash"],
                "stage0_report": stage0_report,
                "target_snapshot_id": str(task["target_snapshot_id"]),
                "baseline_epoch_id": str(baseline["baseline_epoch_id"]),
                "baseline_source_snapshot_id": str(source["snapshot_id"]),
                "workload_hash": baseline["workload_hash"],
                "candidate_kind": request.candidate_kind.value,
                "hotspot_id": str(request.hotspot_id) if request.hotspot_id else None,
            }
            candidate = connection.execute(
                """
                INSERT INTO candidates (
                    candidate_id, task_id, round_id, baseline_epoch_id,
                    source_hash, variant, state, ordinal, metadata,
                    parent_candidate_id, track, release_mode, candidate_kind,
                    optimization_intent, replacement_point, idempotency_key, hotspot_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, 0, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    candidate_id,
                    task_id,
                    round_id,
                    baseline["baseline_epoch_id"],
                    request.source_hash,
                    f"manual-{request.candidate_kind.value}",
                    CandidateState.PROPOSED.value,
                    Jsonb(metadata),
                    request.parent_candidate_id,
                    request.track.value,
                    request.release_mode.value,
                    request.candidate_kind.value,
                    request.optimization_intent,
                    request.replacement_point,
                    request.idempotency_key,
                    request.hotspot_id,
                ),
            ).fetchone()
            if candidate is None:
                candidate = connection.execute(
                    """
                    SELECT * FROM candidates
                    WHERE candidate_id = %s OR idempotency_key = %s
                    FOR UPDATE
                    """,
                    (candidate_id, request.idempotency_key),
                ).fetchone()
                if candidate is None:
                    raise Conflict("M1 Candidate identity conflict could not be resolved")
                expected = {
                    "candidate_id": candidate_id,
                    "task_id": task_id,
                    "baseline_epoch_id": request.baseline_epoch_id,
                    "hotspot_id": request.hotspot_id,
                    "source_hash": request.source_hash,
                    "optimization_intent": request.optimization_intent,
                    "replacement_point": request.replacement_point,
                    "track": request.track.value,
                    "release_mode": request.release_mode.value,
                    "candidate_kind": request.candidate_kind.value,
                    "parent_candidate_id": request.parent_candidate_id,
                    "idempotency_key": request.idempotency_key,
                }
                if any(candidate[name] != value for name, value in expected.items()):
                    raise Conflict(
                        "M1 Candidate idempotency_key belongs to a different task or input"
                    )
                return candidate

            baseline_source = SourceSnapshot(
                snapshot_id=source["snapshot_id"],
                kind=source["kind"],
                repository=source["repository"],
                commit=source["commit"],
                tree_hash=source["tree_hash"],
                source_hash=source["source_hash"],
                worktree_uri=source["worktree_uri"],
                clean=source["clean"],
                parent_snapshot_id=source["parent_snapshot_id"],
                created_at=source["created_at"],
            )

            payload = {
                "task_id": str(task_id),
                "candidate_id": str(candidate_id),
                "adapter_profile": task["adapter_profile"],
                "round_id": str(round_id),
                "baseline_epoch_id": str(baseline["baseline_epoch_id"]),
                "baseline_source": baseline_source.model_dump(mode="json"),
                "target_snapshot_id": str(target["target_snapshot_id"]),
                "target_fingerprint": target["target_fingerprint"],
                "target": target["specification"],
                "stage0_run_id": str(task["stage0_run_id"]),
                "stage0_protocol_hash": baseline["stage0_protocol_hash"],
                "stage0_report": stage0_report,
                "workload_id": task["workload_id"],
                "workload_hash": baseline["workload_hash"],
                "configuration_hash": baseline["configuration_hash"],
                "candidate_source_hash": request.source_hash,
                "optimization_intent": request.optimization_intent,
                "replacement_point": request.replacement_point,
                "track": request.track.value,
                "release_mode": request.release_mode.value,
                "candidate_kind": request.candidate_kind.value,
                "budget": task["budget"],
            }
            if hotspot is not None:
                payload["hotspot_id"] = str(hotspot["hotspot_id"])
                payload["hotspot_intake_hash"] = hotspot["intake_hash"]
                payload["hotspot"] = dict(hotspot["evidence"])
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    adapter_profile, lease_scope, payload, idempotency_key,
                    priority, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, 'none', %s, %s, 0, 3)
                """,
                (
                    job_id,
                    task_id,
                    JobType.MANUAL_BUILD.value,
                    WorkerType.BUILD.value,
                    task["adapter_profile"],
                    Jsonb(payload),
                    f"{candidate_id}:m1-build:v1",
                ),
            )
            transition_candidate(CandidateState.PROPOSED, CandidateState.BUILDING)
            transition_task(
                TaskState.MANUAL_CANDIDATE_PENDING, TaskState.MANUAL_BUILDING
            )
            candidate = connection.execute(
                """
                UPDATE candidates SET state = %s, updated_at = now()
                WHERE candidate_id = %s RETURNING *
                """,
                (CandidateState.BUILDING.value, candidate_id),
            ).fetchone()
            connection.execute(
                """
                UPDATE tasks SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (TaskState.MANUAL_BUILDING.value, task_id),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'manual_candidate_registered', %s)
                """,
                (
                    task_id,
                    Jsonb(
                        {
                            "candidate_id": str(candidate_id),
                            "baseline_epoch_id": str(baseline["baseline_epoch_id"]),
                            "job_id": str(job_id),
                            "candidate_kind": request.candidate_kind.value,
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
        assert candidate is not None
        return candidate

    def create_candidates(
        self,
        task_id: UUID,
        baseline_epoch_id: UUID,
        round_id: UUID,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        with self.connection() as connection:
            for item in candidates:
                candidate_id = UUID(item["candidate_id"])
                row = connection.execute(
                    """
                    INSERT INTO candidates (
                        candidate_id, task_id, round_id, baseline_epoch_id,
                        source_hash, variant, state, ordinal, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (candidate_id) DO UPDATE
                    SET candidate_id = EXCLUDED.candidate_id
                    RETURNING *
                    """,
                    (
                        candidate_id,
                        task_id,
                        round_id,
                        baseline_epoch_id,
                        item["source_hash"],
                        item["variant"],
                        CandidateState.PROPOSED.value,
                        item["ordinal"],
                        Jsonb(item.get("metadata", {})),
                    ),
                ).fetchone()
                assert row is not None
                created.append(row)
        return created

    def transition_candidate(self, candidate_id: UUID, target: CandidateState) -> dict[str, Any]:
        with self.connection() as connection:
            current = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = %s FOR UPDATE", (candidate_id,)
            ).fetchone()
            if current is None:
                raise NotFound(f"candidate not found: {candidate_id}")
            transition_candidate(CandidateState(current["state"]), target)
            row = connection.execute(
                """
                UPDATE candidates SET state = %s, updated_at = now()
                WHERE candidate_id = %s RETURNING *
                """,
                (target.value, candidate_id),
            ).fetchone()
        assert row is not None
        return row

    def transition_task(self, task_id: UUID, target: TaskState) -> dict[str, Any]:
        with self.connection() as connection:
            return self._transition_task(connection, task_id, target)

    def record_artifact(
        self, task_id: UUID, candidate_id: UUID, result: dict[str, Any]
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, candidate_id, kind, uri, content_hash, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (candidate_id, content_hash) DO UPDATE
                SET content_hash = EXCLUDED.content_hash
                RETURNING *
                """,
                (
                    uuid4(),
                    task_id,
                    candidate_id,
                    result["kind"],
                    result["uri"],
                    result["content_hash"],
                    Jsonb(result.get("metadata", {})),
                ),
            ).fetchone()
        assert row is not None
        return row

    def record_artifact_manifest(
        self,
        task_id: UUID,
        manifest: ArtifactManifest,
        provenance: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, candidate_id, kind, uri, content_hash,
                    source_snapshot_id, build_recipe, metadata, sbom_uri,
                    signature_uri, adapter_provenance, synthetic, idempotency_key
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    manifest.artifact_id,
                    task_id,
                    manifest.candidate_id,
                    manifest.kind,
                    manifest.uri,
                    manifest.content_hash,
                    manifest.source_snapshot_id,
                    Jsonb(manifest.build_recipe),
                    Jsonb(manifest.metadata),
                    manifest.sbom_uri,
                    manifest.signature_uri,
                    Jsonb(provenance),
                    manifest.synthetic,
                    idempotency_key,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "artifact_id": manifest.artifact_id,
            "task_id": task_id,
            "candidate_id": manifest.candidate_id,
            "source_snapshot_id": manifest.source_snapshot_id,
            "content_hash": manifest.content_hash,
            "adapter_provenance": provenance,
            "synthetic": manifest.synthetic,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("artifact idempotency_key was reused with different inputs")
        return row

    def record_execution_request(
        self,
        task_id: UUID,
        candidate_id: UUID,
        evaluation_run_id: UUID,
        request: ExecutionRequest,
        idempotency_key: str,
        variant: str = "legacy",
    ) -> dict[str, Any]:
        request_data = request.model_dump(mode="json")
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO execution_requests (
                    request_id, task_id, candidate_id, evaluation_run_id,
                    target_id, request, idempotency_key, variant
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    request.request_id,
                    task_id,
                    candidate_id,
                    evaluation_run_id,
                    request.target_id,
                    Jsonb(request_data),
                    idempotency_key,
                    variant,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "request_id": request.request_id,
            "task_id": task_id,
            "candidate_id": candidate_id,
            "evaluation_run_id": evaluation_run_id,
            "target_id": request.target_id,
            "request": request_data,
            "variant": variant,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("execution request idempotency_key was reused with different inputs")
        return row

    def record_evidence_bundle(
        self,
        bundle: EvidenceBundle,
        evaluation_run_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any]:
        provenance = [item.model_dump(mode="json") for item in bundle.adapter_provenance]
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO evidence_bundles (
                    evidence_id, task_id, candidate_id, baseline_epoch_id,
                    evaluation_run_id,
                    target_id, evidence_type, protocol_version, artifact_ids,
                    measurement_ids, summary, raw_uris, adapter_provenance,
                    synthetic, idempotency_key, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    bundle.evidence_id,
                    bundle.task_id,
                    bundle.candidate_id,
                    bundle.baseline_epoch_id,
                    evaluation_run_id,
                    bundle.target_id,
                    bundle.evidence_type,
                    bundle.protocol_version,
                    Jsonb([str(item) for item in bundle.artifact_ids]),
                    Jsonb([str(item) for item in bundle.measurement_ids]),
                    Jsonb(bundle.summary),
                    Jsonb(bundle.raw_uris),
                    Jsonb(provenance),
                    bundle.synthetic,
                    idempotency_key,
                    bundle.created_at,
                ),
            ).fetchone()
        assert row is not None
        expected = {
            "evidence_id": bundle.evidence_id,
            "task_id": bundle.task_id,
            "candidate_id": bundle.candidate_id,
            "baseline_epoch_id": bundle.baseline_epoch_id,
            "evaluation_run_id": evaluation_run_id,
            "summary": bundle.summary,
            "adapter_provenance": provenance,
            "synthetic": bundle.synthetic,
        }
        if any(row[name] != value for name, value in expected.items()):
            raise Conflict("evidence idempotency_key was reused with different inputs")
        return row

    def enqueue_framework_execution(
        self,
        task_id: UUID,
        *,
        retest: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["workflow_type"] != WorkflowType.FRAMEWORK_SMOKE.value:
                raise Conflict("task is not a Framework Smoke workflow")

            current = TaskState(task["state"])
            if retest:
                if current is not TaskState.AWAITING_SIGNOFF:
                    raise Conflict("Framework Smoke retest requires awaiting_signoff")
                target_state = TaskState.FRAMEWORK_RETESTING
                retest_ordinal = int(task["retest_count"]) + 1
            else:
                if current not in {
                    TaskState.ARTIFACT_PREPARING,
                    TaskState.FRAMEWORK_EXECUTING,
                }:
                    raise Conflict("initial Framework Smoke execution requires prepared artifact")
                target_state = TaskState.FRAMEWORK_EXECUTING
                retest_ordinal = 0
                if current is TaskState.FRAMEWORK_EXECUTING:
                    existing_job = connection.execute(
                        "SELECT * FROM jobs WHERE idempotency_key = %s",
                        (f"{task_id}:framework-smoke:0:v1",),
                    ).fetchone()
                    if existing_job is None:
                        raise Conflict("Framework Smoke task is executing without its durable job")
                    return existing_job

            data = connection.execute(
                """
                SELECT candidate.*, baseline.hardware_fingerprint,
                       baseline.software_fingerprint, baseline.configuration_hash,
                       artifact.artifact_id, artifact.kind AS artifact_kind,
                       artifact.uri AS artifact_uri,
                       artifact.content_hash AS artifact_content_hash,
                       artifact.source_snapshot_id,
                       artifact.build_recipe, artifact.metadata AS artifact_metadata,
                       artifact.sbom_uri, artifact.signature_uri,
                       artifact.synthetic AS artifact_synthetic,
                       snapshot.specification AS target_specification,
                       snapshot.target_fingerprint
                FROM candidates AS candidate
                JOIN baseline_epochs AS baseline
                  ON baseline.baseline_epoch_id = candidate.baseline_epoch_id
                JOIN artifacts AS artifact
                  ON artifact.candidate_id = candidate.candidate_id
                JOIN target_snapshots AS snapshot
                  ON snapshot.target_snapshot_id = %s
                WHERE candidate.task_id = %s
                  AND candidate.variant = 'framework-noop'
                ORDER BY artifact.created_at DESC
                LIMIT 1
                """,
                (task["target_snapshot_id"], task_id),
            ).fetchone()
            if data is None:
                raise Conflict("Framework Smoke execution requires candidate and artifact")

            candidate_state = CandidateState(data["state"])
            if candidate_state is not CandidateState.FRAMEWORK_SMOKE_RUNNING:
                transition_candidate(candidate_state, CandidateState.FRAMEWORK_SMOKE_RUNNING)
                connection.execute(
                    """
                    UPDATE candidates SET state = %s, updated_at = now()
                    WHERE candidate_id = %s
                    """,
                    (
                        CandidateState.FRAMEWORK_SMOKE_RUNNING.value,
                        data["candidate_id"],
                    ),
                )

            if current is not target_state:
                transition_task(current, target_state)
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, retest_count = %s,
                    version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (target_state.value, retest_ordinal, task_id),
            )

            run_key = f"{task_id}:framework-smoke:{retest_ordinal}:v1"
            evaluation_run_id = uuid5(NAMESPACE_URL, run_key + ":evaluation")
            execution_request_id = uuid5(NAMESPACE_URL, run_key + ":request")
            baseline_execution_request_id = uuid5(NAMESPACE_URL, run_key + ":baseline:request")
            noop_execution_request_id = uuid5(NAMESPACE_URL, run_key + ":noop:request")
            evidence_id = uuid5(NAMESPACE_URL, run_key + ":evidence")
            job_id = uuid5(NAMESPACE_URL, run_key + ":job")
            payload = {
                "task_id": str(task_id),
                "candidate_id": str(data["candidate_id"]),
                "round_id": str(data["round_id"]),
                "baseline_epoch_id": str(data["baseline_epoch_id"]),
                "target": data["target_specification"],
                "target_fingerprint": data["target_fingerprint"],
                "artifact": {
                    "artifact_id": str(data["artifact_id"]),
                    "candidate_id": str(data["candidate_id"]),
                    "kind": data["artifact_kind"],
                    "uri": data["artifact_uri"],
                    "content_hash": data["artifact_content_hash"],
                    "source_snapshot_id": str(data["source_snapshot_id"]),
                    "build_recipe": data["build_recipe"],
                    "metadata": data["artifact_metadata"],
                    "sbom_uri": data["sbom_uri"],
                    "signature_uri": data["signature_uri"],
                    "synthetic": data["artifact_synthetic"],
                },
                "evaluation_run_id": str(evaluation_run_id),
                "execution_request_id": str(execution_request_id),
                "baseline_execution_request_id": str(baseline_execution_request_id),
                "noop_execution_request_id": str(noop_execution_request_id),
                "evidence_id": str(evidence_id),
                "retest_ordinal": retest_ordinal,
            }
            job = connection.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, accepted_worker_type,
                    adapter_profile, lease_scope, payload, idempotency_key,
                    priority, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 3)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    job_id,
                    task_id,
                    JobType.FRAMEWORK_SMOKE.value,
                    WorkerType.GPU.value,
                    task["adapter_profile"],
                    LeaseScope.EXCLUSIVE.value,
                    Jsonb(payload),
                    run_key,
                ),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, %s, %s)
                """,
                (
                    task_id,
                    "framework_smoke_retest_enqueued"
                    if retest
                    else "framework_smoke_execution_enqueued",
                    Jsonb(
                        {
                            "job_id": str(job_id),
                            "retest_ordinal": retest_ordinal,
                            "reason": reason,
                        }
                    ),
                ),
            )
        assert job is not None
        return job

    def record_evaluation(self, run: EvaluationRun) -> dict[str, Any]:
        evidence_uri = run.evidence_uris[0] if run.evidence_uris else None
        measurement = (
            Jsonb(run.measurement.model_dump(mode="json")) if run.measurement is not None else None
        )
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO evaluation_runs (
                    evaluation_run_id, task_id, candidate_id, round_id,
                    baseline_epoch_id, phase, passed, protocol_version,
                    target_fingerprint, idempotency_key, metrics, measurement,
                    evidence_uri, evidence_uris, adapter_provenance, synthetic,
                    created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    run.evaluation_run_id,
                    run.task_id,
                    run.candidate_id,
                    run.round_id,
                    run.baseline_epoch_id,
                    run.phase,
                    run.passed,
                    run.protocol_version,
                    run.target_fingerprint,
                    run.idempotency_key,
                    Jsonb(run.metrics),
                    measurement,
                    evidence_uri,
                    Jsonb(run.evidence_uris),
                    Jsonb([item.model_dump(mode="json") for item in run.adapter_provenance]),
                    run.synthetic,
                    run.created_at,
                ),
            ).fetchone()
        assert row is not None
        expected_measurement = (
            run.measurement.model_dump(mode="json") if run.measurement is not None else None
        )
        expected_provenance = [item.model_dump(mode="json") for item in run.adapter_provenance]
        if any(
            (
                row["task_id"] != run.task_id,
                row["candidate_id"] != run.candidate_id,
                row["round_id"] != run.round_id,
                row["baseline_epoch_id"] != run.baseline_epoch_id,
                row["phase"] != run.phase,
                row["protocol_version"] != run.protocol_version,
                row["target_fingerprint"] != run.target_fingerprint,
                row["passed"] != run.passed,
                row["metrics"] != run.metrics,
                row["measurement"] != expected_measurement,
                row["evidence_uris"] != run.evidence_uris,
                row["adapter_provenance"] != expected_provenance,
                row["synthetic"] != run.synthetic,
            )
        ):
            raise Conflict("evaluation idempotency_key was reused with different inputs")
        return row

    def record_execution_attempt(self, attempt: ExecutionAttempt) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO execution_attempts (
                    execution_attempt_id, evaluation_run_id, request_id,
                    variant, attempt_number, status, exit_code, started_at, finished_at,
                    stdout_uri, stderr_uri, result_metadata, adapter_provenance,
                    synthetic
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (evaluation_run_id, variant, attempt_number) DO UPDATE
                SET attempt_number = EXCLUDED.attempt_number
                RETURNING *
                """,
                (
                    attempt.execution_attempt_id,
                    attempt.evaluation_run_id,
                    attempt.request_id,
                    attempt.variant,
                    attempt.attempt_number,
                    attempt.status,
                    attempt.exit_code,
                    attempt.started_at,
                    attempt.finished_at,
                    attempt.stdout_uri,
                    attempt.stderr_uri,
                    Jsonb(attempt.result_metadata),
                    Jsonb(attempt.adapter_provenance.model_dump(mode="json")),
                    attempt.synthetic,
                ),
            ).fetchone()
        assert row is not None
        expected_provenance = attempt.adapter_provenance.model_dump(mode="json")
        if any(
            (
                row["request_id"] != attempt.request_id,
                row["variant"] != attempt.variant,
                row["status"] != attempt.status,
                row["exit_code"] != attempt.exit_code,
                row["started_at"] != attempt.started_at,
                row["finished_at"] != attempt.finished_at,
                row["stdout_uri"] != attempt.stdout_uri,
                row["stderr_uri"] != attempt.stderr_uri,
                row["result_metadata"] != attempt.result_metadata,
                row["adapter_provenance"] != expected_provenance,
                row["synthetic"] != attempt.synthetic,
            )
        ):
            raise Conflict("execution attempt number was reused with a different request")
        return row

    def list_candidates(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM candidates WHERE task_id = %s ORDER BY ordinal", (task_id,)
            ).fetchall()

    def set_manual_candidate_verdict(
        self,
        candidate_id: UUID,
        verdict: ManualCandidateVerdict,
        evidence_bundle_id: UUID,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = %s FOR UPDATE",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise NotFound(f"candidate not found: {candidate_id}")
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR SHARE",
                (candidate["task_id"],),
            ).fetchone()
            if task is None or task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value:
                raise Conflict("candidate does not belong to an M1 workflow")
            evidence = connection.execute(
                """
                SELECT bundle.*,
                       evaluation.task_id AS evaluation_task_id,
                       evaluation.candidate_id AS evaluation_candidate_id,
                       evaluation.baseline_epoch_id AS evaluation_baseline_epoch_id,
                       evaluation.phase AS evaluation_phase,
                       evaluation.protocol_version AS evaluation_protocol_version,
                       evaluation.metrics AS evaluation_metrics,
                       evaluation.measurement AS evaluation_measurement,
                       evaluation.synthetic AS evaluation_synthetic
                FROM evidence_bundles AS bundle
                JOIN evaluation_runs AS evaluation
                  ON evaluation.evaluation_run_id = bundle.evaluation_run_id
                WHERE bundle.evidence_id = %s
                FOR SHARE OF bundle, evaluation
                """,
                (evidence_bundle_id,),
            ).fetchone()
            if evidence is None:
                raise NotFound(f"evidence bundle not found: {evidence_bundle_id}")
            if (
                evidence["task_id"] != candidate["task_id"]
                or evidence["candidate_id"] != candidate_id
                or evidence["baseline_epoch_id"] != candidate["baseline_epoch_id"]
                or evidence["synthetic"]
                or evidence["evaluation_synthetic"]
                or evidence["evaluation_task_id"] != candidate["task_id"]
                or evidence["evaluation_candidate_id"] != candidate_id
                or evidence["evaluation_baseline_epoch_id"]
                != candidate["baseline_epoch_id"]
                or evidence["evaluation_phase"] != "performance"
                or evidence["protocol_version"]
                != evidence["evaluation_protocol_version"]
            ):
                raise Conflict("M1 verdict evidence has mismatched immutable bindings")
            measurement = evidence["evaluation_measurement"]
            if (
                not isinstance(measurement, dict)
                or measurement.get("status") != "measured"
                or evidence["measurement_ids"] != [measurement.get("measurement_id")]
                or evidence["evaluation_metrics"].get("verdict") != verdict.value
                or evidence["summary"].get("verdict") != verdict.value
                or evidence["summary"].get("automatic_release_allowed") is not False
            ):
                raise Conflict("M1 verdict requires one measured, independently adjudicated series")
            if candidate["verdict"] is not None and (
                candidate["verdict"] != verdict.value
                or candidate["evidence_bundle_id"] != evidence_bundle_id
            ):
                raise Conflict("M1 Candidate already has a different durable verdict")
            row = connection.execute(
                """
                UPDATE candidates
                SET verdict = %s, evidence_bundle_id = %s, updated_at = now()
                WHERE candidate_id = %s
                RETURNING *
                """,
                (verdict.value, evidence_bundle_id, candidate_id),
            ).fetchone()
        assert row is not None
        return row

    def list_artifacts(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM artifacts WHERE task_id = %s ORDER BY created_at", (task_id,)
            ).fetchall()

    def list_evaluations(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM evaluation_runs WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()

    def list_execution_attempts(self, evaluation_run_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM execution_attempts
                WHERE evaluation_run_id = %s
                ORDER BY attempt_number, variant
                """,
                (evaluation_run_id,),
            ).fetchall()

    def list_source_snapshots(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM source_snapshots WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()

    def list_execution_requests(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM execution_requests WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()

    def list_execution_attempts_for_task(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT attempt.*
                FROM execution_attempts AS attempt
                JOIN evaluation_runs AS run
                  ON run.evaluation_run_id = attempt.evaluation_run_id
                WHERE run.task_id = %s
                ORDER BY run.created_at, attempt.attempt_number, attempt.variant
                """,
                (task_id,),
            ).fetchall()

    def list_evidence_bundles(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT * FROM evidence_bundles WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()

    def list_task_events(self, task_id: UUID) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = %s ORDER BY created_at, event_id
                """,
                (task_id,),
            ).fetchall()

    def record_task_event(
        self, task_id: UUID, event_type: str, details: dict[str, Any]
    ) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, %s, %s) RETURNING *
                """,
                (task_id, event_type, Jsonb(details)),
            ).fetchone()
        assert row is not None
        return row

    def manual_candidate_summary(self, task_id: UUID) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value:
            raise Conflict("task is not an M1 Manual Candidate workflow")
        with self.connection() as connection:
            baseline = connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s",
                (task_id,),
            ).fetchone()
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE task_id = %s",
                (task_id,),
            ).fetchone()
            hotspots = connection.execute(
                "SELECT * FROM hotspots WHERE task_id = %s ORDER BY created_at, hotspot_id",
                (task_id,),
            ).fetchall()
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()
            artifacts = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = %s ORDER BY created_at, event_id
                """,
                (task_id,),
            ).fetchall()
            signoff = connection.execute(
                """
                SELECT signoff.*, task.state AS task_state,
                       candidate.state AS candidate_state
                FROM manual_candidate_signoffs AS signoff
                JOIN tasks AS task ON task.task_id = signoff.task_id
                JOIN candidates AS candidate
                  ON candidate.candidate_id = signoff.candidate_id
                WHERE signoff.task_id = %s
                """,
                (task_id,),
            ).fetchone()
        if baseline is None:
            raise Conflict("M1 task is missing its immutable Baseline Epoch")
        return {
            "task": task,
            "baseline": baseline,
            "hotspots": [self._manual_hotspot_view(row) for row in hotspots],
            "candidate": candidate,
            "jobs": jobs,
            "artifacts": artifacts,
            "events": events,
            "signoff": signoff,
        }

    def signoff_manual_candidate_task(
        self,
        task_id: UUID,
        request: ManualCandidateSignoffRequest,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            replay = connection.execute(
                """
                SELECT signoff.*, task.state AS task_state,
                       candidate.state AS candidate_state
                FROM manual_candidate_signoffs AS signoff
                JOIN tasks AS task ON task.task_id = signoff.task_id
                JOIN candidates AS candidate
                  ON candidate.candidate_id = signoff.candidate_id
                WHERE signoff.idempotency_key = %s
                FOR UPDATE OF signoff, task, candidate
                """,
                (request.idempotency_key,),
            ).fetchone()
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["workflow_type"] != WorkflowType.MANUAL_CANDIDATE.value:
                raise Conflict("task is not an M1 Manual Candidate workflow")
            candidate = connection.execute(
                "SELECT * FROM candidates WHERE task_id = %s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if candidate is None:
                raise Conflict("M1 signoff requires a registered Candidate")
            expected = {
                "task_id": task_id,
                "candidate_id": candidate["candidate_id"],
                "decision": request.decision.value,
                "actor": request.actor,
                "reason": request.reason,
                "evidence_bundle_id": request.evidence_bundle_id,
            }
            if replay is not None:
                if any(replay[name] != value for name, value in expected.items()):
                    raise Conflict("signoff idempotency_key was reused with different inputs")
                return replay
            # A concurrent identical request may have committed while this request
            # waited for the task row lock. Re-read before checking terminal state.
            replay = connection.execute(
                """
                SELECT signoff.*, task.state AS task_state,
                       candidate.state AS candidate_state
                FROM manual_candidate_signoffs AS signoff
                JOIN tasks AS task ON task.task_id = signoff.task_id
                JOIN candidates AS candidate
                  ON candidate.candidate_id = signoff.candidate_id
                WHERE signoff.idempotency_key = %s
                """,
                (request.idempotency_key,),
            ).fetchone()
            if replay is not None:
                if any(replay[name] != value for name, value in expected.items()):
                    raise Conflict("signoff idempotency_key was reused with different inputs")
                return replay
            previous = connection.execute(
                "SELECT * FROM manual_candidate_signoffs WHERE task_id = %s",
                (task_id,),
            ).fetchone()
            if previous is not None:
                raise Conflict("M1 task already has a different durable signoff")
            if task["state"] != TaskState.AWAITING_SIGNOFF.value:
                raise Conflict("M1 signoff requires awaiting_signoff")
            if candidate["state"] != CandidateState.AWAITING_SIGNOFF.value:
                raise Conflict("M1 Candidate is not awaiting signoff")
            if candidate["evidence_bundle_id"] != request.evidence_bundle_id:
                raise Conflict("signoff must bind the adjudicated EvidenceBundle")
            evidence = connection.execute(
                "SELECT * FROM evidence_bundles WHERE evidence_id = %s FOR SHARE",
                (request.evidence_bundle_id,),
            ).fetchone()
            if evidence is None or (
                evidence["task_id"] != task_id
                or evidence["candidate_id"] != candidate["candidate_id"]
                or evidence["baseline_epoch_id"] != candidate["baseline_epoch_id"]
                or evidence["synthetic"]
            ):
                raise Conflict("signoff evidence has mismatched immutable bindings")

            signoff_id = uuid5(
                NAMESPACE_URL, f"hcuopt:m1-signoff:{request.idempotency_key}"
            )
            inserted = connection.execute(
                """
                INSERT INTO manual_candidate_signoffs (
                    signoff_id, task_id, candidate_id, decision, actor, reason,
                    evidence_bundle_id, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING signoff_id
                """,
                (
                    signoff_id,
                    task_id,
                    candidate["candidate_id"],
                    request.decision.value,
                    request.actor,
                    request.reason,
                    request.evidence_bundle_id,
                    request.idempotency_key,
                ),
            ).fetchone()
            if inserted is None:
                replay = connection.execute(
                    """
                    SELECT signoff.*, task.state AS task_state,
                           candidate.state AS candidate_state
                    FROM manual_candidate_signoffs AS signoff
                    JOIN tasks AS task ON task.task_id = signoff.task_id
                    JOIN candidates AS candidate
                      ON candidate.candidate_id = signoff.candidate_id
                    WHERE signoff.idempotency_key = %s
                    """,
                    (request.idempotency_key,),
                ).fetchone()
                if replay is None:
                    raise Conflict("M1 signoff identity conflict could not be resolved")
                if any(replay[name] != value for name, value in expected.items()):
                    raise Conflict(
                        "signoff idempotency_key was reused with different inputs"
                    )
                return replay
            if request.decision is ManualCandidateDecision.APPROVED:
                task_target = TaskState.COMPLETED
                candidate_target = CandidateState.ACCEPTED
            else:
                task_target = TaskState.REJECTED
                candidate_target = CandidateState.REJECTED
            transition_task(TaskState(task["state"]), task_target)
            transition_candidate(CandidateState(candidate["state"]), candidate_target)
            connection.execute(
                """
                UPDATE tasks SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (task_target.value, task_id),
            )
            connection.execute(
                """
                UPDATE candidates SET state = %s, updated_at = now()
                WHERE candidate_id = %s
                """,
                (candidate_target.value, candidate["candidate_id"]),
            )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'manual_candidate_signoff_recorded', %s)
                """,
                (
                    task_id,
                    Jsonb(
                        {
                            "candidate_id": str(candidate["candidate_id"]),
                            "decision": request.decision.value,
                            "actor": request.actor,
                            "evidence_bundle_id": str(request.evidence_bundle_id),
                            "automatic_release_allowed": False,
                        }
                    ),
                ),
            )
            row = connection.execute(
                """
                SELECT signoff.*, task.state AS task_state,
                       candidate.state AS candidate_state
                FROM manual_candidate_signoffs AS signoff
                JOIN tasks AS task ON task.task_id = signoff.task_id
                JOIN candidates AS candidate
                  ON candidate.candidate_id = signoff.candidate_id
                WHERE signoff.signoff_id = %s
                """,
                (signoff_id,),
            ).fetchone()
        assert row is not None
        return row

    def signoff_framework_task(
        self,
        task_id: UUID,
        request: FrameworkSmokeSignoffRequest,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            replay = connection.execute(
                """
                SELECT signoff.*, task.state AS task_state
                FROM framework_smoke_signoffs AS signoff
                JOIN tasks AS task ON task.task_id = signoff.task_id
                WHERE signoff.idempotency_key = %s
                FOR UPDATE OF signoff, task
                """,
                (request.idempotency_key,),
            ).fetchone()
            expected = {
                "task_id": task_id,
                "decision": request.decision.value,
                "actor": request.actor,
                "reason": request.reason,
                "evidence_bundle_id": request.evidence_bundle_id,
            }
            if replay is not None:
                if any(replay[name] != value for name, value in expected.items()):
                    raise Conflict("signoff idempotency_key was reused with different inputs")
                return replay

            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["workflow_type"] != WorkflowType.FRAMEWORK_SMOKE.value:
                raise Conflict("task is not a Framework Smoke workflow")
            # A concurrent identical request may have committed while this request
            # waited for the task lock. Re-read the durable decision before checking
            # the now-terminal task state.
            replay = connection.execute(
                """
                SELECT signoff.*, task.state AS task_state
                FROM framework_smoke_signoffs AS signoff
                JOIN tasks AS task ON task.task_id = signoff.task_id
                WHERE signoff.idempotency_key = %s
                """,
                (request.idempotency_key,),
            ).fetchone()
            if replay is not None:
                if any(replay[name] != value for name, value in expected.items()):
                    raise Conflict("signoff idempotency_key was reused with different inputs")
                return replay
            if task["state"] != TaskState.AWAITING_SIGNOFF.value:
                raise Conflict("Framework Smoke signoff requires awaiting_signoff")
            previous = connection.execute(
                "SELECT * FROM framework_smoke_signoffs WHERE task_id = %s",
                (task_id,),
            ).fetchone()
            if previous is not None:
                raise Conflict("Framework Smoke task already has a signoff decision")
            evidence = connection.execute(
                """
                SELECT bundle.*, run.passed AS evaluation_passed
                FROM evidence_bundles AS bundle
                JOIN evaluation_runs AS run
                  ON run.evaluation_run_id = bundle.evaluation_run_id
                WHERE bundle.evidence_id = %s
                FOR UPDATE OF bundle, run
                """,
                (request.evidence_bundle_id,),
            ).fetchone()
            if evidence is None:
                raise NotFound(f"evidence bundle not found: {request.evidence_bundle_id}")
            if evidence["task_id"] != task_id:
                raise Conflict("signoff evidence belongs to a different task")
            if evidence["evaluation_passed"] is not True:
                raise Conflict("signoff evidence must bind a passed Framework Smoke run")
            latest_evidence = connection.execute(
                """
                SELECT evidence_id FROM evidence_bundles
                WHERE task_id = %s ORDER BY created_at DESC, evidence_id DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            assert latest_evidence is not None
            if latest_evidence["evidence_id"] != request.evidence_bundle_id:
                raise Conflict("signoff must bind the latest Framework Smoke evidence")

            target_state = (
                TaskState.COMPLETED
                if request.decision is FrameworkSmokeDecision.APPROVED
                else TaskState.REJECTED
            )
            transition_task(TaskState(task["state"]), target_state)
            signoff_id = uuid4()
            row = connection.execute(
                """
                INSERT INTO framework_smoke_signoffs (
                    signoff_id, task_id, decision, actor, reason,
                    evidence_bundle_id, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    signoff_id,
                    task_id,
                    request.decision.value,
                    request.actor,
                    request.reason,
                    request.evidence_bundle_id,
                    request.idempotency_key,
                ),
            ).fetchone()
            if row is None:
                # This can only be a global idempotency-key race with another task;
                # same-task requests are serialized by the task row lock above.
                replay = connection.execute(
                    """
                    SELECT signoff.*, task.state AS task_state
                    FROM framework_smoke_signoffs AS signoff
                    JOIN tasks AS task ON task.task_id = signoff.task_id
                    WHERE signoff.idempotency_key = %s
                    """,
                    (request.idempotency_key,),
                ).fetchone()
                if replay is not None and all(
                    replay[name] == value for name, value in expected.items()
                ):
                    return replay
                raise Conflict("signoff idempotency_key was reused with different inputs")
            connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s
                """,
                (target_state.value, task_id),
            )
            if (
                request.decision is FrameworkSmokeDecision.REJECTED
                and evidence["candidate_id"] is not None
            ):
                candidate = connection.execute(
                    "SELECT * FROM candidates WHERE candidate_id = %s FOR UPDATE",
                    (evidence["candidate_id"],),
                ).fetchone()
                if candidate is not None and candidate["state"] != CandidateState.REJECTED.value:
                    transition_candidate(
                        CandidateState(candidate["state"]), CandidateState.REJECTED
                    )
                    connection.execute(
                        """
                        UPDATE candidates SET state = %s, updated_at = now()
                        WHERE candidate_id = %s
                        """,
                        (CandidateState.REJECTED.value, evidence["candidate_id"]),
                    )
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'framework_smoke_signed_off', %s)
                """,
                (
                    task_id,
                    Jsonb(
                        {
                            "signoff_id": str(signoff_id),
                            "decision": request.decision.value,
                            "actor": request.actor,
                            "reason": request.reason,
                            "evidence_bundle_id": str(request.evidence_bundle_id),
                        }
                    ),
                ),
            )
            row["task_state"] = target_state.value
        return row

    def cancel_framework_task(self, task_id: UUID, reason: str) -> dict[str, Any]:
        with self.connection() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE task_id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFound(f"task not found: {task_id}")
            if task["workflow_type"] != WorkflowType.FRAMEWORK_SMOKE.value:
                raise Conflict("task is not a Framework Smoke workflow")
            if task["state"] == TaskState.CANCELLED.value:
                return task
            if task["state"] in {TaskState.COMPLETED.value, TaskState.REJECTED.value}:
                raise Conflict("terminal Framework Smoke task cannot be cancelled")
            transition_task(TaskState(task["state"]), TaskState.CANCELLED)
            jobs = connection.execute(
                """
                SELECT * FROM jobs
                WHERE task_id = %s AND state IN ('queued', 'running')
                FOR UPDATE
                """,
                (task_id,),
            ).fetchall()
            for job in jobs:
                if job["state"] == JobState.RUNNING.value:
                    self._release_resource(connection, job, "task_cancelled")
            connection.execute(
                """
                UPDATE jobs
                SET state = 'cancelled', last_error = %s, claimed_by = NULL,
                    claim_token = NULL, claimed_at = NULL, heartbeat_at = NULL,
                    lease_id = NULL, resource_id = NULL, fencing_token = NULL,
                    finished_at = now(),
                    updated_at = now()
                WHERE task_id = %s AND state IN ('queued', 'running')
                """,
                (Jsonb({"code": "task_cancelled", "message": reason}), task_id),
            )
            connection.execute(
                """
                UPDATE candidates SET state = %s, updated_at = now()
                WHERE task_id = %s AND state <> %s
                """,
                (CandidateState.REJECTED.value, task_id, CandidateState.REJECTED.value),
            )
            updated = connection.execute(
                """
                UPDATE tasks
                SET state = %s, version = version + 1, updated_at = now()
                WHERE task_id = %s RETURNING *
                """,
                (TaskState.CANCELLED.value, task_id),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, details)
                VALUES (%s, 'framework_smoke_cancelled', %s)
                """,
                (task_id, Jsonb({"reason": reason})),
            )
        assert updated is not None
        return updated

    def framework_smoke_summary(self, task_id: UUID) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task.get("workflow_type") != WorkflowType.FRAMEWORK_SMOKE.value:
            raise Conflict("task is not a Framework Smoke workflow")
        target = self.get_target_snapshot(task_id)
        return {
            "task": task,
            "target": target["specification"],
            "baseline": self.get_baseline(task_id),
            "source_snapshots": self.list_source_snapshots(task_id),
            "candidates": self.list_candidates(task_id),
            "artifacts": self.list_artifacts(task_id),
            "execution_requests": self.list_execution_requests(task_id),
            "execution_attempts": self.list_execution_attempts_for_task(task_id),
            "evaluations": self.list_evaluations(task_id),
            "evidence_bundles": self.list_evidence_bundles(task_id),
            "events": self.list_task_events(task_id),
        }

    def cancel_job(self, job_id: UUID, reason: str) -> dict[str, Any]:
        with self.connection() as connection:
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFound(f"job not found: {job_id}")
            if job["state"] in {
                JobState.SUCCEEDED.value,
                JobState.FAILED.value,
                JobState.CANCELLED.value,
            }:
                return job
            if job["state"] == JobState.RUNNING.value:
                self._release_resource(connection, job, "job_cancelled")
            row = connection.execute(
                """
                UPDATE jobs
                SET state = 'cancelled', last_error = %s, claimed_by = NULL,
                    claim_token = NULL, lease_id = NULL, resource_id = NULL,
                    fencing_token = NULL,
                    finished_at = now(), updated_at = now()
                WHERE job_id = %s RETURNING *
                """,
                (Jsonb({"code": "cancelled", "message": reason}), job_id),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO job_events (job_id, event_type, details)
                VALUES (%s, 'cancelled', %s)
                """,
                (job_id, Jsonb({"reason": reason})),
            )
        assert row is not None
        return row

    def task_summary(self, task_id: UUID) -> dict[str, Any]:
        task = self.get_task(task_id)
        with self.connection() as connection:
            baseline = connection.execute(
                "SELECT * FROM baseline_epochs WHERE task_id = %s", (task_id,)
            ).fetchone()
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE task_id = %s ORDER BY created_at", (task_id,)
            ).fetchall()
            candidates = connection.execute(
                "SELECT * FROM candidates WHERE task_id = %s ORDER BY ordinal", (task_id,)
            ).fetchall()
            artifacts = connection.execute(
                "SELECT * FROM artifacts WHERE task_id = %s ORDER BY created_at", (task_id,)
            ).fetchall()
            evaluations = connection.execute(
                "SELECT * FROM evaluation_runs WHERE task_id = %s ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return {
            "task": task,
            "baseline": baseline,
            "jobs": jobs,
            "candidates": candidates,
            "artifacts": artifacts,
            "evaluations": evaluations,
        }

    def report_resource_cleanup(
        self,
        resource_id: str,
        fencing_token: int,
        cleanup_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        healthy = _cleanup_is_healthy(cleanup_evidence)
        evidence = {**cleanup_evidence, "healthy": healthy, "reason": "cleanup_reported"}
        with self.connection() as connection:
            resource = connection.execute(
                "SELECT * FROM resources WHERE resource_id = %s FOR UPDATE",
                (resource_id,),
            ).fetchone()
            if resource is None:
                raise NotFound(f"resource not found: {resource_id}")
            if int(resource["fencing_token"]) != fencing_token:
                raise StaleFencingToken("resource cleanup used a stale fencing token")
            if resource["state"] not in {
                "fencing",
                "health_check",
                "quarantined",
            }:
                raise Conflict(
                    f"resource cleanup cannot be reported from state {resource['state']}"
                )
            row = connection.execute(
                """
                UPDATE resources
                SET state = %s, owner_job_id = NULL, lease_id = NULL,
                    expires_at = NULL, cleanup_evidence = %s, updated_at = now()
                WHERE resource_id = %s AND fencing_token = %s
                RETURNING *
                """,
                (
                    "available" if healthy else "quarantined",
                    Jsonb(evidence),
                    resource_id,
                    fencing_token,
                ),
            ).fetchone()
        assert row is not None
        return row

    def list_resources(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return connection.execute("SELECT * FROM resources ORDER BY resource_id").fetchall()

    def mark_job_advanced(self, job_id: UUID) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE jobs SET workflow_advanced_at = now(), updated_at = now()
                WHERE job_id = %s
                """,
                (job_id,),
            )

    def unadvanced_succeeded_jobs(
        self, workflow_type: WorkflowType | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            workflow_filter = "AND task.workflow_type = %s" if workflow_type is not None else ""
            parameters: tuple[Any, ...] = (
                (workflow_type.value, limit) if workflow_type is not None else (limit,)
            )
            return connection.execute(
                f"""
                SELECT job.* FROM jobs AS job
                JOIN tasks AS task ON task.task_id = job.task_id
                WHERE job.state = 'succeeded'
                  AND job.workflow_advanced_at IS NULL
                  {workflow_filter}
                ORDER BY job.finished_at
                LIMIT %s
                """,
                parameters,
            ).fetchall()


def _cleanup_is_healthy(evidence: Mapping[str, Any] | None) -> bool:
    if not evidence:
        return False
    fence = evidence.get("fence")
    health = evidence.get("health")
    return (
        isinstance(fence, Mapping)
        and isinstance(health, Mapping)
        and fence.get("fenced") is True
        and health.get("healthy") is True
    )
