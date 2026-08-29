# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.m2 import SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import (
    FormalAuthorityContextContent,
    FormalEvidenceStoreRef,
    FormalVerifierRef,
    formal_authority_context_ref,
    publish_formal_authority_context,
)
from hcuopt.domain.enums import (
    ManualCandidateVerdict,
    RoundBarrierOutcome,
    RoundCandidateState,
    RoundPhase,
    SearchRoundRunMode,
)
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.evidence_reader import HashedEvidenceReader
from hcuopt.evaluation.m2_formal_authority import (
    FormalBarrierPersistence,
    FormalEvidenceBundlePersistence,
    FormalHoldoutRevealPersistence,
    FormalMultipleComparisonPersistence,
    formal_authority_payload_hash,
)
from hcuopt.evaluation.m2_formal_finalizer import (
    FormalM2EvidenceIndex,
    FormalM2EvidenceIndexEntry,
    M2FormalRoundFinalizer,
    build_formal_round_evidence,
    formal_round_evidence_requirements,
)
from hcuopt.evaluation.m2_models import (
    AdjustedCandidateResult,
    BarrierMemberResult,
    MultipleComparisonResult,
    RoundBarrierResult,
)
from hcuopt.evaluation.m2_statistics import (
    M2_FWER_PROTOCOL_VERSION,
    holdout_family_hash,
    m2_fwer_protocol_hash,
    recompute_multiple_comparison_result_hash,
)
from hcuopt.measurement.evidence import EvidenceArtifact, canonical_json_bytes
from hcuopt.orchestrator.search_round import (
    round_budget_ledger_document,
    round_budget_ledger_hash,
)
from hcuopt.storage.repository import PostgresRepository

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - optional until dev dependencies are installed
    psycopg = None
    Jsonb = None


DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _hash(value: str) -> str:
    return "sha256:" + value * 64


