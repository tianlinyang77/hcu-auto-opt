import os
import threading
import unittest

import pytest

from dcuopt.contracts.v1 import (
    BaselineCreate,
    JobCreate,
    Stage0EvidenceRequest,
    TaskCreate,
    WorkerRegister,
)
from dcuopt.domain.enums import (
    GateResult,
    HotPatchCapability,
    JobType,
    LeaseScope,
    ProfilerCapability,
    ProjectMode,
    WorkerType,
)
from dcuopt.domain.errors import StaleClaimToken
from dcuopt.storage.repository import PostgresRepository

try:
    import psycopg
except ImportError:  # pragma: no cover - optional until dev dependencies are installed
    psycopg = None


DATABASE_URL = os.getenv("DCUOPT_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class PostgresClaimIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        assert psycopg is not None
        assert DATABASE_URL is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE job_events, evaluations, artifacts, candidates, hotspots,
                    jobs, workers, stage0_evidence, baseline_epochs, resources, tasks
                RESTART IDENTITY CASCADE
                """
            )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()

    def test_claim_is_atomic_and_returns_distinct_jobs(self) -> None:
        task = self.repository.create_task(
            TaskCreate(name="fixture", workload_id="fixture", idempotency_key="fixture-task")
        )
        for worker_id in ("worker-a", "worker-b"):
            self.repository.register_worker(
                WorkerRegister(worker_id=worker_id, worker_type=WorkerType.AGENT)
            )
        for ordinal in range(2):
            self.repository.enqueue_job(
                JobCreate(
                    task_id=task["task_id"],
                    job_type=JobType.CANDIDATE_GENERATE,
                    accepted_worker_type=WorkerType.AGENT,
                    idempotency_key=f"fixture-job-{ordinal}",
                )
            )

        claimed: list[str] = []
        barrier = threading.Barrier(2)

        def claim(worker_id: str) -> None:
            barrier.wait()
            job = self.repository.claim_job(worker_id)
            assert job is not None
            claimed.append(str(job["job_id"]))

        threads = [
            threading.Thread(target=claim, args=(worker,))
            for worker in ("worker-a", "worker-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(set(claimed)), 2)

    def test_worker_loss_fences_gpu_and_rejects_old_claim(self) -> None:
        task = self.repository.create_task(
            TaskCreate(name="gpu-fixture", workload_id="fixture", idempotency_key="gpu-task")
        )
        self.repository.register_worker(
            WorkerRegister(
                worker_id="gpu-worker",
                worker_type=WorkerType.GPU,
                capabilities={"resource_id": "fake-dcu-0"},
            )
        )
        self.repository.enqueue_job(
            JobCreate(
                task_id=task["task_id"],
                job_type=JobType.PROFILE,
                accepted_worker_type=WorkerType.GPU,
                lease_scope=LeaseScope.EXCLUSIVE,
                idempotency_key="gpu-job-fixture",
            )
        )
        first = self.repository.claim_job("gpu-worker")
        assert first is not None
        self.repository.recover_stale_jobs(stale_after_seconds=0)
        second = self.repository.claim_job("gpu-worker")
        assert second is not None
        self.assertGreater(second["fencing_token"], first["fencing_token"])
        with self.assertRaises(StaleClaimToken):
            self.repository.complete_job(
                first["job_id"],
                first["claim_token"],
                first["fencing_token"],
                {"late": True},
            )

    def test_frozen_baseline_rejects_in_place_update(self) -> None:
        task = self.repository.create_task(
            TaskCreate(
                name="baseline-fixture",
                workload_id="fixture",
                idempotency_key="baseline-task",
            )
        )
        evidence = Stage0EvidenceRequest(
            measurement=GateResult.PASS,
            profiler=ProfilerCapability.FULL,
            hot_patch=HotPatchCapability.HOT_PATCH,
            hardware_fingerprint="fake-hardware",
            software_fingerprint="fake-software",
            timer_resolution_ns=100,
            noise_sigma_ns=200,
            noise_cv=0.01,
            mde_ratio=0.03,
        )
        self.repository.save_stage0(
            task["task_id"], evidence, ProjectMode.FULL_MVP, ("fixture",)
        )
        baseline = self.repository.freeze_baseline(
            task["task_id"],
            BaselineCreate(
                hardware_fingerprint="fake-hardware",
                software_fingerprint="fake-software",
                workload_id="fixture",
                configuration_hash="config-v1",
            ),
        )
        with self.assertRaises(psycopg.Error):
            with self.repository.connection() as connection:
                connection.execute(
                    """
                    UPDATE baseline_epochs SET configuration_hash = 'changed'
                    WHERE baseline_epoch_id = %s
                    """,
                    (baseline["baseline_epoch_id"],),
                )
