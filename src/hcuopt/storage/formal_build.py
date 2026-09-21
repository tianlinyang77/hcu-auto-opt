# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Atomic publication of a completed real build; no builder or hardware invocation."""

import hashlib
import subprocess
from uuid import UUID

from psycopg.types.json import Jsonb

from hcuopt.adapters.formal_candidate_builder import FormalCandidateBuildResult
from hcuopt.contracts.m2 import RoundCandidateBuildTerminal
from hcuopt.contracts.v1 import ManualCandidateBuildResult
from hcuopt.domain.errors import Conflict
from hcuopt.measurement.evidence import canonical_json_bytes
from hcuopt.operator.formal_dispatch import prepare_formal_round
from hcuopt.source_hash import file_uri_to_path
from hcuopt.storage.formal_claim import PostgresFormalClaimStore
from hcuopt.storage.formal_dispatch import _insert


class PostgresFormalBuildStore:
    def __init__(self, claims: PostgresFormalClaimStore, *, enabled: bool = False):
        self.claims, self.enabled = claims, enabled

    def record(
        self, intent_id: UUID, worker_id: str, claim_token: UUID,
        result: FormalCandidateBuildResult,
    ) -> dict:
        if not self.enabled:
            raise Conflict("Formal build publication is disabled")
        build = ManualCandidateBuildResult.model_validate(result.build.model_dump(mode="json"))
        terminal = RoundCandidateBuildTerminal.model_validate(
            result.terminal.model_dump(mode="json")
        )
        artifact, source = build.artifact, build.source
        if (
            terminal.state != "built" or artifact.synthetic
            or terminal.candidate_id != build.candidate_id
            or terminal.artifact_id != artifact.artifact_id
            or terminal.artifact_hash != artifact.content_hash
            or artifact.kind != "python_overlay"
        ):
            raise Conflict("Formal build publication requires a matching real Overlay result")
        path = file_uri_to_path(artifact.uri)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
            raise Conflict("Formal build Artifact must be a read-only regular file")
        if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != artifact.content_hash:
            raise Conflict("Formal build Artifact content Hash differs")
        result_hash = "sha256:" + hashlib.sha256(canonical_json_bytes({
            "build": build.model_dump(mode="json"),
            "terminal": terminal.model_dump(mode="json"),
        })).hexdigest()
        claims = self.claims
        claims.assert_active(intent_id, worker_id, claim_token)
        repository = claims.dispatcher.repository
        prepared = prepare_formal_round(
            claims.dispatcher.coordinator,
            repository.get_formal_start_intent(intent_id), repository,
        )
        with repository.connection() as connection:
            intent = claims._lock_deployment_intent(connection, intent_id)
            if intent != prepared.intent:
                raise Conflict("Formal build Intent changed before publication")
            claims.dispatcher._revalidate_locked(connection, prepared)
            claim = connection.execute(
                "SELECT * FROM formal_dispatch_claims WHERE intent_id = %s "
                "AND worker_id = %s AND claim_token = %s AND state = 'claimed' "
                "AND expires_at > clock_timestamp() FOR SHARE",
                (intent_id, worker_id, claim_token),
            ).fetchone()
            if claim is None or connection.execute(
                "SELECT 1 FROM formal_dispatch_stop_requests WHERE intent_id = %s", (intent_id,)
            ).fetchone():
                raise Conflict("Formal build publication requires a live unstopped claim")
            if terminal.round_id != intent.round_id or not any(
                item.candidate_id == terminal.candidate_id
                and item.round_candidate_id == terminal.round_candidate_id
                for item in intent.candidate_bindings
            ):
                raise Conflict("Formal Build terminal is outside its claimed Intent")
            round_ = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s FOR UPDATE",
                (intent.round_id,),
            ).fetchone()
            member = connection.execute(
                "SELECT * FROM round_candidates WHERE round_candidate_id = %s FOR UPDATE",
                (terminal.round_candidate_id,),
            ).fetchone()
            if round_ is None or member is None or round_["run_mode"] != "formal":
                raise Conflict("Formal build Round or member is unavailable")
            previous = connection.execute(
                "SELECT details FROM task_events WHERE task_id = %s "
                "AND event_type = 'formal_candidate_build_recorded' "
                "AND details->>'round_candidate_id' = %s",
                (intent.task_id, str(terminal.round_candidate_id)),
            ).fetchone()
            if previous is not None:
                if previous["details"]["result_hash"] != result_hash:
                    raise Conflict("Formal build already recorded another result")
                return dict(member)
            if (
                round_["task_id"] != intent.task_id
                or round_["state"] not in {"intake_closed", "building"}
                or round_["artifact_family_hash"] is not None
                or member["state"] != "intake_accepted"
                or member["candidate_kind"] != "business"
                or source.source_hash != member["candidate_source_hash"]
                or artifact.metadata.get("package_manifest_hash") != member["source_manifest_hash"]
                or artifact.metadata.get("replacement_point") != member["replacement_point"]
                or artifact.metadata.get("candidate_kind") != "business"
                or any(p.profile != round_["adapter_profile"] for p in build.adapter_provenance)
            ):
                raise Conflict("Formal build result differs from frozen intake")
            baseline = connection.execute(
                "SELECT s.* FROM baseline_epochs b JOIN source_snapshots s "
                "ON s.snapshot_id = b.source_snapshot_id WHERE b.baseline_epoch_id = %s FOR SHARE",
                (round_["baseline_epoch_id"],),
            ).fetchone()
            if baseline is None or (
                baseline["snapshot_id"] != source.parent_snapshot_id
                or baseline["source_hash"] != member["baseline_source_hash"]
                or baseline["synthetic"] or not baseline["clean"]
                or baseline["repository"] != source.repository
            ):
                raise Conflict("Formal build Baseline differs from its durable parent")
            # Real Overlay builds commit the reviewed replacement. Candidate HEAD
            # must be a direct child of Baseline, not equal to Baseline HEAD.
            try:
                parent_path = file_uri_to_path(baseline["worktree_uri"])
                git = subprocess.run(
                    ["git", "--no-replace-objects", "-C", str(parent_path), "show", "-s",
                     "--format=%P%n%T", source.commit],
                    check=True, capture_output=True, text=True, timeout=30,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise Conflict("Formal build Git ancestry cannot be verified") from error
            if git.stdout.strip().splitlines() != [baseline["commit"], source.tree_hash]:
                raise Conflict("Formal build Git parent or tree differs from its snapshot")
            provenance = Jsonb([p.model_dump(mode="json") for p in build.adapter_provenance])
            _insert(connection, "source_snapshots", {
                **source.model_dump(mode="python"), "task_id": intent.task_id,
                "candidate_id": terminal.candidate_id, "synthetic": False,
                "idempotency_key": f"formal-build-source:{terminal.round_candidate_id}",
                "adapter_provenance": provenance,
            })
            _insert(connection, "artifacts", {
                **artifact.model_dump(mode="python"), "task_id": intent.task_id,
                "idempotency_key": f"formal-build-artifact:{terminal.round_candidate_id}",
                "adapter_provenance": provenance,
            })
            member = connection.execute(
                "UPDATE round_candidates SET state = 'built', artifact_id = %s, "
                "artifact_hash = %s, updated_at = now() WHERE round_candidate_id = %s RETURNING *",
                (artifact.artifact_id, artifact.content_hash, terminal.round_candidate_id),
            ).fetchone()
            connection.execute(
                "UPDATE candidates SET state = 'built', updated_at = now() WHERE candidate_id = %s",
                (terminal.candidate_id,),
            )
            connection.execute(
                "UPDATE search_rounds SET state = 'building', version = version + 1, "
                "updated_at = now() WHERE round_id = %s", (intent.round_id,),
            )
            _insert(connection, "task_events", {
                "task_id": intent.task_id, "event_type": "formal_candidate_build_recorded",
                "details": {
                    "round_candidate_id": str(terminal.round_candidate_id),
                    "result_hash": result_hash, "artifact_id": str(artifact.artifact_id),
                    "automatic_release_allowed": False,
                },
            })
            return dict(member)
