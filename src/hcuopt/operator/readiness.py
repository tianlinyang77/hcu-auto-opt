# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from hcuopt.contracts.formal_readiness_v1 import (
    FormalReadinessEvidenceResult,
    FormalReadinessGateResult,
    FormalReadinessManifest,
    FormalReadinessReport,
)
from hcuopt.measurement.evidence import canonical_json_bytes


class FormalReadinessError(ValueError):
    """One readiness manifest cannot be parsed or verified safely."""


def load_formal_readiness_manifest(path: Path) -> FormalReadinessManifest:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise FormalReadinessError(f"cannot read Formal readiness manifest: {error}") from error
    except yaml.YAMLError as error:
        raise FormalReadinessError(f"invalid Formal readiness YAML: {error}") from error
    if not isinstance(raw, dict):
        raise FormalReadinessError("Formal readiness manifest must contain one mapping")
    try:
        return FormalReadinessManifest.model_validate(raw)
    except ValidationError as error:
        raise FormalReadinessError(
            f"Formal readiness manifest violates its frozen Contract: {error}"
        ) from error


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, digest_mode: str) -> str:
    if digest_mode == "text_lf":
        return _sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def formal_readiness_manifest_hash(manifest: FormalReadinessManifest) -> str:
    return _sha256_bytes(canonical_json_bytes(manifest))


def formal_readiness_report_hash(report: FormalReadinessReport) -> str:
    return _sha256_bytes(canonical_json_bytes(report))


class FormalReadinessAuditor:
    """Verify one no-HCU readiness snapshot without granting execution authority."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(
        self,
        manifest: FormalReadinessManifest,
        repository_root: Path,
    ) -> FormalReadinessReport:
        root = repository_root.resolve(strict=True)
        if not root.is_dir():
            raise FormalReadinessError("Formal readiness repository root must be a directory")

        evidence_cache: dict[tuple[str, str, str], str] = {}
        results: list[FormalReadinessGateResult] = []
        verified_paths: set[str] = set()
        blockers: set[str] = set()
        for gate in manifest.gates:
            statuses = []
            evidence_results: list[FormalReadinessEvidenceResult] = []
            for reference in gate.evidence:
                identity = (reference.path, reference.sha256, reference.digest_mode)
                status = evidence_cache.get(identity)
                if status is None:
                    status = self._verify_evidence(
                        root,
                        reference.path,
                        reference.sha256,
                        reference.digest_mode,
                    )
                    evidence_cache[identity] = status
                statuses.append(status)
                evidence_results.append(
                    FormalReadinessEvidenceResult(
                        path=reference.path,
                        sha256=reference.sha256,
                        digest_mode=reference.digest_mode,
                        evidence_type=reference.evidence_type,
                        status=status,
                    )
                )
                if status == "verified":
                    verified_paths.add(reference.path)
            evidence_status = self._combined_evidence_status(statuses)
            effective_status = gate.status if evidence_status == "verified" else "block"
            # This gate records that the separate project-owner decision is still
            # absent. It must stay on hold while deciding whether the implementation
            # is ready to request that decision; invalid policy evidence still blocks.
            owner_decision_pending = (
                gate.code == "owner_window_authorization" and effective_status == "hold"
            )
            if effective_status != "pass" and not owner_decision_pending:
                blockers.add(gate.code)
            if evidence_status != "verified":
                blockers.add(f"{gate.code}_evidence_{evidence_status}")
            results.append(
                FormalReadinessGateResult(
                    code=gate.code,
                    owner=gate.owner,
                    declared_status=gate.status,
                    effective_status=effective_status,
                    evidence_status=evidence_status,
                    summary=gate.summary,
                    required_action=gate.required_action,
                    evidence=tuple(evidence_results),
                )
            )

        for review in manifest.reviews:
            if review.decision != "accepted_for_formal_window":
                blockers.add(f"review_{review.owner.lower()}_{review.decision}")
            elif review.evidence is not None:
                evidence_status = self._verify_evidence(
                    root,
                    review.evidence.path,
                    review.evidence.sha256,
                    review.evidence.digest_mode,
                )
                if evidence_status != "verified":
                    blockers.add(
                        f"review_{review.owner.lower()}_evidence_{evidence_status}"
                    )
                else:
                    verified_paths.add(review.evidence.path)

        decision = "hold" if blockers else "ready_for_window_authorization"
        generated_at = self.clock()
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise FormalReadinessError("Formal readiness clock must be timezone-aware")
        return FormalReadinessReport(
            audit_id=manifest.audit_id,
            audit_base_commit=manifest.audit_base_commit,
            manifest_hash=formal_readiness_manifest_hash(manifest),
            generated_at=generated_at,
            decision=decision,
            gate_results=tuple(results),
            reviews=manifest.reviews,
            blocker_codes=tuple(sorted(blockers)),
            verified_evidence_count=len(verified_paths),
        )

    @staticmethod
    def _verify_evidence(
        root: Path,
        relative_path: str,
        expected_hash: str,
        digest_mode: str,
    ) -> str:
        unresolved = root.joinpath(*relative_path.split("/"))
        cursor = root
        for part in relative_path.split("/"):
            cursor /= part
            if cursor.is_symlink():
                return "invalid_path"
        try:
            path = unresolved.resolve(strict=True)
        except OSError:
            return "missing"
        if root != path and root not in path.parents:
            return "invalid_path"
        if not path.is_file():
            return "missing"
        try:
            actual_hash = _sha256_file(path, digest_mode)
        except OSError:
            return "missing"
        return "verified" if actual_hash == expected_hash else "hash_mismatch"

    @staticmethod
    def _combined_evidence_status(statuses: list[str]) -> str:
        for status in ("invalid_path", "missing", "hash_mismatch"):
            if status in statuses:
                return status
        return "verified"
