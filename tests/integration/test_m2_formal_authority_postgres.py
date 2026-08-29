# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

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


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class M2FormalAuthorityPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        assert Jsonb is not None
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
                INSERT INTO formal_round_barriers (
                    barrier_id, round_id, authority_context_id,
                    authority_context_hash, phase, input_family_hash,
                    input_summary_hash, rule_version, rule_hash, outcome,
                    expected_member_count, payload, payload_hash, idempotency_key,
                    closed_by, closed_at
                ) VALUES (
                    %s, %s, %s, %s, 'search', %s, %s, 'm2-search-v1', %s,
                    %s, 2, '{}'::jsonb, %s, %s, 'verifier-d', %s
                )
                """,
                (
                    barrier_id,
                    self.ids["round"],
                    self.ids["context"],
                    _hash("e"),
                    family_hash or _hash("d"),
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

        def insert_once(_: int) -> bool:
            try:
                with psycopg.connect(DATABASE_URL) as connection:
                    connection.execute(
                        """
                        INSERT INTO formal_round_barriers (
                            barrier_id, round_id, authority_context_id,
                            authority_context_hash, phase, input_family_hash,
                            input_summary_hash, rule_version, rule_hash, outcome,
                            expected_member_count, payload, payload_hash,
                            idempotency_key, closed_by, closed_at
                        ) VALUES (
                            %s, %s, %s, %s, 'search', %s, %s, 'm2-search-v1',
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
