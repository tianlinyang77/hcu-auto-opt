# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import hashlib
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4, uuid5

import pytest

from hcuopt.adapters.m2_candidate import (
    ScriptedCandidateIntake,
    candidate_source_package_hash,
)
from hcuopt.adapters.manual_candidate import CandidateSourcePackageStore
from hcuopt.contracts.m1 import CandidateSourcePackageManifest
from hcuopt.contracts.operator_v1 import (
    OperatorProfileRef,
    OperatorRoundStartRequest,
    RoundPlanPreviewRequest,
    TargetOperatorProfileRefs,
    WorkloadOperatorProfileRefs,
)
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator import (
    HmacScriptedPlanAuthority,
    OperatorPlanCompiler,
    OperatorStartCoordinator,
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
OVERLAY_PATH = "sglang/fixture_kernel.py"
MOUNT_TARGET = "/opt/hcuopt/overlay/sglang/fixture_kernel.py"


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
        self.temporary_directory = TemporaryDirectory()
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
        self.temporary_directory.cleanup()

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

    def _publish_package(self, ordinal: int) -> dict[str, str]:
        candidate_id = uuid5(self.ids["hotspot_id"], f"operator-candidate-{ordinal}")
        candidate_source_hash = _hash(f"operator-candidate-source-{ordinal}")
        content = f"CANDIDATE = '{candidate_id}'\n".encode()
        content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        manifest = CandidateSourcePackageManifest(
            candidate_id=candidate_id,
            hotspot_id=self.ids["hotspot_id"],
            baseline_source_hash=_hash("baseline-source"),
            candidate_source_hash=candidate_source_hash,
            replacement_point=REPLACEMENT_POINT,
            candidate_kind="fixture",
            overlay_mount_target=MOUNT_TARGET,
            files=[{"path": OVERLAY_PATH, "content_hash": content_hash}],
            profiler_evidence_uri=PROFILER_URI,
            profiler_evidence_hash=_hash("profiler"),
            reviewed_by="operator-scripted-fixture",
            reviewed_at=datetime.now(timezone.utc),
        )
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_hash = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        package_hash = candidate_source_package_hash(manifest_hash, manifest.files)
        digest = candidate_source_hash.removeprefix("sha256:")
        package_root = (
            Path(self.temporary_directory.name) / "sha256" / digest[:2] / digest[2:]
        )
        source = package_root / "files" / OVERLAY_PATH
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        (package_root / "manifest.json").write_bytes(manifest_bytes)
        return {
            "candidate_source_hash": candidate_source_hash,
            "source_package_hash": package_hash,
            "manifest_hash": manifest_hash,
            "manifest_schema_version": "m1-candidate-source-v1",
        }

    def _startable_suite(self):  # type: ignore[no-untyped-def]
        identity = build_operator_service_identity(
            source_commit="a" * 40,
            server_instance_id=UUID("00000000-0000-0000-0000-000000000087"),
            catalog=self.catalog,
        )
        store = CandidateSourcePackageStore(
            Path(self.temporary_directory.name),
            profile="m2-scripted-v1",
            allowed_overlay_roots=("sglang",),
            approved_mount_targets={REPLACEMENT_POINT: MOUNT_TARGET},
        )
        intake = ScriptedCandidateIntake(
            store,
            store_id=self.target.candidate_package_store_id,
            store_hash=self.target.candidate_package_store_hash,
        )
        compiler = OperatorPlanCompiler(
            self.catalog,
            identity,
            candidate_intake=intake,
        )
        payload = self._request().model_dump(mode="json")
        payload["candidates"] = [
            {
                "ordinal": ordinal,
                "source_package_ref": self._publish_package(ordinal),
                "optimization_intent": f"exercise Candidate {ordinal}",
            }
            for ordinal in range(2)
        ]
        payload["expected_service_identity"] = identity.model_dump(mode="json")
        request = RoundPlanPreviewRequest.model_validate(payload)
        preview = compiler.compile(request, self.repository)
        self.assertTrue(preview.start_allowed)
        preview = self.repository.create_operator_plan_preview(
            request.idempotency_key, preview
        )
        coordinator = OperatorStartCoordinator(
            compiler,
            plan_authority=HmacScriptedPlanAuthority(b"postgres-operator-secret-32-bytes!!!!"),
        )
        start = OperatorRoundStartRequest(
            preview_id=preview.preview_id,
            resolved_plan_hash=preview.resolved_plan_hash,
            actor="postgres-operator",
            idempotency_key="operator-start-postgres",
            acknowledged_warning_codes=preview.required_ack_codes,
            expected_service_identity=identity.model_dump(mode="json"),
        )
        return coordinator, start

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

    def test_start_intent_finalizes_and_concurrent_replay_converges(self) -> None:
        coordinator, start = self._startable_suite()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _index: coordinator.start(start, self.repository),
                    range(2),
                )
            )

        self.assertTrue(all(item.state == "finalized" for item in results))
        self.assertEqual({item.intent_id for item in results}, {results[0].intent_id})
        self.assertEqual({item.replayed for item in results}, {False, True})
        stored = self.repository.get_operator_start_intent(results[0].intent_id)
        self.assertEqual(stored.state, "finalized")
        round_row = self.repository.get_search_round(stored.round_id)
        self.assertEqual(round_row["state"], "intake_closed")
        with self.connection.cursor() as cursor:
            candidate_count = cursor.execute(
                "SELECT count(*) AS count FROM round_candidates WHERE round_id = %s",
                (stored.round_id,),
            ).fetchone()[0]
        self.assertEqual(candidate_count, 2)

    def test_start_intent_recovers_after_candidate_insert_before_progress(self) -> None:
        coordinator, start = self._startable_suite()

        class FailAfterFirstCandidate:
            def __init__(self, repository):  # type: ignore[no-untyped-def]
                self.repository = repository
                self.failed = False

            def __getattr__(self, name):  # type: ignore[no-untyped-def]
                return getattr(self.repository, name)

            def add_round_candidate(self, request):  # type: ignore[no-untyped-def]
                result = self.repository.add_round_candidate(request)
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("injected crash after durable Candidate insert")
                return result

        wrapper = FailAfterFirstCandidate(self.repository)
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            coordinator.start(start, wrapper)
        intent_id = uuid5(
            UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8"),
            f"hcuopt:operator-start:{start.idempotency_key}",
        )
        interrupted = self.repository.get_operator_start_intent(intent_id)
        self.assertEqual(interrupted.state, "round_created")
        self.assertEqual(interrupted.candidate_members[0].state, "pending")

        recovered = coordinator.reconcile(intent_id, self.repository)

        self.assertEqual(recovered.state, "finalized")
        self.assertTrue(
            all(
                member.state == "round_member_bound"
                for member in recovered.candidate_members
            )
        )
