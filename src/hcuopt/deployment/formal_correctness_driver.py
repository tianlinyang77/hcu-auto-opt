# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Explicit build-to-correctness handoff; no performance or automatic release."""

from pathlib import Path

from hcuopt.adapters.m1_verification import M1KernelCorrectnessWorkerAdapter
from hcuopt.contracts.m2 import ArtifactFamilyFreezeRequest
from hcuopt.domain.enums import SearchRoundRunMode
from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_correctness_jobs import PostgresFormalCorrectnessJobs
from hcuopt.storage.formal_correctness_journal import PostgresFormalCorrectnessJournal
from hcuopt.storage.formal_correctness_lease import PostgresFormalCorrectnessLease


class FormalCorrectnessDriver:
    def __init__(self, runtime, *, intent_id, worker_id, claim_token):
        self.runtime = runtime
        self.intent_id, self.worker_id, self.claim_token = intent_id, worker_id, claim_token
        self.jobs = PostgresFormalCorrectnessJobs(
            runtime.claims, intent_id, worker_id, claim_token,
        )
        self.lease = PostgresFormalCorrectnessLease(
            self.jobs, enabled=runtime.dispatcher.enabled,
        )

    def freeze_family(self, *, expected_artifact_family_hash: str):
        """Freeze only the caller-reviewed complete family, using the original gate."""
        runtime = self.runtime
        runtime.claims.assert_active(self.intent_id, self.worker_id, self.claim_token)
        intent = runtime.repository.get_formal_start_intent(self.intent_id)
        with runtime.repository.connection() as conn:
            row = conn.execute(
                "SELECT candidate_family_hash FROM search_rounds WHERE round_id = %s",
                (intent.round_id,),
            ).fetchone()
        if row is None:
            raise Conflict("Formal correctness Round is unavailable")
        return runtime.repository.freeze_search_round_artifact_family(ArtifactFamilyFreezeRequest(
            round_id=intent.round_id, candidate_family_hash=row["candidate_family_hash"],
            expected_artifact_family_hash=expected_artifact_family_hash,
        ))

    def prepare(self, *, candidate_id, executor_id: str, wall_seconds: float):
        """Reserve and atomically claim once. This acquires a control-plane lease.

        Physical isolation remains B's responsibility. Repeated calls never
        take over a running/expired Job. Retain the journal owner for recovery.
        """
        reserved = self.jobs.reserve(candidate_id, wall_seconds=wall_seconds)
        claimed = self.lease.claim(
            candidate_id, reserved["reservation"]["reservation_id"], executor_id=executor_id,
        )
        return PostgresFormalCorrectnessJournal(
            self.lease, claimed["job_id"], executor_id=executor_id,
            token=claimed["claim_token"], lease_id=claimed["lease_id"],
            fencing_token=claimed["fencing_token"],
        )

    def _validate_adapter(self, adapter):
        if not isinstance(adapter, M1KernelCorrectnessWorkerAdapter):
            raise TypeError("Formal correctness requires the independent M1 Worker adapter")
        compiler = self.runtime.management.coordinator.compiler
        target = compiler.profiles.require(
            compiler.authorization.profiles.target_profile, SearchRoundRunMode.FORMAL,
        ).authority_refs
        if (adapter.provenance.profile != target.adapter_profile
                or adapter.producer.provenance.profile != target.adapter_profile
                or adapter.provenance.implementation_kind != "real"
                or adapter.producer.provenance.implementation_kind != "real"):
            raise Conflict("Formal correctness adapter differs from admitted target")

    def execute_prepared(self, *, journal, adapter, output_dir: Path):
        self._validate_adapter(adapter)
        if journal.lease is not self.lease:
            raise Conflict("Formal correctness journal belongs to another driver")
        return self.runtime.correctness_consumer(
            journal=journal, adapter=adapter,
        ).execute_current_once(output_dir=output_dir)

    def execute_candidate(self, *, candidate_id, executor_id: str, wall_seconds: float,
                          adapter, output_dir: Path):
        """One candidate, no retry loop. Known/unknown outcomes use existing recovery.

        Validate adapter before acquiring resources. Failures after preparation
        leave durable ownership for reconciliation, never silently release it.
        """
        self._validate_adapter(adapter)
        journal = self.prepare(candidate_id=candidate_id, executor_id=executor_id,
                               wall_seconds=wall_seconds)
        return self.execute_prepared(journal=journal, adapter=adapter, output_dir=output_dir)
