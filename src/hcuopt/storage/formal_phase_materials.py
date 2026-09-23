# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Deployment-scoped reads of durable phase inputs; never advance a Round."""

from dataclasses import dataclass
from uuid import UUID

from hcuopt.contracts.m2 import RoundCandidate, SearchRound
from hcuopt.contracts.m2_formal_authority_v1 import FormalAuthorityContextDescriptor
from hcuopt.domain.errors import Conflict
from hcuopt.storage.formal_phase_journal import PostgresFormalPhaseJournal


@dataclass(frozen=True)
class FormalPhaseMaterials:
    round_authority: SearchRound
    formal_authority: FormalAuthorityContextDescriptor
    member: RoundCandidate


class PostgresFormalPhaseMaterialReader:
    def __init__(self, journal: PostgresFormalPhaseJournal):
        self.journal = journal

    def load(self, candidate_id: UUID) -> FormalPhaseMaterials:
        """Read one locked snapshot; not a Lease or permission to start hardware.

        B checkpoints must re-read before and after sampling. SQL locks are not
        held across external work; ongoing cancellation still needs B cooperation.
        """
        journal = self.journal
        claims = journal.claims
        claims.assert_active(journal.intent_id, journal.worker_id, journal.claim_token)
        repository = claims.dispatcher.repository
        with repository.connection() as connection:
            intent = claims._lock_deployment_intent(connection, journal.intent_id)
            bound = next(
                (item for item in intent.candidate_bindings if item.candidate_id == candidate_id),
                None,
            )
            if bound is None:
                raise Conflict("Formal material candidate is outside the claimed Intent")
            round_row = connection.execute(
                "SELECT * FROM search_rounds WHERE round_id = %s AND task_id = %s FOR SHARE",
                (intent.round_id, intent.task_id),
            ).fetchone()
            member_row = connection.execute(
                "SELECT * FROM round_candidates WHERE round_id = %s "
                "AND round_candidate_id = %s AND candidate_id = %s FOR SHARE",
                (intent.round_id, bound.round_candidate_id, candidate_id),
            ).fetchone()
            if round_row is None or member_row is None:
                raise Conflict("Formal material Round or member is not durable")
            if (
                round_row["artifact_family_hash"] is None
                or member_row["artifact_id"] is None
                or member_row["artifact_hash"] is None
            ):
                raise Conflict(
                    "Formal material requires completed build and frozen Artifact Family"
                )
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = %s AND task_id = %s "
                "AND candidate_id = %s AND content_hash = %s FOR SHARE",
                (
                    member_row["artifact_id"], intent.task_id,
                    candidate_id, member_row["artifact_hash"],
                ),
            ).fetchone()
            if artifact is None or artifact["synthetic"] is not False:
                raise Conflict("Formal material requires a durable non-synthetic Artifact")
            context_row = connection.execute(
                "SELECT * FROM formal_round_authority_contexts "
                "WHERE round_id = %s AND task_id = %s FOR SHARE",
                (intent.round_id, intent.task_id),
            ).fetchone()
            if context_row is None:
                raise Conflict("Formal material requires a durable sealed Authority Context")
            return FormalPhaseMaterials(
                round_authority=repository._search_round_authority(round_row),
                formal_authority=repository._formal_authority_context(context_row),
                member=RoundCandidate.model_validate(
                    {name: member_row[name] for name in RoundCandidate.model_fields}
                ),
            )
