# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from hcuopt.measurement.evidence import canonical_json_bytes


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
