# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Load build inputs from the reservation and its durable Formal Intent."""

from hcuopt.contracts.m2 import RoundCandidate
from hcuopt.contracts.platform_v1 import SourceSnapshot
from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_build_jobs import PostgresFormalBuildJobs


class PostgresFormalBuildMaterialReader:
    def __init__(self, journal):
        self.journal = journal

    def load(self, reservation_id):
        journal = self.journal
        repo = journal.claims.dispatcher.repository
        with repo.connection() as conn:
            intent = journal.claims._lock_deployment_intent(conn, journal.intent_id)
            budget = conn.execute(
                "SELECT * FROM round_budget_reservations WHERE reservation_id = %s FOR SHARE",
                (reservation_id,),
            ).fetchone()
            if budget is None or budget["round_id"] != intent.round_id:
                raise Conflict("Formal build reservation differs from Intent")
            candidate_id = budget["candidate_id"]
            if not any(c.candidate_id == candidate_id for c in intent.candidate_bindings):
                raise Conflict("Formal build candidate is outside Intent")
            job = conn.execute(
                "SELECT * FROM jobs WHERE job_id = %s FOR SHARE", (budget["job_id"],),
            ).fetchone()
            binding = PostgresFormalBuildJobs(
                journal.claims, journal.intent_id, journal.worker_id, journal.claim_token,
            ).binding(candidate_id)
            if (job is None or job["task_id"] != intent.task_id
                    or job["execution_lane"] != "formal" or job["job_type"] != "manual_build"
                    or job["payload"] != binding):
                raise Conflict("Formal build material Job owner differs")
            row = conn.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR SHARE", (intent.round_id,),
            ).fetchone()
            member = conn.execute(
                "SELECT * FROM round_candidates WHERE round_id = %s AND candidate_id = %s "
                "FOR SHARE", (intent.round_id, candidate_id),
            ).fetchone()
            if row is None or member is None:
                raise Conflict("Formal build durable intake is missing")
            round_ = repo._search_round_authority(row)
            baseline = conn.execute(
                "SELECT s.* FROM baseline_epochs b JOIN source_snapshots s "
                "ON s.snapshot_id = b.source_snapshot_id WHERE b.baseline_epoch_id = %s "
                "AND b.frozen = true FOR SHARE", (round_.baseline_epoch_id,),
            ).fetchone()
            hotspot = conn.execute(
                "SELECT * FROM hotspots WHERE hotspot_id = %s FOR SHARE", (round_.hotspot_id,),
            ).fetchone()
            if (baseline is None or baseline["synthetic"] or not baseline["clean"]
                    or baseline["kind"] != "baseline" or hotspot is None
                    or hotspot["baseline_epoch_id"] != round_.baseline_epoch_id
                    or hotspot["evidence"].get("replacement_point") != round_.replacement_point):
                raise Conflict("Formal build Baseline or Hotspot differs")
            return dict(
                round_authority=round_,
                member=RoundCandidate.model_validate({
                    key: member[key] for key in RoundCandidate.model_fields
                }),
                baseline=SourceSnapshot.model_validate({
                    key: baseline[key] for key in SourceSnapshot.model_fields
                }),
                hotspot=hotspot["evidence"], hotspot_intake_hash=hotspot["intake_hash"],
            )
