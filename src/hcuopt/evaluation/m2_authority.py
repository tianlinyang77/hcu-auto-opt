# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from hcuopt.contracts.m2 import SearchRound
from hcuopt.contracts.platform_v1 import SHA256_PATTERN
from hcuopt.domain.enums import SearchRoundRunMode, SearchRoundState
from hcuopt.evaluation.m2_models import FrozenEvaluationModel
from hcuopt.measurement.evidence import canonical_json_bytes

M2_HOLDOUT_AUTHORITY_SCHEMA_VERSION = "m2a-scripted-holdout-authority-v1"
M2_HOLDOUT_REVEAL_LEASE_SCHEMA_VERSION = "m2a-holdout-reveal-lease-v1"
M2_HOLDOUT_REVEAL_SCHEMA_VERSION = "m2a-holdout-reveal-v1"


class HoldoutAuthorityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HoldoutPlanCommitment(FrozenEvaluationModel):
    schema_version: Literal["m2a-scripted-holdout-authority-v1"] = (
        M2_HOLDOUT_AUTHORITY_SCHEMA_VERSION
    )
    round_id: UUID
    commitment: str = Field(pattern=SHA256_PATTERN)
    commitment_scheme: Literal["sha256-nonce-v1"] = "sha256-nonce-v1"
    authority_id: str = Field(min_length=1, max_length=300)
    authority_hash: str = Field(pattern=SHA256_PATTERN)
    synthetic: Literal[True] = True
    created_at: datetime

    @model_validator(mode="after")
    def require_aware_created_at(self) -> HoldoutPlanCommitment:
        _require_aware(self.created_at, "Holdout commitment time")
        return self


class HoldoutRevealLease(FrozenEvaluationModel):
    schema_version: Literal["m2a-holdout-reveal-lease-v1"] = (
        M2_HOLDOUT_REVEAL_LEASE_SCHEMA_VERSION
    )
    reveal_lease_id: UUID
    round_id: UUID
    holdout_family_hash: str = Field(pattern=SHA256_PATTERN)
    authorized_worker_id: str = Field(min_length=1, max_length=300)
    execution_lease_id: UUID
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    issued_at: datetime
    expires_at: datetime
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def require_bounded_lifetime(self) -> HoldoutRevealLease:
        _require_aware(self.issued_at, "Reveal Lease issue time")
        _require_aware(self.expires_at, "Reveal Lease expiry time")
        if self.expires_at <= self.issued_at:
            raise ValueError("Reveal Lease expiry must follow issue time")
        return self


class HoldoutRevealResult(FrozenEvaluationModel):
    schema_version: Literal["m2a-holdout-reveal-v1"] = (
        M2_HOLDOUT_REVEAL_SCHEMA_VERSION
    )
    reveal_lease_id: UUID
    round_id: UUID
    holdout_family_hash: str = Field(pattern=SHA256_PATTERN)
    commitment: str = Field(pattern=SHA256_PATTERN)
    plan_hash: str = Field(pattern=SHA256_PATTERN)
    canonical_plan_json: str = Field(min_length=2, max_length=1_000_000)
    nonce_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_worker_id: str = Field(min_length=1, max_length=300)
    execution_lease_id: UUID
    resource_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    authority_id: str = Field(min_length=1, max_length=300)
    authority_hash: str = Field(pattern=SHA256_PATTERN)
    reveal_evidence_hash: str = Field(pattern=SHA256_PATTERN)
    revealed_at: datetime
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def require_aware_reveal_time(self) -> HoldoutRevealResult:
        _require_aware(self.revealed_at, "Holdout reveal time")
        encoded = self.canonical_plan_json.encode("utf-8")
        if _sha256(encoded) != self.plan_hash:
            raise ValueError("revealed canonical Plan does not match plan_hash")
        if _sha256(bytes.fromhex(self.nonce_hex) + encoded) != self.commitment:
            raise ValueError("revealed nonce and Plan do not match commitment")
        if _sha256(canonical_json_bytes(self.evidence_payload())) != (
            self.reveal_evidence_hash
        ):
            raise ValueError("Holdout reveal evidence does not match its payload Hash")
        return self

    def evidence_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reveal_lease_id": str(self.reveal_lease_id),
            "round_id": str(self.round_id),
            "holdout_family_hash": self.holdout_family_hash,
            "commitment": self.commitment,
            "plan_hash": self.plan_hash,
            "nonce_hex": self.nonce_hex,
            "authorized_worker_id": self.authorized_worker_id,
            "execution_lease_id": str(self.execution_lease_id),
            "resource_id": self.resource_id,
            "fencing_token": self.fencing_token,
            "authority_id": self.authority_id,
            "authority_hash": self.authority_hash,
            "revealed_at": self.revealed_at.isoformat(),
            "synthetic": True,
        }


