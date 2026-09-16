# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in endpoint control-plane checks in an isolated PostgreSQL schema."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from hcuopt.contracts.endpoint_control_v1 import EndpointValidationRunCreate
from hcuopt.contracts.v1 import WorkerRegister
from hcuopt.domain.enums import TaskState, WorkerType
from hcuopt.evaluation.endpoint_workload import load_endpoint_workload_spec
from hcuopt.measurement.endpoint_models import (
    EndpointMeasurementPlan,
    SignedM1EvidenceReference,
)
from hcuopt.storage.repository import PostgresRepository

DSN = os.environ.get("HCUOPT_ENDPOINT_TEST_DATABASE_URL")
ROOT = Path(__file__).parents[2]
PROFILE = "bw20-endpoint-provisional-v1"
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="explicit endpoint-test DSN required"),
]


def _hash(value: int) -> str:
    return f"sha256:{value:064x}"


@pytest.fixture
def endpoint_database():
    schema = "hcuopt_endpoint_" + uuid4().hex
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(DSN, options=f"-c search_path={schema}")
            repository = PostgresRepository(dsn)
            repository.migrate()
            yield repository
        finally:
            # Only the generated test schema is removed; shared project data is untouched.
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_signed_m1(repository: PostgresRepository) -> EndpointValidationRunCreate:
    workload = load_endpoint_workload_spec(
        ROOT / "config/workloads/bw20-sglang-endpoint-provisional-v1.yaml"
    )
    target_snapshot_id = uuid4()
    stage0_task_id = uuid4()
    stage0_run_id = uuid4()
    task_id = uuid4()
    baseline_source_id = uuid4()
    baseline_epoch_id = uuid4()
    candidate_id = uuid4()
    candidate_source_id = uuid4()
    artifact_id = uuid4()
    evaluation_id = uuid4()
    evidence_id = uuid4()
    signoff_id = uuid4()
    round_id = uuid4()
    provenance = [
        {
            "profile": PROFILE,
            "capability": "postgres_fixture",
            "adapter_name": "EndpointPostgresFixture",
            "adapter_version": "1",
            "implementation_kind": "real",
            "source_commit": None,
        }
    ]
    candidate_source_hash = _hash(2)
    artifact_hash = _hash(3)
    target_fingerprint = _hash(1)
    evidence_hash = _hash(4)
    with repository.connection() as connection:
        connection.execute(
            """
            INSERT INTO target_snapshots (
                target_snapshot_id, target_id, target_fingerprint,
                specification, source_path
            ) VALUES (%s, %s, %s, %s, 'fixture://target')
            """,
            (
                target_snapshot_id,
                workload.target_id,
                target_fingerprint,
                Jsonb({"fixture": "endpoint-postgres"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO tasks (
                task_id, name, workload_id, idempotency_key, state,
                automatic_release_allowed, workflow_type, target_id,
                target_snapshot_id, adapter_profile, stage0_authority
            ) VALUES (
                %s, 'Endpoint Stage0 fixture', 'endpoint-stage0-fixture', %s,
                'degraded', FALSE, 'stage0', %s, %s, %s, 'formal'
            )
            """,
            (
                stage0_task_id,
                f"endpoint-stage0:{stage0_task_id}",
                workload.target_id,
                target_snapshot_id,
                PROFILE,
            ),
        )
        connection.execute(
            """
            INSERT INTO stage0_runs (
                stage0_run_id, task_id, target_snapshot_id, adapter_profile,
                mode, state, protocol_version, idempotency_key, report, finalized_at
            ) VALUES (
                %s, %s, %s, %s, 'formal', 'finalized', 's0-g0-v2', %s,
                %s, now()
            )
            """,
            (
                stage0_run_id,
                stage0_task_id,
                target_snapshot_id,
                PROFILE,
                f"endpoint-stage0-run:{stage0_run_id}",
                Jsonb({"mode": "degraded_manual_intake"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO tasks (
                task_id, name, workload_id, idempotency_key, state,
                automatic_release_allowed, workflow_type, target_id,
                target_snapshot_id, adapter_profile, stage0_run_id
            ) VALUES (
                %s, 'Signed M1 fixture', %s, %s, 'completed', FALSE,
                'manual_candidate', %s, %s, %s, %s
            )
            """,
            (
                task_id,
                workload.workload_id,
                f"signed-m1:{task_id}",
                workload.target_id,
                target_snapshot_id,
                PROFILE,
                stage0_run_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO source_snapshots (
                snapshot_id, task_id, kind, repository, commit, tree_hash,
                source_hash, worktree_uri, clean, idempotency_key,
                adapter_provenance, synthetic, created_at
            ) VALUES (
                %s, %s, 'baseline', 'fixture/repository', %s, %s, %s,
                'file:///fixture/baseline', TRUE, %s, %s, FALSE, now()
            )
            """,
            (
                baseline_source_id,
                task_id,
                workload.source_commit,
                "a" * 40,
                _hash(10),
                f"baseline-source:{task_id}",
                Jsonb(provenance),
            ),
        )
        connection.execute(
            """
            INSERT INTO baseline_epochs (
                baseline_epoch_id, task_id, hardware_fingerprint,
                software_fingerprint, workload_id, configuration_hash,
                baseline_kind, target_snapshot_id, stage0_run_id,
                stage0_protocol_hash, source_snapshot_id, workload_hash,
                image_digest, adapter_profile
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'manual_candidate', %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            (
                baseline_epoch_id,
                task_id,
                _hash(11),
                _hash(12),
                workload.workload_id,
                _hash(13),
                target_snapshot_id,
                stage0_run_id,
                _hash(14),
                baseline_source_id,
                workload.workload_hash,
                workload.image_digest,
                PROFILE,
            ),
        )
        connection.execute(
            """
            INSERT INTO candidates (
                candidate_id, task_id, round_id, baseline_epoch_id,
                source_hash, variant, state, ordinal, metadata, track,
                release_mode, candidate_kind, optimization_intent,
                replacement_point, idempotency_key
            ) VALUES (
                %s, %s, %s, %s, %s, 'signed-agent-overlay', 'accepted', 0,
                '{}'::jsonb, 'triton', 'overlay', 'business',
                'signed endpoint fixture', 'sglang.srt.mem_cache.allocator', %s
            )
            """,
            (
                candidate_id,
                task_id,
                round_id,
                baseline_epoch_id,
                candidate_source_hash,
                f"signed-candidate:{candidate_id}",
            ),
        )
        connection.execute(
            """
            INSERT INTO source_snapshots (
                snapshot_id, task_id, candidate_id, kind, repository, commit,
                tree_hash, source_hash, worktree_uri, clean, parent_snapshot_id,
                idempotency_key, adapter_provenance, synthetic, created_at
            ) VALUES (
                %s, %s, %s, 'candidate', 'fixture/repository', %s, %s, %s,
                'file:///fixture/candidate', TRUE, %s, %s, %s, FALSE, now()
            )
            """,
            (
                candidate_source_id,
                task_id,
                candidate_id,
                workload.source_commit,
                "b" * 40,
                candidate_source_hash,
                baseline_source_id,
                f"candidate-source:{candidate_id}",
                Jsonb(provenance),
            ),
        )
        connection.execute(
            """
            INSERT INTO artifacts (
                artifact_id, task_id, candidate_id, kind, uri, content_hash,
                metadata, source_snapshot_id, build_recipe,
                adapter_provenance, synthetic, idempotency_key
            ) VALUES (
                %s, %s, %s, 'python_overlay', 'file:///fixture/overlay.py', %s,
                '{}'::jsonb, %s, '{}'::jsonb, %s, FALSE, %s
            )
            """,
            (
                artifact_id,
                task_id,
                candidate_id,
                artifact_hash,
                candidate_source_id,
                Jsonb(provenance),
                f"artifact:{artifact_id}",
            ),
        )
        connection.execute(
            """
            INSERT INTO evaluation_runs (
                evaluation_run_id, task_id, candidate_id, phase, passed,
                protocol_version, metrics, round_id, baseline_epoch_id,
                target_fingerprint, idempotency_key, evidence_uris,
                adapter_provenance, synthetic
            ) VALUES (
                %s, %s, %s, 'performance', TRUE, 'm1-fixture-v1',
                '{}'::jsonb, %s, %s, %s, %s, '[]'::jsonb, %s, FALSE
            )
            """,
            (
                evaluation_id,
                task_id,
                candidate_id,
                round_id,
                baseline_epoch_id,
                target_fingerprint,
                f"evaluation:{evaluation_id}",
                Jsonb(provenance),
            ),
        )
        connection.execute(
            """
            INSERT INTO evidence_bundles (
                evidence_id, task_id, candidate_id, baseline_epoch_id,
                evaluation_run_id, target_id, evidence_type, protocol_version,
                summary, adapter_provenance, synthetic, idempotency_key, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'm1_manual_candidate',
                'm1-fixture-v1', %s, %s, FALSE, %s, now()
            )
            """,
            (
                evidence_id,
                task_id,
                candidate_id,
                baseline_epoch_id,
                evaluation_id,
                workload.target_id,
                Jsonb({"file_hash": evidence_hash}),
                Jsonb(provenance),
                f"evidence:{evidence_id}",
            ),
        )
        connection.execute(
            "UPDATE candidates SET evidence_bundle_id = %s WHERE candidate_id = %s",
            (evidence_id, candidate_id),
        )
        connection.execute(
            """
            INSERT INTO manual_candidate_signoffs (
                signoff_id, task_id, candidate_id, decision, actor, reason,
                evidence_bundle_id, idempotency_key
            ) VALUES (
                %s, %s, %s, 'approved', 'endpoint-postgres-fixture',
                'fixture accepts evidence only', %s, %s
            )
            """,
            (
                signoff_id,
                task_id,
                candidate_id,
                evidence_id,
                f"signoff:{signoff_id}",
            ),
        )

    return EndpointValidationRunCreate(
        name="BW20 provisional endpoint PostgreSQL acceptance",
        signed_m1=SignedM1EvidenceReference(
            task_id=task_id,
            candidate_id=candidate_id,
            baseline_epoch_id=baseline_epoch_id,
            target_snapshot_id=target_snapshot_id,
            target_id=workload.target_id,
            target_fingerprint=target_fingerprint,
            candidate_source_hash=candidate_source_hash,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            evidence_bundle_id=evidence_id,
            evidence_bundle_hash=evidence_hash,
            signoff_id=signoff_id,
        ),
        workload=workload,
        plan=EndpointMeasurementPlan(
            run_mode="provisional",
            acquisition_order=("baseline", "candidate", "candidate", "baseline"),
            warmup_requests=1,
            measured_requests_per_acquisition=2,
            ready_timeout_seconds=300,
            request_timeout_seconds=60,
        ),
        environment_fingerprint=_hash(20),
        adapter_profile=PROFILE,
        idempotency_key=f"endpoint-postgres:{uuid4()}",
    )


@pytest.mark.parametrize(
    ("healthy", "expected_resource_state"),
    [(True, "available"), (False, "quarantined")],
)
def test_endpoint_run_claim_lease_failure_and_cleanup(
    endpoint_database: PostgresRepository,
    healthy: bool,
    expected_resource_state: str,
) -> None:
    repository = endpoint_database
    with repository.connection() as connection:
        versions = {
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations WHERE version IN (22, 23)"
            ).fetchall()
        }
    assert versions == {22, 23}

    request = _seed_signed_m1(repository)
    run = repository.create_endpoint_validation_run(request)
    assert run["state"] == "queued"
    assert run["automatic_release_allowed"] is False
    with repository.connection() as connection:
        with pytest.raises(psycopg.Error, match="endpoint validation bindings are immutable"):
            connection.execute(
                """
                UPDATE endpoint_validation_runs
                SET environment_fingerprint = %s
                WHERE endpoint_run_id = %s
                """,
                (_hash(21), run["endpoint_run_id"]),
            )

    worker_id = f"endpoint-postgres-worker-{uuid4()}"
    resource_id = f"endpoint-postgres-resource-{uuid4()}"
    repository.register_worker(
        WorkerRegister(
            worker_id=worker_id,
            worker_type=WorkerType.GPU,
            adapter_profile=PROFILE,
            capabilities={"resource_id": resource_id},
        )
    )
    job = repository.claim_job(worker_id)
    assert job is not None
    assert job["job_id"] == run["job_id"]
    assert job["lease_scope"] == "exclusive"
    assert job["resource_id"] == resource_id
    assert job["lease_id"] is not None
    assert job["fencing_token"] == 1
    repository.assert_live_job_lease(
        worker_id,
        job["job_id"],
        job["claim_token"],
        job["fencing_token"],
    )
    assert repository.get_endpoint_validation_run(run["endpoint_run_id"])["state"] == (
        "running"
    )

    cleanup = {
        "fence": {
            "fenced": True,
            "resource_id": resource_id,
            "fencing_token": job["fencing_token"],
        },
        "health": {
            "healthy": healthy,
            "quarantined": not healthy,
            "resource_id": resource_id,
        },
    }
    error = {"code": "fixture_endpoint_failure", "message": "CPU-only failure path"}
    failed_job = repository.fail_job(
        job["job_id"],
        job["claim_token"],
        job["fencing_token"],
        error,
        retryable=False,
        cleanup_evidence=cleanup,
    )
    assert failed_job["state"] == "failed"

    summary = repository.endpoint_validation_summary(run["endpoint_run_id"])
    assert summary["run"]["state"] == "failed"
    assert summary["run"]["failure_error"] == error
    assert summary["run"]["cleanup_evidence"] == cleanup
    assert summary["task"]["state"] == TaskState.REJECTED.value
    assert summary["task"]["automatic_release_allowed"] is False
    assert any(
        event["event_type"] == "endpoint_validation_failed"
        for event in summary["events"]
    )
    with repository.connection() as connection:
        resource = connection.execute(
            "SELECT * FROM resources WHERE resource_id = %s", (resource_id,)
        ).fetchone()
    assert resource is not None
    assert resource["state"] == expected_resource_state
    assert resource["owner_job_id"] is None
    assert resource["lease_id"] is None
    assert resource["cleanup_evidence"]["healthy"] is healthy

