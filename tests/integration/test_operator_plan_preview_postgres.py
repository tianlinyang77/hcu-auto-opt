# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.operator_v1 import (
    OperatorProfileRef,
    RoundPlanPreviewRequest,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.operator import (
    OperatorPlanCompiler,
    build_operator_service_identity,
    build_scripted_operator_profile_catalog,
)
from hcuopt.operator.errors import OperatorPlanHashMismatch
from hcuopt.storage.repository import PostgresRepository

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - optional until dev dependencies are installed
    psycopg = None
    Jsonb = None


DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
PROFILER_URI = "fixture:///operator/profiler.json"
CORRECTNESS_URI = "fixture:///operator/correctness.json"
REPLACEMENT_POINT = "sglang.fixture.layer_norm"


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _profile_ref(profile) -> OperatorProfileRef:  # type: ignore[no-untyped-def]
    return OperatorProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        profile_kind=profile.profile_kind,
        profile_hash=profile.profile_hash,
    )


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class OperatorPlanPreviewPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        assert Jsonb is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.catalog = build_scripted_operator_profile_catalog()
        self.profiles = {item.profile_kind: item for item in self.catalog.list()}
        self.target = TargetOperatorProfileRefs.model_validate(
            self.profiles["target"].authority_refs
        )
        self.workload = WorkloadOperatorProfileRefs.model_validate(
            self.profiles["workload"].authority_refs
        )
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE operator_plan_previews, tasks, target_snapshots
                RESTART IDENTITY CASCADE
                """
            )
        self.connection.commit()
        self.ids = self._create_authority()

    def tearDown(self) -> None:
        self.connection.close()

    def _create_authority(self) -> dict[str, UUID]:
        ids = {
            "task_id": uuid4(),
            "target_snapshot_id": uuid4(),
            "stage0_run_id": uuid4(),
            "source_snapshot_id": uuid4(),
            "baseline_epoch_id": uuid4(),
            "hotspot_id": uuid4(),
        }
        now = datetime.now(timezone.utc)
        hotspot_evidence = {
            "profiler_raw_output_uri": PROFILER_URI,
            "profiler_raw_output_hash": _hash("profiler"),
            "correctness_spec_uri": CORRECTNESS_URI,
            "correctness_spec_hash": _hash("correctness"),
            "replacement_point": REPLACEMENT_POINT,
            "shape": [1, 128],
            "dtype": "float16",
        }
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO target_snapshots (
                    target_snapshot_id, target_id, target_fingerprint,
                    specification, source_path
                ) VALUES (%s, %s, %s, %s, 'fixture://operator-target')
                """,
                (
                    ids["target_snapshot_id"],
                    self.target.target_id,
                    self.target.target_spec_hash,
                    Jsonb({"synthetic": True}),
                ),
            )
            cursor.execute(
                """
                INSERT INTO tasks (
                    task_id, name, workload_id, idempotency_key, state, budget,
                    automatic_release_allowed, workflow_type, target_id,
                    target_snapshot_id, adapter_profile, stage0_authority
                ) VALUES (
                    %s, 'Operator scripted Stage 0', %s,
                    'operator-preview-stage0-task', 'completed', '{}'::jsonb,
                    FALSE, 'stage0', %s, %s, %s, 'synthetic'
                )
                """,
                (
                    ids["task_id"],
                    self.workload.workload_id,
                    self.target.target_id,
                    ids["target_snapshot_id"],
                    self.target.adapter_profile,
                ),
            )
            cursor.execute(
                """
                INSERT INTO stage0_runs (
                    stage0_run_id, task_id, target_snapshot_id, adapter_profile,
                    mode, state, protocol_version, idempotency_key, report,
                    created_at, finalized_at
                ) VALUES (
                    %s, %s, %s, %s, 'dry_run', 'finalized',
                    'operator-scripted-stage0-v1', 'operator-preview-stage0-run',
                    %s, %s, %s
                )
                """,
                (
                    ids["stage0_run_id"],
                    ids["task_id"],
                    ids["target_snapshot_id"],
                    self.target.adapter_profile,
                    Jsonb({"synthetic": True}),
                    now,
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO source_snapshots (
                    snapshot_id, task_id, kind, repository, commit, tree_hash,
                    source_hash, worktree_uri, clean, idempotency_key,
                    adapter_provenance, synthetic, created_at
                ) VALUES (
                    %s, %s, 'baseline', 'fixture://operator-source',
                    'fixture-commit', %s, %s, 'fixture://operator-worktree',
                    TRUE, 'operator-preview-baseline-source', %s, TRUE, %s
                )
                """,
                (
                    ids["source_snapshot_id"],
                    ids["task_id"],
                    _hash("tree"),
                    _hash("baseline-source"),
                    Jsonb([{"implementation_kind": "scripted"}]),
                    now,
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
                    %s, %s, 'operator-hardware', 'operator-software', %s, %s,
                    TRUE, 'search_round', %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    ids["baseline_epoch_id"],
                    ids["task_id"],
                    self.workload.workload_id,
                    self.workload.configuration_hash,
                    ids["target_snapshot_id"],
                    ids["stage0_run_id"],
                    self.target.required_stage0_protocol_hash,
                    ids["source_snapshot_id"],
                    self.workload.workload_hash,
                    _hash("image"),
                    self.target.adapter_profile,
                ),
            )
            cursor.execute(
                """
                INSERT INTO hotspots (
                    hotspot_id, task_id, baseline_epoch_id, symbol, share_ratio,
                    opportunity_score, patchability, evidence, candidate_kind,
                    actor, intake_hash, idempotency_key
                ) VALUES (
                    %s, %s, %s, 'sglang.fixture.layer_norm', 0.2, 0.5,
                    'patchable', %s, 'fixture', 'operator-fixture', %s,
                    'operator-preview-hotspot'
                )
                """,
                (
                    ids["hotspot_id"],
                    ids["task_id"],
                    ids["baseline_epoch_id"],
                    Jsonb(hotspot_evidence),
                    _hash("hotspot-intake"),
                ),
            )
        self.connection.commit()
        return ids

    def _request(self) -> RoundPlanPreviewRequest:
        identity = build_operator_service_identity(
            source_commit="a" * 40,
            server_instance_id=UUID("00000000-0000-0000-0000-000000000087"),
            catalog=self.catalog,
        )
        return RoundPlanPreviewRequest(
            name="PostgreSQL Scripted Preview",
            run_mode="scripted",
            target_profile=_profile_ref(self.profiles["target"]),
            workload_profile=_profile_ref(self.profiles["workload"]),
            measurement_profile=_profile_ref(self.profiles["measurement"]),
            hotspot={
                "source": "profiler",
                "hotspot_id": self.ids["hotspot_id"],
                "hotspot_intake_hash": _hash("hotspot-intake"),
                "profiler_evidence_uri": PROFILER_URI,
                "profiler_evidence_hash": _hash("profiler"),
                "correctness_evidence_uri": CORRECTNESS_URI,
                "correctness_evidence_hash": _hash("correctness"),
                "replacement_point": REPLACEMENT_POINT,
                "workload_hash": self.workload.workload_hash,
                "shape": [1, 128],
                "dtype": "float16",
            },
            candidates=(
                {
                    "ordinal": 0,
                    "source_package_ref": {
                        "candidate_source_hash": _hash("candidate-0"),
                        "source_package_hash": _hash("package-0"),
                        "manifest_hash": _hash("manifest-0"),
                        "manifest_schema_version": "m1-candidate-source-v1",
                    },
                    "optimization_intent": "exercise candidate zero",
                },
                {
                    "ordinal": 1,
                    "source_package_ref": {
                        "candidate_source_hash": _hash("candidate-1"),
                        "source_package_hash": _hash("package-1"),
                        "manifest_hash": _hash("manifest-1"),
                        "manifest_schema_version": "m1-candidate-source-v1",
                    },
                    "optimization_intent": "exercise candidate one",
                },
            ),
            max_promoted=1,
            idempotency_key="operator-preview-postgres",
            expected_service_identity=identity.model_dump(mode="json"),
        )

    def test_preview_resolves_authority_and_replays_concurrently(self) -> None:
        request = self._request()
        identity = build_operator_service_identity(
            source_commit="a" * 40,
            server_instance_id=UUID("00000000-0000-0000-0000-000000000087"),
            catalog=self.catalog,
        )
        compiler = OperatorPlanCompiler(self.catalog, identity)
        preview = compiler.compile(request, self.repository)

        self.assertFalse(preview.start_allowed)
        self.assertIsNotNone(preview.resolved_plan.authority)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _index: self.repository.create_operator_plan_preview(
                        request.idempotency_key, preview
                    ),
                    range(2),
                )
            )

        self.assertEqual(results[0], results[1])
        self.assertEqual(
            self.repository.get_operator_plan_preview(preview.preview_id), preview
        )
        self.assertEqual(
            self.repository.get_operator_plan_preview_by_idempotency(
                request.idempotency_key
            ),
            preview,
        )
        with self.assertRaises(OperatorPlanHashMismatch):
            self.repository.create_operator_plan_preview(
                request.idempotency_key,
                preview.model_copy(update={"resolved_plan_hash": _hash("changed")}),
            )

    def test_manual_hotspot_without_bound_intake_evidence_is_blocked(self) -> None:
        request = self._request()
        manual_hotspot = {
            **request.hotspot.model_dump(mode="json"),
            "source": "manual",
            "manual_intake_uri": "fixture:///operator/manual-intake.json",
            "manual_intake_hash": _hash("manual-intake"),
        }
        identity = build_operator_service_identity(
            source_commit="a" * 40,
            server_instance_id=UUID("00000000-0000-0000-0000-000000000087"),
            catalog=self.catalog,
        )
        compiler = OperatorPlanCompiler(
            self.catalog,
            identity,
        )

        preview = compiler.compile(
            RoundPlanPreviewRequest.model_validate(
                {**request.model_dump(mode="json"), "hotspot": manual_hotspot}
            ),
            self.repository,
        )

        self.assertFalse(preview.start_allowed)
        self.assertIsNone(preview.resolved_plan.authority)
        self.assertIn(
            "operator_authority_unavailable",
            {item.code for item in preview.checks},
        )
