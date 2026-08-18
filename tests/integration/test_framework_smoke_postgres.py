import os
import unittest
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hcuopt.contracts.v1 import (
    FrameworkSmokeCreate,
    FrameworkSmokeResult,
    FrameworkSmokeVariantExecution,
    PairedFrameworkSmokeResult,
    WorkerRegister,
)
from hcuopt.domain.enums import CandidateState, TaskState, WorkerType
from hcuopt.domain.errors import StaleClaimToken
from hcuopt.orchestrator.router import WorkflowRouter
from hcuopt.storage.repository import PostgresRepository
from hcuopt.targets import load_target
from hcuopt.workers.handlers import FakeJobHandlers

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

DATABASE_URL = os.getenv("HCUOPT_DATABASE_URL")
ROOT = Path(__file__).parents[2]
TARGET_PATH = ROOT / "config" / "targets" / "nmz36-sglang-0.5.12.yaml"
PROFILE = "fake-v1-control-flow-only"


@unittest.skipUnless(DATABASE_URL and psycopg, "requires PostgreSQL and psycopg")
@pytest.mark.postgres
class FrameworkSmokePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        assert psycopg is not None
        self.repository = PostgresRepository(DATABASE_URL)
        self.repository.migrate()
        self.connection = psycopg.connect(DATABASE_URL)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE task_events, evidence_bundles, execution_requests,
                    source_snapshots, job_events, execution_attempts,
                    evaluation_runs, artifacts, candidates, hotspots, jobs,
                    workers, stage0_evidence, baseline_epochs, resources,
                    tasks, target_snapshots
                RESTART IDENTITY CASCADE
                """
            )
        self.connection.commit()
        self.target = load_target(TARGET_PATH)
        self.handlers = FakeJobHandlers()
        self.router = WorkflowRouter(self.repository)

    def tearDown(self) -> None:
        self.connection.close()

    def _register_workers(self) -> None:
        self.repository.register_worker(
            WorkerRegister(
                worker_id="build-fake",
                worker_type=WorkerType.BUILD,
                adapter_profile=PROFILE,
                capabilities={
                    "adapter_profile": PROFILE,
                    "adapters": ["source_manager", "builder", "artifact_store"],
                },
            )
        )
        self.repository.register_worker(
            WorkerRegister(
                worker_id="gpu-fake",
                worker_type=WorkerType.GPU,
                adapter_profile=PROFILE,
                capabilities={
                    "adapter_profile": PROFILE,
                    "adapters": ["executor", "evaluator", "resource_cleaner"],
                    "resource_id": "fake-hcu-0",
                },
            )
        )

    def _complete_claim(self, worker_id: str) -> dict | None:
        job = self.repository.claim_job(worker_id)
        if job is None:
            return None
        payload = dict(job["payload"])
        payload["_job_context"] = {
            "job_id": str(job["job_id"]),
            "attempt_number": job["attempts"],
            "resource_id": job["resource_id"],
            "fencing_token": job["fencing_token"],
        }
        result = self.handlers.handle(job["job_type"], payload)
        completed = self.repository.complete_job(
            job["job_id"], job["claim_token"], job["fencing_token"], result
        )
        self.router.advance(completed)
        return completed

    def test_full_chain_replay_retest_and_profile_claiming(self) -> None:
        task = self.repository.create_framework_smoke_task(
            FrameworkSmokeCreate(
                name="framework fixture",
                target_id=self.target.target_id,
                adapter_profile=PROFILE,
                idempotency_key="framework-fixture-task",
            ),
            self.target,
            str(TARGET_PATH),
        )
        self.repository.register_worker(
            WorkerRegister(
                worker_id="wrong-profile",
                worker_type=WorkerType.BUILD,
                adapter_profile="other-profile",
            )
        )
        self.assertIsNone(self.repository.claim_job("wrong-profile"))
        self._register_workers()

        source_completed = self._complete_claim("build-fake")
        assert source_completed is not None
        self.router.framework_smoke.advance(source_completed)
        build_completed = self._complete_claim("build-fake")
        assert build_completed is not None
        self.router.framework_smoke.advance(build_completed)
        completed = self._complete_claim("gpu-fake")
        assert completed is not None
        replayed_completion = self.repository.complete_job(
            completed["job_id"],
            completed["claim_token"],
            completed["fencing_token"],
            completed["result"],
        )
        self.assertEqual(replayed_completion["job_id"], completed["job_id"])
        summary = self.repository.framework_smoke_summary(task["task_id"])
        self.assertEqual(summary["task"]["state"], TaskState.AWAITING_SIGNOFF.value)
        self.assertEqual(
            summary["candidates"][0]["state"],
            CandidateState.FRAMEWORK_SMOKE_PASSED.value,
        )
        self.assertEqual(len(summary["source_snapshots"]), 2)
        self.assertEqual(len(summary["artifacts"]), 1)
        self.assertEqual(len(summary["evaluations"]), 1)
        self.assertEqual(len(summary["execution_requests"]), 1)
        self.assertEqual(len(summary["execution_attempts"]), 1)
        self.assertEqual(len(summary["evidence_bundles"]), 1)

        self.router.framework_smoke.advance(completed)
        replay = self.repository.framework_smoke_summary(task["task_id"])
        self.assertEqual(len(replay["evaluations"]), 1)
        self.assertEqual(len(replay["evidence_bundles"]), 1)

        self.router.framework_smoke.enqueue_retest(task["task_id"], "operator retest")
        self.assertIsNotNone(self._complete_claim("gpu-fake"))
        retested = self.repository.framework_smoke_summary(task["task_id"])
        self.assertEqual(retested["task"]["retest_count"], 1)
        self.assertEqual(len(retested["evaluations"]), 2)
        self.assertEqual(len(retested["evidence_bundles"]), 2)

    def test_cancel_invalidates_an_inflight_claim(self) -> None:
        task = self.repository.create_framework_smoke_task(
            FrameworkSmokeCreate(
                name="cancel fixture",
                target_id=self.target.target_id,
                adapter_profile=PROFILE,
                idempotency_key="framework-cancel-task",
            ),
            self.target,
            str(TARGET_PATH),
        )
        self._register_workers()
        claimed = self.repository.claim_job("build-fake")
        assert claimed is not None
        cancelled = self.repository.cancel_framework_task(
            task["task_id"], "operator cancelled"
        )
        self.assertEqual(cancelled["state"], TaskState.CANCELLED.value)
        with self.assertRaises(StaleClaimToken):
            self.repository.complete_job(
                claimed["job_id"],
                claimed["claim_token"],
                claimed["fencing_token"],
                {"late": True},
            )

    def test_paired_framework_result_persists_both_variants_idempotently(self) -> None:
        task = self.repository.create_framework_smoke_task(
            FrameworkSmokeCreate(
                name="paired framework fixture",
                target_id=self.target.target_id,
                adapter_profile=PROFILE,
                idempotency_key="paired-framework-fixture-task",
            ),
            self.target,
            str(TARGET_PATH),
        )
        self._register_workers()
        self.assertIsNotNone(self._complete_claim("build-fake"))
        self.assertIsNotNone(self._complete_claim("build-fake"))

        job = self.repository.claim_job("gpu-fake")
        assert job is not None
        payload = dict(job["payload"])
        payload["_job_context"] = {
            "job_id": str(job["job_id"]),
            "attempt_number": job["attempts"],
            "resource_id": job["resource_id"],
            "fencing_token": job["fencing_token"],
        }
        single = FrameworkSmokeResult.model_validate(
            self.handlers.handle(job["job_type"], payload)
        )

        executions = []
        attempts = {}
        for variant in ("baseline", "noop"):
            request_id = UUID(payload[f"{variant}_execution_request_id"])
            request = single.execution_request.model_copy(
                update={"request_id": request_id}
            )
            result = single.execution_result.model_copy(update={"request_id": request_id})
            attempt = single.execution_attempt.model_copy(
                update={
                    "execution_attempt_id": uuid4(),
                    "request_id": request_id,
                    "variant": variant,
                }
            )
            attempts[variant] = str(attempt.execution_attempt_id)
            executions.append(
                FrameworkSmokeVariantExecution(
                    variant=variant,
                    execution_request=request,
                    execution_result=result,
                    execution_attempt=attempt,
                    cleanup_evidence=single.cleanup_evidence,
                )
            )

        evidence = single.evidence.model_copy(
            update={
                "summary": {
                    **single.evidence.summary,
                    "execution_attempt_ids": attempts,
                }
            }
        )
        paired = PairedFrameworkSmokeResult(
            executions=executions,
            evaluation=single.evaluation,
            evidence=evidence,
            cleanup_evidence={
                **single.cleanup_evidence,
                "variants": {
                    "baseline": single.cleanup_evidence,
                    "noop": single.cleanup_evidence,
                },
            },
            adapter_provenance=single.adapter_provenance,
            synthetic=single.synthetic,
        )
        completed = self.repository.complete_job(
            job["job_id"],
            job["claim_token"],
            job["fencing_token"],
            paired.model_dump(mode="json"),
        )
        self.router.advance(completed)

        summary = self.repository.framework_smoke_summary(task["task_id"])
        self.assertEqual(
            [item["variant"] for item in summary["execution_requests"]],
            ["baseline", "noop"],
        )
        self.assertEqual(
            {item["variant"] for item in summary["execution_attempts"]},
            {"baseline", "noop"},
        )
        self.assertEqual(len(summary["execution_requests"]), 2)
        self.assertEqual(len(summary["execution_attempts"]), 2)

        self.router.advance(completed)
        replay = self.repository.framework_smoke_summary(task["task_id"])
        self.assertEqual(len(replay["execution_requests"]), 2)
        self.assertEqual(len(replay["execution_attempts"]), 2)
        self.assertEqual(len(replay["evaluations"]), 1)
        self.assertEqual(len(replay["evidence_bundles"]), 1)