@dataclass(frozen=True, slots=True)
class _StoredHoldoutPlan:
    encoded: bytes
    nonce: bytes
    commitment: HoldoutPlanCommitment


class SyntheticHoldoutPlanAuthority:
    """In-memory M2a fixture Store with one-time, identity-bound reveals."""

    def __init__(self, *, authority_id: str, authority_version: str) -> None:
        if not authority_id or len(authority_id) > 300:
            raise ValueError("Holdout Authority ID must be bounded non-empty text")
        if not authority_version or len(authority_version) > 200:
            raise ValueError("Holdout Authority version must be bounded non-empty text")
        self.authority_id = authority_id
        self.authority_version = authority_version
        self.authority_hash = _sha256(
            canonical_json_bytes(
                {
                    "schema_version": M2_HOLDOUT_AUTHORITY_SCHEMA_VERSION,
                    "authority_id": authority_id,
                    "authority_version": authority_version,
                    "synthetic": True,
                }
            )
        )
        self._plans: dict[UUID, _StoredHoldoutPlan] = {}
        self._leases: dict[UUID, HoldoutRevealLease] = {}
        self._round_lease_ids: dict[UUID, UUID] = {}
        self._consumed_lease_ids: set[UUID] = set()

    def commit_plan(
        self,
        *,
        round_id: UUID,
        plan: dict[str, Any],
        nonce: bytes | None = None,
        created_at: datetime | None = None,
    ) -> HoldoutPlanCommitment:
        if round_id in self._plans:
            raise HoldoutAuthorityError(
                "holdout_plan_already_committed",
                "Holdout Plan can be committed only once per Round",
            )
        created = created_at or _utcnow()
        _require_aware(created, "Holdout commitment time")
        nonce_value = nonce if nonce is not None else secrets.token_bytes(32)
        if len(nonce_value) != 32:
            raise ValueError("Holdout commitment nonce must contain exactly 256 bits")
        encoded = canonical_json_bytes(plan)
        commitment = HoldoutPlanCommitment(
            round_id=round_id,
            commitment=_sha256(nonce_value + encoded),
            authority_id=self.authority_id,
            authority_hash=self.authority_hash,
            created_at=created,
        )
        self._plans[round_id] = _StoredHoldoutPlan(
            encoded=encoded,
            nonce=bytes(nonce_value),
            commitment=commitment,
        )
        return commitment

    def issue_reveal_lease(
        self,
        *,
        round_authority: SearchRound,
        authorized_worker_id: str,
        execution_lease_id: UUID,
        resource_id: str,
        fencing_token: int,
        expires_at: datetime,
        issued_at: datetime | None = None,
        reveal_lease_id: UUID | None = None,
    ) -> HoldoutRevealLease:
        stored = self._require_round_authority(round_authority)
        if round_authority.state is not SearchRoundState.SEARCH_BARRIER:
            raise HoldoutAuthorityError(
                "holdout_plan_not_revealable",
                "Holdout reveal is allowed only after Search reaches its Barrier",
            )
        if round_authority.holdout_family_hash is None:
            raise HoldoutAuthorityError(
                "holdout_family_not_frozen",
                "Holdout reveal requires one non-empty frozen promoted family",
            )
        if any(
            value is not None
            for value in (
                round_authority.holdout_plan_hash,
                round_authority.holdout_reveal_lease_id,
                round_authority.holdout_reveal_evidence_hash,
            )
        ):
            raise HoldoutAuthorityError(
                "holdout_plan_already_revealed",
                "Round authority already contains Holdout reveal identities",
            )
        if round_authority.round_id in self._round_lease_ids:
            raise HoldoutAuthorityError(
                "holdout_reveal_lease_already_issued",
                "Holdout Reveal Lease can be issued only once per Round",
            )
        if not authorized_worker_id or len(authorized_worker_id) > 300:
            raise ValueError("authorized worker ID must be bounded non-empty text")
        if not resource_id or len(resource_id) > 200 or fencing_token < 1:
            raise ValueError("Reveal Lease requires a valid resource and Fencing token")
        issued = issued_at or _utcnow()
        lease = HoldoutRevealLease(
            reveal_lease_id=reveal_lease_id or uuid4(),
            round_id=round_authority.round_id,
            holdout_family_hash=round_authority.holdout_family_hash,
            authorized_worker_id=authorized_worker_id,
            execution_lease_id=execution_lease_id,
            resource_id=resource_id,
            fencing_token=fencing_token,
            issued_at=issued,
            expires_at=expires_at,
        )
        if stored.commitment.commitment != round_authority.holdout_plan_commitment:
            raise HoldoutAuthorityError(
                "holdout_commitment_mismatch",
                "Round commitment does not match the D-owned Holdout Plan",
            )
        self._leases[lease.reveal_lease_id] = lease
        self._round_lease_ids[lease.round_id] = lease.reveal_lease_id
        return lease

    def reveal(
        self,
        *,
        reveal_lease_id: UUID,
        round_id: UUID,
        authorized_worker_id: str,
        execution_lease_id: UUID,
        resource_id: str,
        fencing_token: int,
        revealed_at: datetime | None = None,
    ) -> HoldoutRevealResult:
        lease = self._leases.get(reveal_lease_id)
        if lease is None:
            raise HoldoutAuthorityError(
                "holdout_reveal_lease_invalid", "Holdout Reveal Lease does not exist"
            )
        if reveal_lease_id in self._consumed_lease_ids:
            raise HoldoutAuthorityError(
                "holdout_reveal_lease_consumed",
                "Holdout Reveal Lease has already been consumed",
            )
        revealed = revealed_at or _utcnow()
        _require_aware(revealed, "Holdout reveal time")
        if revealed >= lease.expires_at:
            raise HoldoutAuthorityError(
                "holdout_reveal_lease_expired", "Holdout Reveal Lease has expired"
            )
        actual = (
            round_id,
            authorized_worker_id,
            execution_lease_id,
            resource_id,
            fencing_token,
        )
        expected = (
            lease.round_id,
            lease.authorized_worker_id,
            lease.execution_lease_id,
            lease.resource_id,
            lease.fencing_token,
        )
        if actual != expected:
            raise HoldoutAuthorityError(
                "holdout_reveal_lease_invalid",
                "Reveal Lease does not authorize this Round, Worker, Lease, or Fence",
            )
        stored = self._plans.get(round_id)
        if stored is None:
            raise HoldoutAuthorityError(
                "holdout_plan_authority_unavailable",
                "D-owned Holdout Plan is unavailable",
            )
        reconstructed = _sha256(stored.nonce + stored.encoded)
        if reconstructed != stored.commitment.commitment:
            raise HoldoutAuthorityError(
                "holdout_commitment_mismatch",
                "Stored nonce and Plan do not reconstruct the frozen commitment",
            )
        plan_hash = _sha256(stored.encoded)
        evidence_payload = {
            "schema_version": M2_HOLDOUT_REVEAL_SCHEMA_VERSION,
            "reveal_lease_id": str(lease.reveal_lease_id),
            "round_id": str(round_id),
            "holdout_family_hash": lease.holdout_family_hash,
            "commitment": reconstructed,
            "plan_hash": plan_hash,
            "nonce_hex": stored.nonce.hex(),
            "authorized_worker_id": authorized_worker_id,
            "execution_lease_id": str(execution_lease_id),
            "resource_id": resource_id,
            "fencing_token": fencing_token,
            "authority_id": self.authority_id,
            "authority_hash": self.authority_hash,
            "revealed_at": revealed.isoformat(),
            "synthetic": True,
        }
        result = HoldoutRevealResult(
            reveal_lease_id=lease.reveal_lease_id,
            round_id=round_id,
            holdout_family_hash=lease.holdout_family_hash,
            commitment=reconstructed,
            plan_hash=plan_hash,
            canonical_plan_json=stored.encoded.decode("utf-8"),
            nonce_hex=stored.nonce.hex(),
            authorized_worker_id=authorized_worker_id,
            execution_lease_id=execution_lease_id,
            resource_id=resource_id,
            fencing_token=fencing_token,
            authority_id=self.authority_id,
            authority_hash=self.authority_hash,
            reveal_evidence_hash=_sha256(canonical_json_bytes(evidence_payload)),
            revealed_at=revealed,
        )
        self._consumed_lease_ids.add(reveal_lease_id)
        return result

    def _require_round_authority(self, round_authority: SearchRound) -> _StoredHoldoutPlan:
        if round_authority.run_mode is not SearchRoundRunMode.SCRIPTED:
            raise HoldoutAuthorityError(
                "holdout_plan_forbidden",
                "Synthetic Holdout Authority accepts only Scripted Rounds",
            )
        stored = self._plans.get(round_authority.round_id)
        if stored is None:
            raise HoldoutAuthorityError(
                "holdout_plan_authority_unavailable",
                "D-owned Holdout Plan is unavailable",
            )
        if (
            round_authority.holdout_plan_authority_id != self.authority_id
            or round_authority.holdout_plan_authority_hash != self.authority_hash
        ):
            raise HoldoutAuthorityError(
                "holdout_plan_authority_unavailable",
                "Round binds another Holdout Plan Authority",
            )
        return stored


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
