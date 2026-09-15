# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Read-only review binding for a future BW20 clock backend artifact.

Static identity checks never import or execute the artifact and never grant
clock authority. Runtime activation remains intentionally unimplemented.
"""

import argparse
import json
import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hcuopt.deployment.bw20_clock_journal import ClockJournal
from hcuopt.deployment.bw20_runtime_verify import _pinned, checked_digest
from hcuopt.deployment.bw20_stage0_runtime import RESOURCE


class BW20ClockBackendReviewManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    target_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9._-]+$")
    target_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resource_id: Literal[RESOURCE] = RESOURCE
    strategy: Literal["confirmed_default_auto_v1"] = "confirmed_default_auto_v1"
    backend_artifact: Path
    backend_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    journal_path: Path
    authority_provider_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:/-]+$"
    )
    recovery_policy: Literal["manual_fenced_takeover_v1"] = "manual_fenced_takeover_v1"
    sclk_target_mhz: Literal[1500] = 1500
    memory_clock_action: Literal["observe_only_no_write"] = "observe_only_no_write"
    hardware_acceptance_evidence: Path | None = None
    hardware_acceptance_sha256: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_paths_and_evidence(self):
        for path in (self.backend_artifact, self.journal_path):
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("backend review paths must be absolute and canonical")
        evidence = self.hardware_acceptance_evidence
        pin = self.hardware_acceptance_sha256
        if (evidence is None) != (pin is None):
            raise ValueError("hardware acceptance path and pin must be supplied together")
        if evidence is not None and (not evidence.is_absolute() or ".." in evidence.parts):
            raise ValueError("hardware acceptance path must be absolute and canonical")
        return self


def inspect_clock_backend_review(
    manifest_path: Path,
    *,
    manifest_sha256: str,
    expected_target_id: str,
    expected_target_fingerprint: str,
    journal: ClockJournal,
) -> dict:
    """Verify retained bytes and binding only; never import, execute or activate them."""
    manifest = BW20ClockBackendReviewManifest.model_validate_json(
        _pinned(manifest_path, manifest_sha256)
    )
    if (
        not isinstance(expected_target_fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_target_fingerprint) is None
    ):
        raise ValueError("independent target fingerprint required")
    if manifest.target_id != expected_target_id:
        raise ValueError("clock backend review target id changed")
    if manifest.target_fingerprint != expected_target_fingerprint:
        raise ValueError("clock backend review target fingerprint changed")
    if not isinstance(journal, ClockJournal) or manifest.journal_path != journal.path:
        raise ValueError("clock backend review must bind the deployment journal")
    journal_stat = journal.path.lstat()
    if journal.path.resolve(strict=True) != journal.path or not stat.S_ISREG(journal_stat.st_mode):
        raise ValueError("clock journal is missing or redirected")
    if checked_digest(manifest.backend_artifact, limit=64 * 1024**2) != manifest.backend_sha256:
        raise ValueError("clock backend artifact differs from review pin")

    blockers = []
    acceptance = manifest.hardware_acceptance_evidence
    if acceptance is None:
        blockers.append("hardware_acceptance_evidence_missing")
        acceptance_hash_verified = False
    else:
        acceptance_hash_verified = (
            checked_digest(acceptance, limit=16 * 1024**2)
            == manifest.hardware_acceptance_sha256
        )
        if not acceptance_hash_verified:
            raise ValueError("hardware acceptance evidence differs from review pin")
        blockers.append("hardware_acceptance_semantics_not_adjudicated")

    unresolved = journal.inspect_unresolved(RESOURCE)
    if unresolved:
        blockers.append("clock_journal_requires_reconciliation")
    return {
        "schema_version": "bw20-clock-backend-static-review-v1",
        "manifest_sha256": manifest_sha256,
        "target_id": manifest.target_id,
        "target_fingerprint": manifest.target_fingerprint,
        "resource_id": RESOURCE,
        "strategy": manifest.strategy,
        "backend_artifact_sha256": manifest.backend_sha256,
        "backend_artifact_hash_verified": True,
        "backend_imported": False,
        "backend_executed": False,
        "clock_mutation_performed": False,
        "journal_bound": True,
        "journal_clear": not unresolved,
        "hardware_acceptance_hash_verified": acceptance_hash_verified,
        "hardware_acceptance_semantics_verified": False,
        "static_inputs_verified": True,
        "review_blockers": blockers,
        "clock_control_bound": False,
        "execution_allowed": False,
        "stage0_accepted": False,
        "automatic_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--target-fingerprint", required=True)
    args = parser.parse_args(argv)

    manifest = BW20ClockBackendReviewManifest.model_validate_json(
        _pinned(args.manifest, args.sha256)
    )
    journal = ClockJournal.open_existing(manifest.journal_path)
    report = inspect_clock_backend_review(
        args.manifest,
        manifest_sha256=args.sha256,
        expected_target_id=args.target_id,
        expected_target_fingerprint=args.target_fingerprint,
        journal=journal,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
