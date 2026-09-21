# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Opt-in correctness execution with durable invocation and exact result replay."""

import hashlib
import time
from pathlib import Path
from uuid import UUID

from hcuopt.adapters.m1_verification import M1KernelCorrectnessWorkerAdapter, _correctness_context
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.formal_correctness import validate_formal_correctness_context
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.storage.formal_correctness_journal import PostgresFormalCorrectnessJournal


class FormalCorrectnessConsumer:
    def __init__(self, journal: PostgresFormalCorrectnessJournal,
                 adapter: M1KernelCorrectnessWorkerAdapter, *, enabled: bool = False):
        if not isinstance(adapter, M1KernelCorrectnessWorkerAdapter):
            raise TypeError("Formal correctness requires the independent M1 Worker adapter")
        self.journal, self.adapter, self.enabled = journal, adapter, enabled

    def execute_once(self, *, load_materials, output_dir: Path):
        """load_materials is deployment-owned, never an HTTP payload callback.

        It must load current authoritative (Round, all frozen build members,
        M1 payload), including trusted target/source/hotspot/Stage0 records.
        This function does not implement that deployment-specific loader.
        """
        if not self.enabled:
            raise Conflict("Formal correctness consumer is disabled")
        round_, members, payload = load_materials()
        context = _correctness_context(payload, payload.get("_job_context"))
        validate_formal_correctness_context(
            round_authority=round_, members=members, context=context,
        )
        journal = self.journal
        owner = journal.owner
        if (payload.get("adapter_profile") != round_.adapter_profile
                or self.adapter.provenance.profile != round_.adapter_profile
                or self.adapter.provenance.implementation_kind != "real"
                or UUID(str(payload.get("round_id"))) != round_.round_id
                or UUID(str(payload["_job_context"].get("job_id"))) != journal.job_id
                or context.lease_id != owner["lease_id"]
                or context.fencing_token != owner["fencing_token"]):
            raise Conflict("Formal correctness payload differs from execution binding")
        adapter_binding = {
            "verifier": self.adapter.provenance.model_dump(mode="json"),
            "producer": self.adapter.producer.provenance.model_dump(mode="json"),
        }
        snapshot = canonical_json_bytes({
            "round": round_.model_dump(mode="json"),
            "members": [m.model_dump(mode="json") for m in members], "payload": payload,
            "output_dir": str(output_dir.resolve()), "adapter": adapter_binding,
        })
        input_hash = "sha256:" + hashlib.sha256(snapshot).hexdigest()
        repo = journal.lease.jobs.claims.dispatcher.repository
        with repo.connection() as conn:
            job, _ = journal._locked(conn, input_hash)
            if (str(context.candidate_id) != job["payload"]["candidate_id"]
                    or str(context.artifact_id) != job["payload"]["artifact_id"]
                    or context.artifact_hash != job["payload"]["artifact_hash"]
                    or context.resource_id != job["resource_id"]
                    or round_.artifact_family_hash != job["payload"]["artifact_family_hash"]):
                raise Conflict("Formal correctness materials differ from claimed Job")
        events, acquired = journal.begin(input_hash)
        if not acquired:
            if "formal_correctness_result" not in events:
                raise Conflict("Formal correctness invocation is uncertain; no retry")
            return journal.finalize_recorded_result(input_hash)
        try:
            journal.lease.assert_live(journal.job_id, **owner)
            current_round, current_members, current_payload = load_materials()
            current = canonical_json_bytes({
                "round": current_round.model_dump(mode="json"),
                "members": [m.model_dump(mode="json") for m in current_members],
                "payload": current_payload, "output_dir": str(output_dir.resolve()),
                "adapter": adapter_binding,
            })
            if current != snapshot:
                raise Conflict("Formal correctness durable materials changed before invocation")
            started = time.monotonic()
            result = self.adapter.run_manual_correctness(payload, output_dir)
            elapsed = time.monotonic() - started
            # Retain completed evidence even after stop/expiry; do not run a second time.
            journal.record_result(input_hash, result, wall_seconds=elapsed)
        except Exception:
            journal.mark_unknown(input_hash)
            raise
        # A settlement/release failure retains the known result for reconciliation.
        return journal.finalize_recorded_result(input_hash)