class _ProtectedEvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._by_hash: dict[str, EvidenceArtifact] = {}

    def publish(self, value: object) -> EvidenceArtifact:
        return self.publish_bytes(canonical_json_bytes(value))

    def publish_bytes(self, encoded: bytes) -> EvidenceArtifact:
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        existing = self._by_hash.get(digest)
        if existing is not None:
            return existing
        path = self.root / f"{digest[7:]}.json"
        path.write_bytes(encoded)
        artifact = EvidenceArtifact(
            uri=path.as_uri(), sha256=digest, byte_count=len(encoded)
        )
        self._by_hash[digest] = artifact
        return artifact

    def artifact_for_hash(self, expected_hash: str) -> EvidenceArtifact:
        return self._by_hash[expected_hash]

    def tamper(self, expected_hash: str) -> None:
        artifact = self.artifact_for_hash(expected_hash)
        path = self.root / f"{artifact.sha256[7:]}.json"
        path.write_bytes(b'{"tampered":true}\n')


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class M2FormalAuthorityPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        assert Jsonb is not None
        self.temp_dir = tempfile.TemporaryDirectory()
        self.evidence_store = _ProtectedEvidenceStore(Path(self.temp_dir.name))
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute("TRUNCATE tasks, target_snapshots RESTART IDENTITY CASCADE")
        self.connection.commit()
        self.ids = {name: uuid4() for name in self._id_names()}
        self._seed_formal_parent()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    @staticmethod
    def _id_names() -> tuple[str, ...]:
        return (
            "target",
            "stage0_task",
            "stage0_run",
            "baseline_task",
            "source",
            "baseline",
            "hotspot",
            "round_task",
            "round",
            "context",
            "search_barrier",
            "reveal_lease",
            "holdout_barrier",
            "fwer",
            "bundle",
        )

    def _seed_formal_parent(self) -> None:
        ids = self.ids
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO target_snapshots (
                    target_snapshot_id, target_id, target_fingerprint,
                    specification, source_path
                ) VALUES (%s, 'nmz36-formal-test', 'formal-target-fingerprint', %s, %s)
                """,
                (ids["target"], Jsonb({}), "fixture://formal-target"),
            )
            cursor.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile, stage0_authority, project_mode
                ) VALUES (
                    %s, 'Formal Stage 0', 'formal-workload', 'formal-stage0-task',
                    'completed', '{}'::jsonb, FALSE, 'stage0', 'nmz36-formal-test',
                    %s, 'formal-stage0-v1', 'formal', 'degraded_manual_intake'
                )
                """,
                (ids["stage0_task"], ids["target"]),
            )
            cursor.execute(
                """
                INSERT INTO stage0_runs (
                    stage0_run_id, task_id, target_snapshot_id, adapter_profile,
                    mode, state, protocol_version, idempotency_key, report,
                    created_at, finalized_at
                ) VALUES (
                    %s, %s, %s, 'formal-stage0-v1', 'formal', 'finalized',
                    'formal-stage0-protocol-v1', 'formal-stage0-run', %s, %s, %s
                )
                """,
                (
                    ids["stage0_run"],
                    ids["stage0_task"],
                    ids["target"],
                    Jsonb({"state": "finalized"}),
                    NOW,
                    NOW,
                ),
            )
            cursor.execute(
                """
                INSERT INTO stage0_evidence (
                    task_id, evidence, report, stage0_run_id
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    ids["stage0_task"],
                    Jsonb(
                        {
                            "synthetic": False,
                            "stage0_run_id": str(ids["stage0_run"]),
                            "protocol_version": "formal-stage0-protocol-v1",
                            "protocol_hash": _hash("1"),
                        }
                    ),
                    Jsonb(
                        {
                            "evidence_authority": "formal",
                            "automatic_release_allowed": False,
                        }
                    ),
                    ids["stage0_run"],
                ),
            )
            cursor.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile, stage0_run_id,
                    stage0_authority, project_mode
                ) VALUES (
                    %s, 'Formal Baseline', 'formal-workload', 'formal-baseline-task',
                    'completed', '{}'::jsonb, FALSE, 'manual_candidate',
                    'nmz36-formal-test', %s, 'formal-adapter-v1', %s,
                    'formal', 'degraded_manual_intake'
                )
                """,
                (ids["baseline_task"], ids["target"], ids["stage0_run"]),
            )
            cursor.execute(
                """
                INSERT INTO source_snapshots (
                    snapshot_id, task_id, kind, repository, commit, tree_hash,
                    source_hash, worktree_uri, clean, idempotency_key,
                    adapter_provenance, synthetic, created_at
                ) VALUES (
                    %s, %s, 'baseline', 'fixture://formal-source', 'formal-commit',
                    %s, %s, 'fixture://formal-worktree', TRUE,
                    'formal-baseline-source', %s, FALSE, %s
                )
                """,
                (
                    ids["source"],
                    ids["baseline_task"],
                    _hash("2"),
                    _hash("3"),
                    Jsonb([{"implementation_kind": "git"}]),
                    NOW,
                ),
            )
            cursor.execute(
                """
                INSERT INTO baseline_epochs (
                    baseline_epoch_id, task_id, hardware_fingerprint,
                    software_fingerprint, workload_id, configuration_hash,
                    frozen, baseline_kind, target_snapshot_id, stage0_run_id,
                    stage0_protocol_hash, source_snapshot_id, workload_hash,
                    image_digest, adapter_profile
                ) VALUES (
                    %s, %s, 'formal-hardware', 'formal-software',
                    'formal-workload', %s, TRUE, 'manual_candidate', %s, %s,
                    %s, %s, %s, %s, 'formal-adapter-v1'
                )
                """,
                (
                    ids["baseline"],
                    ids["baseline_task"],
                    _hash("4"),
                    ids["target"],
                    ids["stage0_run"],
                    _hash("1"),
                    ids["source"],
                    _hash("5"),
                    _hash("6"),
                ),
            )
            cursor.execute(
                """
                INSERT INTO hotspots (
                    hotspot_id, task_id, baseline_epoch_id, symbol, share_ratio,
                    opportunity_score, patchability, evidence, candidate_kind,
                    actor, intake_hash, idempotency_key
                ) VALUES (
                    %s, %s, %s, 'sglang.formal.hotspot', 0.2, 0.5,
                    'patchable', %s, 'business', 'operator-c', %s,
                    'formal-hotspot'
                )
                """,
                (
                    ids["hotspot"],
                    ids["baseline_task"],
                    ids["baseline"],
                    Jsonb({"replacement_point": "sglang.formal.hotspot"}),
                    _hash("7"),
                ),
            )
            cursor.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile, stage0_run_id,
                    stage0_authority, project_mode
                ) VALUES (
                    %s, 'Formal M2 Round', 'formal-workload', 'formal-round-task',
                    'running', '{}'::jsonb, FALSE, 'search_round',
                    'nmz36-formal-test', %s, 'formal-adapter-v1', %s,
                    'formal', 'degraded_manual_intake'
                )
                """,
                (ids["round_task"], ids["target"], ids["stage0_run"]),
            )
            cursor.execute(
                """
                INSERT INTO search_rounds (
                    round_id, task_id, idempotency_key, state, run_mode, project_mode,
                    target_snapshot_id, stage0_run_id, stage0_protocol_hash,
                    baseline_epoch_id, hotspot_id, replacement_point, workload_id,
                    workload_hash, configuration_hash, image_digest, adapter_profile,
                    declared_candidate_count, max_promoted, family_alpha,
                    search_plan_hash, holdout_plan_commitment,
                    holdout_plan_authority_id, holdout_plan_authority_hash,
                    selection_rule_hash, budget, candidate_family_hash,
                    artifact_family_hash, automatic_release_allowed
                ) VALUES (
                    %s, %s, 'formal-round', 'search_measuring', 'formal',
                    'degraded_manual_intake', %s, %s, %s, %s, %s,
                    'sglang.formal.hotspot', 'formal-workload', %s, %s, %s,
                    'formal-adapter-v1', 2, 1, 0.05, %s, %s,
                    'd-holdout-authority-v1', %s, %s, %s,
                    %s, %s, FALSE
                )
                """,
                (
                    ids["round"],
                    ids["round_task"],
                    ids["target"],
                    ids["stage0_run"],
                    _hash("1"),
                    ids["baseline"],
                    ids["hotspot"],
                    _hash("5"),
                    _hash("4"),
                    _hash("6"),
                    _hash("8"),
                    _hash("9"),
                    _hash("a"),
                    _hash("b"),
                    Jsonb(
                        {
                            "max_candidates": 2,
                            "max_build_attempts": 2,
                            "max_correctness_attempts": 2,
                            "max_search_samples": 20,
                            "max_holdout_samples": 20,
                            "max_wall_seconds": 600,
                            "max_exclusive_lease_seconds": 300,
                        }
                    ),
                    _hash("c"),
                    _hash("d"),
                ),
            )
        self.connection.commit()

    def _context_params(self, **updates: object) -> dict[str, object]:
        values: dict[str, object] = {
            "context": self.ids["context"],
            "context_hash": _hash("e"),
            "round": self.ids["round"],
            "task": self.ids["round_task"],
            "target": self.ids["target"],
            "stage0": self.ids["stage0_run"],
            "stage0_hash": _hash("1"),
            "baseline": self.ids["baseline"],
            "hotspot": self.ids["hotspot"],
            "candidate_family": _hash("c"),
            "artifact_family": _hash("d"),
            "search_plan": _hash("8"),
            "holdout_commitment": _hash("9"),
            "holdout_authority_hash": _hash("a"),
            "selection_rule": _hash("b"),
            "synthetic": False,
            "auto_release": False,
        }
        values.update(updates)
        return values

    def _publish_formal_index(self, context, requirements) -> EvidenceArtifact:
        entries = []
        for requirement in requirements:
            artifact = (
                self.evidence_store.publish_bytes(requirement.expected_bytes)
                if requirement.expected_bytes is not None
                else self.evidence_store.artifact_for_hash(requirement.sha256)
            )
            self.assertEqual(artifact.sha256, requirement.sha256)
            entries.append(
                FormalM2EvidenceIndexEntry(
                    role=requirement.role,
                    evidence_type=requirement.evidence_type,
                    uri=artifact.uri,
                    sha256=artifact.sha256,
                    producer_role=requirement.producer_role,
                    producer_id=requirement.producer_id
                    or f"producer-{requirement.producer_role}",
                    producer_hash=requirement.producer_hash
                    or self.evidence_store.publish(
                        {"producer": requirement.producer_role}
                    ).sha256,
                    retention_owner="m2-formal-postgres-test",
                    accessibility_checked_at=NOW,
                )
            )
        index = FormalM2EvidenceIndex(
            round_id=context.round_id,
            authority_context_id=context.authority_context_id,
            authority_context_hash=context.context_hash,
            evidence_store_id=context.evidence_store.store_id,
            evidence_store_hash=context.evidence_store.store_hash,
            entries=tuple(entries),
            created_at=NOW,
        )
        return self.evidence_store.publish(index)

    def _prepare_repository_zero_promotion(self, *, promote: bool = False):
        search_plan = self.evidence_store.publish({"phase": "search", "plan": True})
        selection_rule = self.evidence_store.publish({"selection": "formal-v1"})
        input_summary = self.evidence_store.publish({"phase": "search", "members": 2})
        store_identity = self.evidence_store.publish({"store": "formal-evidence-v1"})
        access_policy = self.evidence_store.publish({"access": "protected-local-root"})
        verifier_identity = self.evidence_store.publish({"verifier": "m2-d-verifier"})
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE search_rounds
                SET search_plan_hash = %s, selection_rule_hash = %s
                WHERE round_id = %s
                """,
                (search_plan.sha256, selection_rule.sha256, self.ids["round"]),
            )
        candidate_rows = []
        members = []
        for ordinal in range(2):
            candidate_id = uuid4()
            round_candidate_id = uuid4()
            artifact_id = uuid4()
            artifact = self.evidence_store.publish(
                {"candidate": ordinal, "artifact": "overlay"}
            )
            correctness = self.evidence_store.publish(
                {"candidate": ordinal, "correctness": True}
            )
            budget = self.evidence_store.publish({"candidate": ordinal, "budget": True})
            cleanup = self.evidence_store.publish(
                {"candidate": ordinal, "cleanup": "complete"}
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO candidates (
                        candidate_id, task_id, round_id, baseline_epoch_id,
                        source_hash, variant, state, ordinal, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'built', %s, '{}'::jsonb)
                    """,
                    (
                        candidate_id,
                        self.ids["round_task"],
                        self.ids["round"],
                        self.ids["baseline"],
                        _hash(str(ordinal + 6)),
                        f"formal-candidate-{ordinal}",
                        ordinal,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_id, task_id, candidate_id, kind, uri, content_hash,
                        metadata
                    ) VALUES (%s, %s, %s, 'overlay', %s, %s, '{}'::jsonb)
                    """,
                    (
                        artifact_id,
                        self.ids["round_task"],
                        candidate_id,
                        artifact.uri,
                        artifact.sha256,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO round_candidates (
                        round_candidate_id, round_id, candidate_id, ordinal,
                        source_package_store_id, source_package_store_hash,
                        source_package_hash, source_manifest_hash,
                        baseline_source_hash, candidate_source_hash,
                        optimization_intent, replacement_point, candidate_kind,
                        artifact_id, artifact_hash, state, idempotency_key
                    ) VALUES (
                        %s, %s, %s, %s, 'formal-source-store', %s, %s, %s,
                        %s, %s, %s, 'sglang.formal.hotspot', 'business',
                        %s, %s, 'correctness_passed', %s
                    )
                    """,
                    (
                        round_candidate_id,
                        self.ids["round"],
                        candidate_id,
                        ordinal,
                        _hash(str(ordinal + 1)),
                        _hash(str(ordinal + 2)),
                        _hash(str(ordinal + 3)),
                        _hash("3"),
                        _hash(str(ordinal + 4)),
                        f"formal optimization {ordinal}",
                        artifact_id,
                        artifact.sha256,
                        f"formal-round-candidate-{ordinal}",
                    ),
                )
            candidate_rows.append((candidate_id, round_candidate_id, artifact_id, artifact))
            members.append(
                BarrierMemberResult(
                    round_candidate_id=round_candidate_id,
                    candidate_id=candidate_id,
                    candidate_state=RoundCandidateState.SEARCH_MEASURED,
                    artifact_id=artifact_id,
                    artifact_hash=artifact.sha256,
                    correctness_evidence_hash=correctness.sha256,
                    round_measurement_ref_id=uuid4(),
                    budget_usage_evidence_hash=budget.sha256,
                    cleanup_evidence_hash=cleanup.sha256,
                    synthetic=False,
                )
            )
        self.connection.commit()
        context = publish_formal_authority_context(
            FormalAuthorityContextContent(
                authority_context_id=self.ids["context"],
                round_id=self.ids["round"],
                task_id=self.ids["round_task"],
                target_snapshot_id=self.ids["target"],
                stage0_run_id=self.ids["stage0_run"],
                stage0_protocol_hash=_hash("1"),
                baseline_epoch_id=self.ids["baseline"],
                hotspot_id=self.ids["hotspot"],
                target_profile_hash=_hash("f"),
                workload_profile_hash=_hash("0"),
                measurement_profile_hash=_hash("2"),
                candidate_family_hash=_hash("c"),
                artifact_family_hash=_hash("d"),
                search_plan_hash=search_plan.sha256,
                holdout_plan_commitment=_hash("9"),
                holdout_plan_authority_id="d-holdout-authority-v1",
                holdout_plan_authority_hash=_hash("a"),
                selection_rule_hash=selection_rule.sha256,
                evidence_store=FormalEvidenceStoreRef(
                    store_id="formal-evidence-v1",
                    store_version=1,
                    store_hash=store_identity.sha256,
                    access_policy_hash=access_policy.sha256,
                ),
                verifier=FormalVerifierRef(
                    verifier_id="m2-d-verifier",
                    verifier_version="m2-d-formal-v1",
                    verifier_hash=verifier_identity.sha256,
                ),
                sealed_by="operator-a",
                sealed_at=NOW,
            )
        )
        repository = PostgresRepository(
            DATABASE_URL,
            m2_formal_finalizer=M2FormalRoundFinalizer(
                HashedEvidenceReader(Path(self.temp_dir.name))
            ),
        )
        repository.record_formal_authority_context(context)
        repository.record_formal_authority_context(context)
        with repository.connection() as connection:
            pre_barrier_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s",
                (self.ids["round"],),
            ).fetchone()
        assert pre_barrier_row is not None
        pre_barrier_authority = SearchRound.model_validate(
            {name: pre_barrier_row[name] for name in SearchRound.model_fields}
        )
        ordered_members = tuple(sorted(members, key=lambda item: str(item.candidate_id)))
        promoted_ids = (ordered_members[0].candidate_id,) if promote else ()
        promoted_family_hash = (
            holdout_family_hash(
                round_authority=pre_barrier_authority,
                members=(ordered_members[0],),
            )
            if promote
            else None
        )
        barrier = RoundBarrierResult(
            barrier_id=self.ids["search_barrier"],
            round_id=self.ids["round"],
            run_mode=SearchRoundRunMode.FORMAL,
            synthetic=False,
            phase=RoundPhase.SEARCH,
            input_family_hash=context.artifact_family_hash,
            expected_member_count=2,
            members=ordered_members,
            rule_version="m2-search-v1",
            rule_hash=context.selection_rule_hash,
            promoted_candidate_ids=promoted_ids,
            outcome=(
                RoundBarrierOutcome.MEMBERS_PROMOTED
                if promote
                else RoundBarrierOutcome.NO_PROMOTABLE_CANDIDATE
            ),
            input_summary_hash=input_summary.sha256,
            closed_by=context.verifier.verifier_id,
            closed_at=NOW,
            idempotency_key="formal-search-barrier-repository",
        )
        record = FormalBarrierPersistence(
            context=formal_authority_context_ref(context),
            barrier=barrier,
            holdout_family_hash=promoted_family_hash,
            payload_hash=formal_authority_payload_hash(barrier),
        )
        repository.record_formal_barrier(record)
        repository.record_formal_barrier(record)
        budget_document = round_budget_ledger_document(self.ids["round"], [], [])
        budget_artifact = self.evidence_store.publish(budget_document)
        self.assertEqual(
            budget_artifact.sha256,
            round_budget_ledger_hash(self.ids["round"], [], []),
        )
        with repository.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s",
                (self.ids["round"],),
            ).fetchone()
        assert round_row is not None
        authority = SearchRound.model_validate(
            {name: round_row[name] for name in SearchRound.model_fields}
        )
        return repository, context, authority, barrier, budget_artifact.sha256

    def _build_zero_bundle(
        self,
        context,
        authority: SearchRound,
        barrier: RoundBarrierResult,
        budget_hash: str,
    ):
        requirements = formal_round_evidence_requirements(
            context=context,
            round_authority=authority,
            search_barrier=barrier,
            budget_ledger_hash=budget_hash,
        )
        index = self._publish_formal_index(context, requirements)
        return build_formal_round_evidence(
            context=context,
            round_authority=authority,
            search_barrier=barrier,
            budget_ledger_hash=budget_hash,
            evidence_index_uri=index.uri,
            evidence_index_hash=index.sha256,
            evidence_reader=HashedEvidenceReader(Path(self.temp_dir.name)),
        )

    def _insert_context(self, **updates: object) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
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
                    verifier_hash, sealed_by, sealed_at, run_mode, synthetic,
                    automatic_release_allowed
                ) VALUES (
                    %(context)s, %(context_hash)s, %(round)s, %(task)s,
                    %(target)s, %(stage0)s, %(stage0_hash)s, %(baseline)s,
                    %(hotspot)s, %(target_profile)s, %(workload_profile)s,
                    %(measurement_profile)s, %(candidate_family)s,
                    %(artifact_family)s, %(search_plan)s, %(holdout_commitment)s,
                    'd-holdout-authority-v1', %(holdout_authority_hash)s,
                    %(selection_rule)s, 'formal-evidence-v1', 1,
                    %(store_hash)s, %(access_hash)s, 'm2-d-verifier',
                    'm2-d-formal-v1', %(verifier_hash)s, 'operator-a', %(sealed_at)s,
                    'formal', %(synthetic)s, %(auto_release)s
                )
                """,
                {
                    **self._context_params(**updates),
                    "target_profile": _hash("f"),
                    "workload_profile": _hash("0"),
                    "measurement_profile": _hash("2"),
                    "store_hash": _hash("3"),
                    "access_hash": _hash("4"),
                    "verifier_hash": _hash("5"),
                    "sealed_at": NOW,
                },
            )
        self.connection.commit()

    def _insert_search_barrier(
        self,
        *,
        barrier_id: UUID | None = None,
        family_hash: str | None = None,
        outcome: str = "members_promoted",
        idempotency_key: str = "formal-search-barrier",
    ) -> UUID:
        barrier_id = barrier_id or self.ids["search_barrier"]
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE search_rounds
                SET state = 'search_barrier', holdout_family_hash = %s
                WHERE round_id = %s
                """,
                (
                    _hash("9") if outcome == "members_promoted" else None,
                    self.ids["round"],
                ),
            )
            cursor.execute(
                """
                INSERT INTO formal_round_barriers (
                    barrier_id, round_id, authority_context_id,
                    authority_context_hash, phase, input_family_hash,
                    holdout_family_hash, input_summary_hash,
                    rule_version, rule_hash, outcome,
                    expected_member_count, payload, payload_hash, idempotency_key,
                    closed_by, closed_at
                ) VALUES (
                    %s, %s, %s, %s, 'search', %s, %s, %s, 'm2-search-v1', %s,
                    %s, 2, '{}'::jsonb, %s, %s, 'verifier-d', %s
                )
                """,
                (
                    barrier_id,
                    self.ids["round"],
                    self.ids["context"],
                    _hash("e"),
                    family_hash or _hash("d"),
                    _hash("9") if outcome == "members_promoted" else None,
                    _hash("6"),
                    _hash("7"),
                    outcome,
                    _hash("8"),
                    idempotency_key,
                    NOW,
                ),
            )
        self.connection.commit()
        return barrier_id

    def _freeze_reveal_on_round(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE search_rounds
                SET state = 'search_barrier', holdout_family_hash = %s,
                    holdout_plan_hash = %s, holdout_reveal_lease_id = %s,
                    holdout_reveal_evidence_hash = %s
                WHERE round_id = %s
                """,
                (
                    _hash("9"),
                    _hash("a"),
                    self.ids["reveal_lease"],
                    _hash("b"),
                    self.ids["round"],
                ),
            )
        self.connection.commit()

    def _insert_reveal(self, *, fencing_token: int = 7, uri: str | None = None) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO formal_round_holdout_reveals (
                    reveal_lease_id, round_id, authority_context_id,
                    authority_context_hash, search_barrier_id, fencing_token,
                    holdout_family_hash, holdout_plan_hash, reveal_evidence_uri,
                    reveal_evidence_hash, revealed_by, revealed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'verifier-d', %s)
                """,
                (
                    self.ids["reveal_lease"],
                    self.ids["round"],
                    self.ids["context"],
                    _hash("e"),
                    self.ids["search_barrier"],
                    fencing_token,
                    _hash("9"),
                    _hash("a"),
                    uri or "file:///protected/m2/reveal.json",
                    _hash("b"),
                    NOW,
                ),
            )
        self.connection.commit()

    def _insert_holdout_barrier(self, *, family_hash: str | None = None) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO formal_round_barriers (
                    barrier_id, round_id, authority_context_id,
                    authority_context_hash, phase, parent_search_barrier_id,
                    input_family_hash, holdout_family_hash, input_summary_hash,
                    rule_version, rule_hash, outcome, expected_member_count,
                    payload, payload_hash, idempotency_key, closed_by, closed_at
                ) VALUES (
                    %s, %s, %s, %s, 'holdout', %s, %s, %s, %s,
                    'm2-holdout-v1', %s, 'completed', 1, '{}'::jsonb,
                    %s, 'formal-holdout-barrier', 'verifier-d', %s
                )
                """,
                (
                    self.ids["holdout_barrier"],
                    self.ids["round"],
                    self.ids["context"],
                    _hash("e"),
                    self.ids["search_barrier"],
                    family_hash or _hash("9"),
                    family_hash or _hash("9"),
                    _hash("c"),
                    _hash("d"),
                    _hash("e"),
                    NOW,
                ),
            )
        self.connection.commit()

    def _insert_fwer(self, *, family_hash: str | None = None) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO formal_multiple_comparison_results (
                    multiple_comparison_id, round_id, authority_context_id,
                    authority_context_hash, holdout_barrier_id,
                    holdout_family_hash, protocol_version, protocol_hash,
                    result_hash, payload, payload_hash, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'm2-bonferroni-v1', %s,
                    %s, '{}'::jsonb, %s, %s
                )
                """,
                (
                    self.ids["fwer"],
                    self.ids["round"],
                    self.ids["context"],
                    _hash("e"),
                    self.ids["holdout_barrier"],
                    family_hash or _hash("9"),
                    _hash("f"),
                    _hash("0"),
                    _hash("1"),
                    NOW,
                ),
            )
        self.connection.commit()

    def _insert_evidence(self, **updates: object) -> None:
        values: dict[str, object] = {
            "bundle": self.ids["bundle"],
            "round": self.ids["round"],
            "task": self.ids["round_task"],
            "context": self.ids["context"],
            "context_hash": _hash("e"),
            "store_id": "formal-evidence-v1",
            "store_hash": _hash("3"),
            "candidate_family": _hash("c"),
            "artifact_family": _hash("d"),
            "holdout_family": _hash("9"),
            "search_plan": _hash("8"),
            "holdout_commitment": _hash("9"),
            "holdout_plan": _hash("a"),
            "reveal_hash": _hash("b"),
        }
        values.update(updates)
        with self.connection.cursor() as cursor:
            cursor.execute(
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
                    payload, payload_hash, created_at
                ) VALUES (
                    %(bundle)s, %(round)s, %(task)s, %(context)s,
                    %(context_hash)s, %(store_id)s, %(store_hash)s,
                    'holdout_completed', %(candidate_family)s,
                    %(artifact_family)s, %(holdout_family)s, %(search_plan)s,
                    %(holdout_commitment)s, %(holdout_plan)s, %(reveal_hash)s,
                    %(search_barrier)s, %(holdout_barrier)s, %(fwer)s,
                    'file:///protected/m2/evidence-index.json', %(index_hash)s,
                    %(budget_hash)s, '{}'::jsonb, %(payload_hash)s, %(created_at)s
                )
                """,
                {
                    **values,
                    "search_barrier": self.ids["search_barrier"],
                    "holdout_barrier": self.ids["holdout_barrier"],
                    "fwer": self.ids["fwer"],
                    "index_hash": _hash("2"),
                    "budget_hash": _hash("3"),
                    "payload_hash": _hash("4"),
                    "created_at": NOW,
                },
            )
        self.connection.commit()

    def _prepare_full_holdout_chain(self) -> None:
        self._insert_context()
        self._insert_search_barrier()
        self._freeze_reveal_on_round()
        self._insert_reveal()
        self._insert_holdout_barrier()
        self._insert_fwer()

    def _assert_rejected(self, sql: str, params: tuple[object, ...] = ()) -> None:
        assert psycopg is not None
        with self.assertRaises(psycopg.Error):
            with self.connection.transaction():
                self.connection.execute(sql, params)

    def test_complete_formal_parent_is_accepted_and_then_identity_is_frozen(self) -> None:
        self._insert_context()

        self._assert_rejected(
            "UPDATE search_rounds SET search_plan_hash = %s WHERE round_id = %s",
            (_hash("0"), self.ids["round"]),
        )

    def test_scripted_round_cannot_create_formal_context(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE search_rounds
                SET run_mode = 'scripted', project_mode = NULL
                WHERE round_id = %s
                """,
                (self.ids["round"],),
            )
        self.connection.commit()

        with self.assertRaises(psycopg.Error):
            self._insert_context()
        self.connection.rollback()

    def test_fake_formal_round_bound_to_dry_run_is_rejected(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE stage0_runs SET mode = 'dry_run' WHERE stage0_run_id = %s",
                (self.ids["stage0_run"],),
            )
            cursor.execute(
                "UPDATE tasks SET stage0_authority = 'synthetic' WHERE task_id = %s",
                (self.ids["stage0_task"],),
            )
        self.connection.commit()

        with self.assertRaises(psycopg.Error):
            self._insert_context()
        self.connection.rollback()

    def test_formal_context_rejects_synthetic_or_auto_release(self) -> None:
        for field in ("synthetic", "auto_release"):
            with self.subTest(field=field):
                with self.assertRaises(psycopg.Error):
                    self._insert_context(**{field: True})
                self.connection.rollback()

    def test_search_barrier_rejects_family_drift(self) -> None:
        self._insert_context()

        with self.assertRaises(psycopg.Error):
            self._insert_search_barrier(family_hash=_hash("0"))
        self.connection.rollback()

    def test_holdout_barrier_requires_the_frozen_search_parent(self) -> None:
        self._insert_context()
        self._insert_search_barrier()
        self._freeze_reveal_on_round()

        self._assert_rejected(
            """
            INSERT INTO formal_round_barriers (
                barrier_id, round_id, authority_context_id,
                authority_context_hash, phase, parent_search_barrier_id,
                input_family_hash, holdout_family_hash, input_summary_hash,
                rule_version, rule_hash, outcome, expected_member_count,
                payload, payload_hash, idempotency_key, closed_by, closed_at
            ) VALUES (
                %s, %s, %s, %s, 'holdout', %s, %s, %s, %s,
                'm2-holdout-v1', %s, 'completed', 1, '{}'::jsonb,
                %s, 'bad-holdout-parent', 'verifier-d', %s
            )
            """,
            (
                self.ids["holdout_barrier"],
                self.ids["round"],
                self.ids["context"],
                _hash("e"),
                uuid4(),
                _hash("9"),
                _hash("9"),
                _hash("c"),
                _hash("d"),
                _hash("e"),
                NOW,
            ),
        )

    def test_reveal_rejects_invalid_fencing_or_remote_uri(self) -> None:
        self._insert_context()
        self._insert_search_barrier()
        self._freeze_reveal_on_round()

        cases = (
            (0, "file:///protected/m2/reveal.json"),
            (7, "https://remote/reveal.json"),
        )
        for fencing_token, uri in cases:
            with self.subTest(fencing_token=fencing_token, uri=uri):
                with self.assertRaises(psycopg.Error):
                    self._insert_reveal(fencing_token=fencing_token, uri=uri)
                self.connection.rollback()

    def test_fwer_rejects_holdout_family_drift(self) -> None:
        self._insert_context()
        self._insert_search_barrier()
        self._freeze_reveal_on_round()
        self._insert_reveal()
        self._insert_holdout_barrier()

        with self.assertRaises(psycopg.Error):
            self._insert_fwer(family_hash=_hash("0"))
        self.connection.rollback()

    def test_evidence_bundle_rejects_context_store_or_family_drift(self) -> None:
        self._prepare_full_holdout_chain()

        cases = (
            ("context_hash", _hash("0")),
            ("store_id", "other-evidence-store"),
            ("candidate_family", _hash("0")),
            ("artifact_family", _hash("0")),
            ("holdout_family", _hash("0")),
        )
        for field, value in cases:
            with self.subTest(field=field):
                with self.assertRaises(psycopg.Error):
                    self._insert_evidence(**{field: value})
                self.connection.rollback()

    def test_all_formal_authority_rows_are_append_only(self) -> None:
        self._prepare_full_holdout_chain()
        self._insert_evidence()
        identities = (
            ("formal_round_authority_contexts", "authority_context_id", self.ids["context"]),
            ("formal_round_barriers", "barrier_id", self.ids["search_barrier"]),
            ("formal_round_holdout_reveals", "reveal_lease_id", self.ids["reveal_lease"]),
            (
                "formal_multiple_comparison_results",
                "multiple_comparison_id",
                self.ids["fwer"],
            ),
            ("formal_round_evidence_bundles", "round_evidence_bundle_id", self.ids["bundle"]),
        )
        for table, key, value in identities:
            self._assert_rejected(
                f"UPDATE {table} SET created_at = created_at WHERE {key} = %s",
                (value,),
            )
            self._assert_rejected(f"DELETE FROM {table} WHERE {key} = %s", (value,))

    def test_concurrent_search_barrier_insert_has_one_winner(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        self._insert_context()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE search_rounds
                SET state = 'search_barrier', holdout_family_hash = %s
                WHERE round_id = %s
                """,
                (_hash("9"), self.ids["round"]),
            )
        self.connection.commit()

        def insert_once(_: int) -> bool:
            try:
                with psycopg.connect(DATABASE_URL) as connection:
                    connection.execute(
                        """
                        INSERT INTO formal_round_barriers (
                            barrier_id, round_id, authority_context_id,
                            authority_context_hash, phase, input_family_hash,
                            holdout_family_hash, input_summary_hash,
                            rule_version, rule_hash, outcome,
                            expected_member_count, payload, payload_hash,
                            idempotency_key, closed_by, closed_at
                        ) VALUES (
                            %s, %s, %s, %s, 'search', %s, %s, %s, 'm2-search-v1',
                            %s, 'members_promoted', 2, '{}'::jsonb, %s, %s,
                            'verifier-d', %s
                        )
                        """,
                        (
                            uuid4(),
                            self.ids["round"],
                            self.ids["context"],
                            _hash("e"),
                            _hash("d"),
                            _hash("9"),
                            _hash("6"),
                            _hash("7"),
                            _hash("8"),
                            f"concurrent-search-{uuid4()}",
                            NOW,
                        ),
                    )
                return True
            except psycopg.Error:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(insert_once, range(2)))

        self.assertEqual(sorted(results), [False, True])
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM formal_round_barriers WHERE round_id = %s",
                (self.ids["round"],),
            )
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_repository_zero_promotion_finalizer_is_idempotent_and_concurrent(self) -> None:
        repository, context, authority, barrier, budget_hash = (
            self._prepare_repository_zero_promotion()
        )
        bundle = self._build_zero_bundle(context, authority, barrier, budget_hash)
        record = FormalEvidenceBundlePersistence(
            context=formal_authority_context_ref(context),
            bundle=bundle,
            payload_hash=formal_authority_payload_hash(bundle),
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(
                pool.map(
                    lambda _: repository.finalize_formal_search_round(record), range(2)
                )
            )

        self.assertTrue(all(row["state"] == "awaiting_signoff" for row in rows))
        replay = repository.finalize_formal_search_round(record)
        self.assertEqual(replay["state"], "awaiting_signoff")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM tasks WHERE task_id = %s",
                (self.ids["round_task"],),
            )
            self.assertEqual(cursor.fetchone()[0], "awaiting_signoff")
            cursor.execute(
                "SELECT count(*) FROM formal_round_evidence_bundles WHERE round_id = %s",
                (self.ids["round"],),
            )
            self.assertEqual(cursor.fetchone()[0], 1)
            cursor.execute(
                """
                SELECT count(*) FROM task_events
                WHERE task_id = %s AND event_type = 'm2_formal_round_awaiting_signoff'
                """,
                (self.ids["round_task"],),
            )
            self.assertEqual(cursor.fetchone()[0], 1)
        self.evidence_store.tamper(context.search_plan_hash)
        with self.assertRaisesRegex(Conflict, "evidence_hash_mismatch"):
            repository.finalize_formal_search_round(record)

    def test_repository_completed_holdout_path_stops_at_signoff(self) -> None:
        repository, context, authority, search, budget_hash = (
            self._prepare_repository_zero_promotion(promote=True)
        )
        promoted = next(
            item
            for item in search.members
            if item.candidate_id == search.promoted_candidate_ids[0]
        )
        family_hash = holdout_family_hash(
            round_authority=authority,
            members=(promoted,),
        )
        plan = self.evidence_store.publish({"phase": "holdout", "plan": True})
        reveal_evidence = self.evidence_store.publish(
            {"phase": "holdout", "revealed": True}
        )
        reveal = FormalHoldoutRevealPersistence(
            context=formal_authority_context_ref(context),
            search_barrier_id=search.barrier_id,
            reveal_lease_id=self.ids["reveal_lease"],
            fencing_token=7,
            holdout_family_hash=family_hash,
            holdout_plan_hash=plan.sha256,
            reveal_evidence_uri=reveal_evidence.uri,
            reveal_evidence_hash=reveal_evidence.sha256,
            revealed_by=context.verifier.verifier_id,
            revealed_at=NOW,
        )
        repository.record_formal_holdout_reveal(reveal)
        repository.record_formal_holdout_reveal(reveal)
        holdout_budget = self.evidence_store.publish({"phase": "holdout", "budget": True})
        holdout_cleanup = self.evidence_store.publish(
            {"phase": "holdout", "cleanup": "complete"}
        )
        holdout_member = promoted.model_copy(
            update={
                "candidate_state": RoundCandidateState.HOLDOUT_MEASURED,
                "round_measurement_ref_id": uuid4(),
                "budget_usage_evidence_hash": holdout_budget.sha256,
                "cleanup_evidence_hash": holdout_cleanup.sha256,
            }
        )
        holdout = RoundBarrierResult(
            barrier_id=self.ids["holdout_barrier"],
            round_id=self.ids["round"],
            run_mode=SearchRoundRunMode.FORMAL,
            synthetic=False,
            phase=RoundPhase.HOLDOUT,
            input_family_hash=family_hash,
            expected_member_count=1,
            members=(holdout_member,),
            rule_version="m2-holdout-v1",
            rule_hash=context.selection_rule_hash,
            promoted_candidate_ids=(),
            outcome=RoundBarrierOutcome.COMPLETED,
            input_summary_hash=self.evidence_store.publish(
                {"phase": "holdout", "summary": True}
            ).sha256,
            closed_by=context.verifier.verifier_id,
            closed_at=NOW,
            idempotency_key="formal-holdout-barrier-repository",
        )
        holdout_record = FormalBarrierPersistence(
            context=formal_authority_context_ref(context),
            barrier=holdout,
            holdout_family_hash=family_hash,
            parent_search_barrier_id=search.barrier_id,
            payload_hash=formal_authority_payload_hash(holdout),
        )
        repository.record_formal_barrier(holdout_record)
        repository.record_formal_barrier(holdout_record)
        raw = self.evidence_store.publish({"phase": "holdout", "raw": True})
        baseline = self.evidence_store.publish({"phase": "holdout", "baseline": True})
        adjusted = AdjustedCandidateResult(
            candidate_id=holdout_member.candidate_id,
            round_measurement_ref_id=holdout_member.round_measurement_ref_id,
            synthetic=False,
            correctness_evidence_hash=holdout_member.correctness_evidence_hash,
            raw_evidence_hash=raw.sha256,
            baseline_sample_set_hash=baseline.sha256,
            verdict=ManualCandidateVerdict.INCONCLUSIVE,
            adjusted_ci_lower=-0.01,
            adjusted_ci_upper=0.01,
            stage0_mde_ratio=0.02,
            workload_mde_ratio=0.02,
            credible_threshold=0.02,
        )
        comparison = MultipleComparisonResult(
            multiple_comparison_id=self.ids["fwer"],
            round_id=self.ids["round"],
            run_mode=SearchRoundRunMode.FORMAL,
            synthetic=False,
            holdout_barrier_id=holdout.barrier_id,
            holdout_family_hash=family_hash,
            protocol_version=M2_FWER_PROTOCOL_VERSION,
            protocol_hash=m2_fwer_protocol_hash(),
            family_alpha=0.05,
            m=1,
            alpha_candidate=0.05,
            candidate_results=(adjusted,),
            result_hash=_hash("0"),
            created_at=NOW,
        )
        comparison = comparison.model_copy(
            update={"result_hash": recompute_multiple_comparison_result_hash(comparison)}
        )
        comparison_record = FormalMultipleComparisonPersistence(
            context=formal_authority_context_ref(context),
            result=comparison,
            payload_hash=formal_authority_payload_hash(comparison),
        )
        repository.record_formal_multiple_comparison(comparison_record)
        repository.record_formal_multiple_comparison(comparison_record)
        with repository.connection() as connection:
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s",
                (self.ids["round"],),
            ).fetchone()
        assert round_row is not None
        authority = SearchRound.model_validate(
            {name: round_row[name] for name in SearchRound.model_fields}
        )
        requirements = formal_round_evidence_requirements(
            context=context,
            round_authority=authority,
            search_barrier=search,
            budget_ledger_hash=budget_hash,
            holdout_reveal=reveal,
            holdout_barrier=holdout,
            multiple_comparison=comparison,
        )
        index = self._publish_formal_index(context, requirements)
        bundle = build_formal_round_evidence(
            context=context,
            round_authority=authority,
            search_barrier=search,
            budget_ledger_hash=budget_hash,
            evidence_index_uri=index.uri,
            evidence_index_hash=index.sha256,
            evidence_reader=HashedEvidenceReader(Path(self.temp_dir.name)),
            holdout_reveal=reveal,
            holdout_barrier=holdout,
            multiple_comparison=comparison,
        )
        bundle_record = FormalEvidenceBundlePersistence(
            context=formal_authority_context_ref(context),
            bundle=bundle,
            payload_hash=formal_authority_payload_hash(bundle),
        )

        finalized = repository.finalize_formal_search_round(bundle_record)

        self.assertEqual(finalized["state"], "awaiting_signoff")
        self.assertFalse(bundle.synthetic)
        self.assertFalse(bundle.automatic_release_allowed)
        self.assertEqual(
            bundle.summary["performance_conclusion"], "formal_single_operation_only"
        )

    def test_repository_formal_finalizer_rejects_reserved_budget_and_tampering(self) -> None:
        repository, context, authority, barrier, budget_hash = (
            self._prepare_repository_zero_promotion()
        )
        bundle = self._build_zero_bundle(context, authority, barrier, budget_hash)
        record = FormalEvidenceBundlePersistence(
            context=formal_authority_context_ref(context),
            bundle=bundle,
            payload_hash=formal_authority_payload_hash(bundle),
        )
        job_id = uuid4()
        reservation_id = uuid4()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jobs (
                    job_id, task_id, job_type, state, payload,
                    accepted_worker_type, adapter_profile, lease_scope,
                    idempotency_key
                ) VALUES (
                    %s, %s, 'performance', 'queued', '{}'::jsonb,
                    'gpu', 'formal-adapter-v1', 'exclusive',
                    'formal-reserved-budget-job'
                )
                """,
                (job_id, self.ids["round_task"]),
            )
            cursor.execute(
                """
                INSERT INTO round_budget_reservations (
                    reservation_id, round_id, job_id, attempt, candidate_id,
                    phase, planned, state, idempotency_key
                ) VALUES (
                    %s, %s, %s, 1, NULL, 'search', '{}'::jsonb,
                    'reserved', 'formal-reserved-budget'
                )
                """,
                (reservation_id, self.ids["round"], job_id),
            )
        self.connection.commit()

        with self.assertRaisesRegex(Conflict, "every Budget reservation terminal"):
            repository.finalize_formal_search_round(record)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM round_budget_reservations WHERE reservation_id = %s",
                (reservation_id,),
            )
            cursor.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        self.connection.commit()
        self.evidence_store.tamper(context.search_plan_hash)
        with self.assertRaisesRegex(Conflict, "evidence_hash_mismatch"):
            repository.finalize_formal_search_round(record)
