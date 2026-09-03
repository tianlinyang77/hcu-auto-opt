# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol, runtime_checkable

from hcuopt.contracts.m2_formal_execution_v1 import M2FormalPhaseExecutionRecord
from hcuopt.domain.enums import RoundPhase
from hcuopt.measurement.harness import MeasurementSafetyError

from .m2_formal_receipt import _prepare_no_follow_directory, _require_contained


@runtime_checkable
class M2FormalPhaseIsolationAuthority(Protocol):
    """Durable, atomic authority for Formal cross-phase uniqueness claims."""

    def claim(self, record: M2FormalPhaseExecutionRecord) -> None: ...


class SqliteM2FormalPhaseIsolationAuthority:
    """Single-host durable authority suitable for the locked Formal worker.

    The database is deliberately required from deployment configuration.  It is
    not an in-memory default, so worker restarts and independent Search/Holdout
    worker processes observe the same uniqueness claims.
    """

    def __init__(self, database_path: Path) -> None:
        root = _prepare_no_follow_directory(database_path.parent)
        self.database_path = _require_contained(root, database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS formal_phase_plans (
                    round_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    phase_plan_hash TEXT NOT NULL,
                    measurement_plan_hash TEXT NOT NULL,
                    PRIMARY KEY (round_id, phase)
                );
                CREATE TABLE IF NOT EXISTS formal_measurement_claims (
                    execution_id TEXT PRIMARY KEY,
                    round_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    measurement_id TEXT NOT NULL UNIQUE,
                    raw_evidence_uri TEXT NOT NULL UNIQUE,
                    raw_evidence_hash TEXT NOT NULL UNIQUE,
                    baseline_sample_set_hash TEXT NOT NULL UNIQUE,
                    process_identity_set_hash TEXT NOT NULL UNIQUE,
                    cache_namespace_set_hash TEXT NOT NULL UNIQUE
                );
                """
            )

    def claim(self, record: M2FormalPhaseExecutionRecord) -> None:
        if record.status != "succeeded":
            return
        reference = record.measurement_ref
        if reference is None:
            raise MeasurementSafetyError(
                "successful Formal execution omitted its Round Measurement Ref"
            )
        phase = record.binding.phase
        other_phase = (
            RoundPhase.HOLDOUT if phase is RoundPhase.SEARCH else RoundPhase.SEARCH
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            other = connection.execute(
                """
                SELECT phase_plan_hash, measurement_plan_hash
                FROM formal_phase_plans
                WHERE round_id = ? AND phase = ?
                """,
                (str(record.binding.round_id), other_phase.value),
            ).fetchone()
            if other is not None and (
                record.binding.phase_plan_hash in other
                or record.binding.measurement_plan_hash in other
            ):
                raise MeasurementSafetyError(
                    "Search and Holdout require distinct execution plans"
                )
            current = connection.execute(
                """
                SELECT phase_plan_hash, measurement_plan_hash
                FROM formal_phase_plans
                WHERE round_id = ? AND phase = ?
                """,
                (str(record.binding.round_id), phase.value),
            ).fetchone()
            plans = (
                record.binding.phase_plan_hash,
                record.binding.measurement_plan_hash,
            )
            if current is not None and current != plans:
                raise MeasurementSafetyError(
                    "one Formal phase cannot use multiple execution plans"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO formal_phase_plans (
                    round_id, phase, phase_plan_hash, measurement_plan_hash
                ) VALUES (?, ?, ?, ?)
                """,
                (str(record.binding.round_id), phase.value, *plans),
            )
            connection.execute(
                """
                INSERT INTO formal_measurement_claims (
                    execution_id, round_id, phase, measurement_id,
                    raw_evidence_uri, raw_evidence_hash,
                    baseline_sample_set_hash, process_identity_set_hash,
                    cache_namespace_set_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.execution_id),
                    str(record.binding.round_id),
                    phase.value,
                    str(reference.measurement_id),
                    reference.raw_evidence_uri,
                    reference.raw_evidence_hash,
                    reference.baseline_sample_set_hash,
                    reference.process_identity_set_hash,
                    reference.cache_namespace_set_hash,
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise MeasurementSafetyError(
                "Formal phase isolation rejected a reused execution or evidence identity"
            ) from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


__all__ = [
    "M2FormalPhaseIsolationAuthority",
    "SqliteM2FormalPhaseIsolationAuthority",
]
