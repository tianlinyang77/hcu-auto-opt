# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from hcuopt.measurement.evidence import canonical_json_bytes

BUILD_TERMINAL_STATES = frozenset({"built", "build_failed", "invalid"})


def candidate_family_hash(
    round_authority: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
) -> str:
    """Hash the immutable M2a intake family independently of insertion order."""

    canonical_members = []
    for member in sorted(members, key=lambda item: int(item["ordinal"])):
        canonical_members.append(
            {
                "ordinal": int(member["ordinal"]),
                "candidate_id": str(member["candidate_id"]),
                "round_candidate_id": str(member["round_candidate_id"]),
                "source_package_store_id": member["source_package_store_id"],
                "source_package_store_hash": member["source_package_store_hash"],
                "source_package_hash": member["source_package_hash"],
                "source_manifest_version": member["source_manifest_version"],
                "source_manifest_hash": member["source_manifest_hash"],
                "baseline_source_hash": member["baseline_source_hash"],
                "candidate_source_hash": member["candidate_source_hash"],
                "hotspot_id": str(round_authority["hotspot_id"]),
                "replacement_point": member["replacement_point"],
                "candidate_kind": member["candidate_kind"],
                "optimization_intent": member["optimization_intent"],
            }
        )
    return "sha256:" + hashlib.sha256(canonical_json_bytes(canonical_members)).hexdigest()


def artifact_family_hash(
    round_authority: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
) -> str:
    """Hash every immutable Build terminal, including failed members."""

    candidate_family = round_authority.get("candidate_family_hash")
    if not isinstance(candidate_family, str):
        raise ValueError("Artifact Family requires one frozen Candidate Family")
    declared_count = int(round_authority["declared_candidate_count"])
    if len(members) != declared_count:
        raise ValueError("Artifact Family requires the declared Candidate count")

    canonical_members = []
    ordinals: set[int] = set()
    candidate_ids: set[str] = set()
    round_candidate_ids: set[str] = set()
    for member in sorted(members, key=lambda item: int(item["ordinal"])):
        ordinal = int(member["ordinal"])
        candidate_id = str(member["candidate_id"])
        round_candidate_id = str(member["round_candidate_id"])
        if (
            ordinal in ordinals
            or candidate_id in candidate_ids
            or round_candidate_id in round_candidate_ids
        ):
            raise ValueError("Artifact Family contains duplicate member identities")
        ordinals.add(ordinal)
        candidate_ids.add(candidate_id)
        round_candidate_ids.add(round_candidate_id)
        state = str(member["state"])
        artifact_id = member.get("artifact_id")
        artifact_hash = member.get("artifact_hash")
        failure_code = member.get("terminal_failure_code")
        failure_hash = member.get("failure_evidence_hash")
        if (artifact_id is None) != (artifact_hash is None) or (
            failure_code is None
        ) != (failure_hash is None):
            raise ValueError("Artifact Family terminal requires complete identity pairs")
        has_artifact = artifact_id is not None and artifact_hash is not None
        has_failure = failure_code is not None and failure_hash is not None
        if state not in BUILD_TERMINAL_STATES or has_artifact == has_failure:
            raise ValueError("Artifact Family member is not one unambiguous Build terminal")
        if has_artifact and state != "built":
            raise ValueError("only a built member may bind an Artifact")
        if has_failure and state not in {"build_failed", "invalid"}:
            raise ValueError("Build failure evidence requires a failed or invalid member")
        canonical_members.append(
            {
                "ordinal": ordinal,
                "candidate_id": candidate_id,
                "round_candidate_id": round_candidate_id,
                "state": state,
                "artifact_id": str(artifact_id) if has_artifact else None,
                "artifact_hash": artifact_hash if has_artifact else None,
                "terminal_failure_code": failure_code if has_failure else None,
                "failure_evidence_hash": failure_hash if has_failure else None,
            }
        )
    value = {
        "candidate_family_hash": candidate_family,
        "members": canonical_members,
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()
