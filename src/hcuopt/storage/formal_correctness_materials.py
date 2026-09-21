# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read-only deployment loader for Formal correctness; no user-supplied payload."""

import re
from uuid import UUID

from hcuopt.adapters.m1_verification import _correctness_context
from hcuopt.contracts.m2 import RoundCandidate
from hcuopt.contracts.platform_v1 import ArtifactManifest, SourceSnapshot
from hcuopt.domain.errors import Conflict
from hcuopt.evaluation.formal_correctness import validate_formal_correctness_context
from hcuopt.storage.formal_correctness_journal import PostgresFormalCorrectnessJournal


class PostgresFormalCorrectnessMaterialReader:
    def __init__(self, journal: PostgresFormalCorrectnessJournal):
        self.journal = journal

    def load(self):
        """Load all identities under one transaction; execution separately checks live lease.

        Allows exact historical result replay after stop/expiry; this method alone
        grants no authority to execute. File contents are checked by D's reader.
        """
        journal = self.journal
        jobs = journal.lease.jobs
        repo = jobs.claims.dispatcher.repository
        with repo.connection() as conn:
            intent = jobs.claims._lock_deployment_intent(conn, jobs.intent_id)
            job = conn.execute(
                "SELECT * FROM jobs WHERE job_id = %s FOR SHARE", (journal.job_id,)
            ).fetchone()
            if (
                job is None
                or job["execution_lane"] != "formal"
                or job["job_type"] != "manual_correctness"
                or job["task_id"] != intent.task_id
                or job["payload"].get("intent_id") != str(intent.intent_id)
                or job["claimed_by"] != journal.owner["executor_id"]
                or job["claim_token"] != journal.owner["token"]
                or job["lease_id"] != journal.owner["lease_id"]
                or job["fencing_token"] != journal.owner["fencing_token"]
            ):
                raise Conflict("Formal correctness material Job owner differs")
            round_row = conn.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR SHARE", (intent.round_id,)
            ).fetchone()
            rows = conn.execute(
                "SELECT * FROM round_candidates WHERE round_id = %s ORDER BY ordinal FOR SHARE",
                (intent.round_id,),
            ).fetchall()
            if round_row is None:
                raise Conflict("Formal correctness Round is missing")
            round_ = repo._search_round_authority(round_row)
            members = [
                RoundCandidate.model_validate(
                    {name: row[name] for name in RoundCandidate.model_fields}
                )
                for row in rows
            ]
            candidate_id = UUID(job["payload"]["candidate_id"])
            if not any(m.candidate_id == candidate_id for m in intent.candidate_bindings):
                raise Conflict("Formal correctness candidate is outside Intent")
            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = %s AND candidate_id = %s "
                "AND task_id = %s AND content_hash = %s FOR SHARE",
                (
                    UUID(job["payload"]["artifact_id"]),
                    candidate_id,
                    intent.task_id,
                    job["payload"]["artifact_hash"],
                ),
            ).fetchone()
            baseline = conn.execute(
                "SELECT s.* FROM baseline_epochs b JOIN source_snapshots s "
                "ON s.snapshot_id = b.source_snapshot_id WHERE b.baseline_epoch_id = %s "
                "AND b.frozen = true FOR SHARE",
                (round_.baseline_epoch_id,),
            ).fetchone()
            if (
                artifact is None
                or artifact["synthetic"]
                or baseline is None
                or baseline["synthetic"]
            ):
                raise Conflict("Formal correctness real Artifact/Baseline is missing")
            source = conn.execute(
                "SELECT * FROM source_snapshots WHERE snapshot_id = %s AND candidate_id = %s "
                "AND task_id = %s FOR SHARE",
                (artifact["source_snapshot_id"], candidate_id, intent.task_id),
            ).fetchone()
            if (
                source is None
                or source["synthetic"]
                or not source["clean"]
                or not baseline["clean"]
                or source["kind"] != "candidate"
                or baseline["kind"] != "baseline"
                or source["parent_snapshot_id"] != baseline["snapshot_id"]
            ):
                raise Conflict("Formal correctness Candidate source ancestry differs")
            target = conn.execute(
                "SELECT * FROM target_snapshots WHERE target_snapshot_id = %s FOR SHARE",
                (round_.target_snapshot_id,),
            ).fetchone()
            hotspot = conn.execute(
                "SELECT * FROM hotspots WHERE hotspot_id = %s FOR SHARE", (round_.hotspot_id,)
            ).fetchone()
            stage = conn.execute(
                "SELECT e.evidence, e.report, r.state, r.mode, r.target_snapshot_id, "
                "r.adapter_profile, r.task_id, t.workload_id FROM stage0_evidence e "
                "JOIN stage0_runs r ON r.stage0_run_id = e.stage0_run_id "
                "JOIN tasks t ON t.task_id = r.task_id WHERE r.stage0_run_id = %s FOR SHARE",
                (round_.stage0_run_id,),
            ).fetchone()
            if (
                target is None
                or hotspot is None
                or stage is None
                or stage["state"] != "finalized"
                or stage["mode"] != "formal"
                or stage["evidence"].get("synthetic") is not False
                or stage["target_snapshot_id"] != round_.target_snapshot_id
                or stage["report"].get("evidence_authority") != "formal"
                or stage["report"].get("automatic_release_allowed") is not False
                or hotspot["baseline_epoch_id"] != round_.baseline_epoch_id
                or hotspot["evidence"].get("replacement_point") != round_.replacement_point
            ):
                raise Conflict("Formal correctness target/hotspot/Stage0 authority differs")
            report = {
                "uri": stage["report"].get("machine_report_uri"),
                "sha256": stage["report"].get("machine_report_hash"),
                "input_digest": stage["evidence"].get("input_digest"),
                "protocol_version": stage["evidence"].get("protocol_version"),
                "protocol_hash": stage["evidence"].get("protocol_hash"),
                "stage0_task_id": str(stage["task_id"]),
                "stage0_workload_id": stage["workload_id"],
                "stage0_adapter_profile": stage["adapter_profile"],
            }
            if (
                any(not isinstance(v, str) or not v for v in report.values())
                or report["protocol_hash"] != round_.stage0_protocol_hash
                or any(
                    not re.fullmatch(r"sha256:[0-9a-f]{64}", report[k])
                    for k in ("sha256", "input_digest")
                )
            ):
                raise Conflict("Formal correctness requires a hashed Stage0 machine report")
            payload = {
                "task_id": str(intent.task_id),
                "candidate_id": str(candidate_id),
                "round_id": str(intent.round_id),
                "adapter_profile": round_.adapter_profile,
                "baseline_epoch_id": str(round_.baseline_epoch_id),
                "target_snapshot_id": str(round_.target_snapshot_id),
                "target": target["specification"],
                "target_fingerprint": target["target_fingerprint"],
                "stage0_run_id": str(round_.stage0_run_id),
                "stage0_report": report,
                "stage0_protocol_hash": round_.stage0_protocol_hash,
                "workload_id": round_.workload_id,
                "workload_hash": round_.workload_hash,
                "configuration_hash": round_.configuration_hash,
                "hotspot_id": str(round_.hotspot_id),
                "hotspot": hotspot["evidence"],
                "baseline_source": SourceSnapshot.model_validate(
                    {k: baseline[k] for k in SourceSnapshot.model_fields}
                ).model_dump(mode="json"),
                "candidate_source": SourceSnapshot.model_validate(
                    {k: source[k] for k in SourceSnapshot.model_fields}
                ).model_dump(mode="json"),
                "artifact": ArtifactManifest.model_validate(
                    {k: artifact[k] for k in ArtifactManifest.model_fields}
                ).model_dump(mode="json"),
                "_job_context": {
                    "job_id": str(job["job_id"]),
                    "lease_scope": "shared",
                    "lease_id": str(job["lease_id"]),
                    "resource_id": job["resource_id"],
                    "fencing_token": job["fencing_token"],
                },
            }
            context = _correctness_context(payload, payload["_job_context"])
            validate_formal_correctness_context(
                round_authority=round_,
                members=members,
                context=context,
            )
            return round_, members, payload
