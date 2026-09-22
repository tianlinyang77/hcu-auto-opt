# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""One bounded build call using a pre-reserved budget, not a background scheduler."""

import hashlib
import time
from pathlib import Path

from hcuopt.adapters.formal_candidate_builder import (
    FormalCandidateBuildResult,
    FormalRoundCandidateBuilder,
)
from hcuopt.contracts.m2 import RoundCandidate, RoundCandidateBuildTerminal
from hcuopt.contracts.v1 import ManualCandidateBuildResult
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.storage.formal_build import PostgresFormalBuildStore
from hcuopt.storage.formal_build_journal import PostgresFormalBuildJournal


class FormalBuildConsumer:
    def __init__(self, journal: PostgresFormalBuildJournal,
                 builder: FormalRoundCandidateBuilder, store: PostgresFormalBuildStore,
                 *, enabled: bool = False):
        if store.claims is not journal.claims:
            raise Conflict("Formal build publication and journal must share claim authority")
        self.journal, self.builder, self.store, self.enabled = journal, builder, store, enabled

    def execute_current_once(self, *, reservation_id, output_dir: Path):
        """Deployment path: callers identify a reservation, not mutable build inputs."""
        from hcuopt.storage.formal_build_materials import PostgresFormalBuildMaterialReader

        if not self.enabled or not self.builder.enabled or not self.store.enabled:
            raise Conflict("Formal build consumer is disabled")
        repo = self.journal.claims.dispatcher.repository
        with repo.connection() as conn:
            previous = conn.execute(
                "SELECT * FROM formal_build_journal WHERE intent_id = %s AND reservation_id = %s",
                (self.journal.intent_id, reservation_id),
            ).fetchone()
        if previous is not None:
            args = (previous["candidate_id"], reservation_id, previous["input_hash"])
            row, acquired = self.journal.begin(*args)
            if acquired or row["state"] != "result_recorded":
                raise Conflict("Formal build is uncertain; recovery required, no retry")
            return self._publish_recorded(args, row)
        materials = PostgresFormalBuildMaterialReader(self.journal).load(reservation_id)
        return self.execute_once(reservation_id=reservation_id, output_dir=output_dir, **materials)

    def _publish_recorded(self, args, row):
        result = FormalCandidateBuildResult(
            ManualCandidateBuildResult.model_validate(row["result"]["build"]),
            RoundCandidateBuildTerminal.model_validate(row["result"]["terminal"]),
        )
        self.journal.settle_recorded_result(*args)
        self.store.record(
            self.journal.intent_id, self.journal.worker_id, self.journal.claim_token, result,
        )
        self.journal.complete_published_job(*args)
        return result

    def execute_once(self, *, reservation_id, round_authority, member, baseline,
                     hotspot, hotspot_intake_hash: str, output_dir: Path):
        """Persist output before publication; never rerun an uncertain build.

        Budget reservation must already exist. Successful build usage is settled
        before publication, including replay. Unknown failures require reconciliation.
        The journal owns isolated Job claiming/completion. Creation and budget
        reservation remain explicit deployment operations.
        """
        if not self.enabled or not self.builder.enabled or not self.store.enabled:
            raise Conflict("Formal build consumer is disabled")
        payload = {
            "round": round_authority.model_dump(mode="json"),
            "member": member.model_dump(mode="json"),
            "baseline": baseline.model_dump(mode="json"), "hotspot": dict(hotspot),
            "hotspot_intake_hash": hotspot_intake_hash,
            "output_dir": str(output_dir.resolve()),
            "store_id": self.builder.store_id, "store_hash": self.builder.store_hash,
        }
        input_hash = "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        journal = self.journal
        args = (member.candidate_id, reservation_id, input_hash)
        row, acquired = journal.begin(*args)
        if not acquired:
            if row["state"] != "result_recorded":
                raise Conflict("Formal build is uncertain; recovery required, no retry")
            result = FormalCandidateBuildResult(
                ManualCandidateBuildResult.model_validate(row["result"]["build"]),
                RoundCandidateBuildTerminal.model_validate(row["result"]["terminal"]),
            )
        else:
            try:
                journal.claims.assert_active(
                    journal.intent_id, journal.worker_id, journal.claim_token,
                )
                repo = journal.claims.dispatcher.repository
                with repo.connection() as conn:
                    intent = journal.claims._lock_deployment_intent(conn, journal.intent_id)
                    durable_round = conn.execute(
                        "SELECT * FROM search_rounds WHERE round_id = %s FOR SHARE",
                        (intent.round_id,),
                    ).fetchone()
                    durable_member = conn.execute(
                        "SELECT * FROM round_candidates WHERE round_id = %s "
                        "AND candidate_id = %s FOR SHARE", (intent.round_id, member.candidate_id),
                    ).fetchone()
                    if (durable_round is None or durable_member is None
                            or repo._search_round_authority(durable_round) != round_authority
                            or RoundCandidate.model_validate({
                                key: durable_member[key] for key in RoundCandidate.model_fields
                            }) != member):
                        raise Conflict("Formal build materials differ from durable intake")
                    parent = conn.execute(
                        "SELECT s.* FROM baseline_epochs b JOIN source_snapshots s "
                        "ON s.snapshot_id = b.source_snapshot_id "
                        "WHERE b.baseline_epoch_id = %s FOR SHARE",
                        (round_authority.baseline_epoch_id,),
                    ).fetchone()
                    if parent is None or parent["synthetic"] or any(
                        parent[key] != value
                        for key, value in baseline.model_dump(mode="python").items()
                    ):
                        raise Conflict("Formal build Baseline differs from durable parent")
                    current_hotspot = conn.execute(
                        "SELECT * FROM hotspots WHERE hotspot_id = %s FOR SHARE",
                        (round_authority.hotspot_id,),
                    ).fetchone()
                    if (current_hotspot is None
                            or current_hotspot["baseline_epoch_id"]
                            != round_authority.baseline_epoch_id
                            or current_hotspot["evidence"] != dict(hotspot)
                            or current_hotspot["intake_hash"] != hotspot_intake_hash):
                        raise Conflict("Formal build Hotspot differs from durable intake")
                started = time.monotonic()
                result = self.builder.build_member(
                    round_authority=round_authority, member=member, baseline=baseline,
                    hotspot=hotspot, hotspot_intake_hash=hotspot_intake_hash, output_dir=output_dir,
                )
                wall_seconds = time.monotonic() - started
                journal.record_result(*args, result, wall_seconds=wall_seconds)
            except BaseException:
                journal.mark_unknown(*args)
                raise
        # Settlement errors retain the immutable output and usage for retry.
        # Do not mark that known result unknown or rerun the builder.
        journal.settle_recorded_result(*args)
        # Publication errors retain exact output for replay, not another build.
        self.store.record(journal.intent_id, journal.worker_id, journal.claim_token, result)
        journal.complete_published_job(*args)
        return result
