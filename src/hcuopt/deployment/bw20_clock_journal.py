# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

"""Local durable clock intent log, NOT a resource lease or recovery authority.

Deploy one pinned journal on durable local storage per host, in an operator-owned
directory. Never use a fresh temporary path to bypass unresolved operations. The
BW20 Worker consults this journal before claim, execution and cleanup settlement.
"""

import json
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class ClockJournalError(RuntimeError):
    pass


class ClockJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        if (not self.path.is_absolute() or self.path.resolve() != self.path
                or not self.path.parent.is_dir()):
            raise ValueError("clock journal requires an explicit durable local path")
        with self._connection(create=True) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS clock_operations (
                    operation_id TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    original_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS clock_unresolved_resource
                    ON clock_operations(resource_id) WHERE state != 'restored';
                CREATE TABLE IF NOT EXISTS clock_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS clock_recovery_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE UNIQUE INDEX IF NOT EXISTS clock_active_recovery_resource
                    ON clock_recovery_attempts(resource_id) WHERE state = 'claimed';
            """)

    @classmethod
    def open_existing(cls, path: Path) -> "ClockJournal":
        """Open a provisioned journal without creating or migrating any bytes."""
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("clock journal requires an absolute canonical path")
        try:
            resolved = path.resolve(strict=True)
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ClockJournalError("existing clock journal is unavailable") from exc
        if resolved != path or not stat.S_ISREG(mode):
            raise ClockJournalError("clock journal is missing or redirected")
        journal = cls.__new__(cls)
        journal.path = path
        required = {"clock_operations", "clock_events", "clock_recovery_attempts"}
        try:
            with journal._readonly_connection() as conn:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table'"
                    )
                }
                if not required.issubset(tables):
                    raise ClockJournalError("existing clock journal schema is incomplete")
                conn.execute(
                    "SELECT operation_id,resource_id,authorization_id,original_json,state "
                    "FROM clock_operations LIMIT 0"
                )
                conn.execute(
                    "SELECT attempt_id,operation_id,resource_id,authorization_id,"
                    "fencing_token,state FROM clock_recovery_attempts LIMIT 0"
                )
        except sqlite3.DatabaseError as exc:
            raise ClockJournalError("existing clock journal is invalid") from exc
        return journal

    @contextmanager
    def _connection(self, *, create=False):
        # SQLite FULL synchronizes the rollback journal and database at commit.
        # Durability still depends on the filesystem/device honoring flushes.
        # Subsequent reads must not silently replace a missing journal with an
        # empty database. Provisioning belongs only to explicit construction.
        if self.path.resolve() != self.path:
            raise ClockJournalError("clock journal path redirected")
        conn = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                               uri=True, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _readonly_connection(self):
        if self.path.resolve() != self.path:
            raise ClockJournalError("clock journal path redirected")
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def begin(self, *, resource_id: str, authorization_id: str, original: dict) -> str:
        operation_id = str(uuid4())
        with self._connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO clock_operations "
                    "(operation_id,resource_id,authorization_id,original_json,state) "
                    "VALUES (?,?,?,?,'mutation_possible')",
                    (operation_id, resource_id, authorization_id,
                     json.dumps(original, sort_keys=True, allow_nan=False)),
                )
            except sqlite3.IntegrityError as exc:
                raise ClockJournalError("unresolved clock operation blocks resource reuse") from exc
            conn.execute("INSERT INTO clock_events(operation_id,state) VALUES (?,?)",
                         (operation_id, "mutation_possible"))
        return operation_id

    def transition(self, operation_id: str, *, expected: str, state: str):
        allowed = {
            "mutation_possible": {"active", "restoring", "reconciliation_required"},
            "active": {"restoring", "reconciliation_required"},
            "restoring": {"restored", "reconciliation_required"},
        }
        if state not in allowed.get(expected, set()):
            raise ClockJournalError("invalid clock journal transition")
        with self._connection() as conn:
            result = conn.execute(
                "UPDATE clock_operations SET state=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE operation_id=? AND state=?",
                (state, operation_id, expected),
            )
            if result.rowcount != 1:
                raise ClockJournalError("clock journal state changed or operation missing")
            conn.execute("INSERT INTO clock_events(operation_id,state) VALUES (?,?)",
                         (operation_id, state))

    def unresolved(self, resource_id: str) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM clock_operations WHERE resource_id=? AND state!='restored'",
                (resource_id,),
            )]

    def inspect_unresolved(self, resource_id: str) -> list[dict]:
        """Strict read-only inspection for preflight/reporting, never settlement."""
        with self._readonly_connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM clock_operations WHERE resource_id=? AND state!='restored'",
                (resource_id,),
            )]

    def require_clear(self, resource_id: str) -> None:
        if self.unresolved(resource_id):
            raise ClockJournalError("unresolved clock intent requires resource reconciliation")

    def claim_recovery(
        self,
        *,
        resource_id: str,
        authorization_id: str,
        fencing_token: int,
    ) -> dict:
        if not isinstance(authorization_id, str) or not authorization_id.strip():
            raise ValueError("explicit recovery authorization reference required")
        if type(fencing_token) is not int or fencing_token < 1:
            raise ValueError("positive recovery fencing token required")
        attempt_id = str(uuid4())
        recoverable = {"mutation_possible", "active", "restoring", "reconciliation_required"}
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute(
                "SELECT * FROM clock_operations WHERE resource_id=? AND state!='restored'",
                (resource_id,),
            ))
            if len(rows) != 1 or rows[0]["state"] not in recoverable:
                raise ClockJournalError("clock operation is not available for recovery")
            operation = dict(rows[0])
            result = conn.execute(
                "UPDATE clock_operations SET state='recovery_claimed',updated_at=CURRENT_TIMESTAMP "
                "WHERE operation_id=? AND state=?",
                (operation["operation_id"], operation["state"]),
            )
            if result.rowcount != 1:
                raise ClockJournalError("clock recovery claim lost its journal race")
            try:
                conn.execute(
                    "INSERT INTO clock_recovery_attempts "
                    "(attempt_id,operation_id,resource_id,authorization_id,fencing_token,state) "
                    "VALUES (?,?,?,?,?,'claimed')",
                    (attempt_id, operation["operation_id"], resource_id,
                     authorization_id, fencing_token),
                )
            except sqlite3.IntegrityError as exc:
                raise ClockJournalError("another clock recovery attempt is active") from exc
            conn.execute(
                "INSERT INTO clock_events(operation_id,state) VALUES (?,?)",
                (operation["operation_id"], f"recovery_claimed:{attempt_id}"),
            )
        return {
            **operation,
            "attempt_id": attempt_id,
            "recovery_authorization_id": authorization_id,
            "recovery_fencing_token": fencing_token,
        }

    def finish_recovery(self, attempt_id: str, *, restored: bool) -> None:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("recovery attempt id required")
        operation_state = "restored" if restored else "reconciliation_required"
        attempt_state = "restored" if restored else "failed"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT operation_id FROM clock_recovery_attempts "
                "WHERE attempt_id=? AND state='claimed'",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ClockJournalError("clock recovery attempt is missing or already finished")
            result = conn.execute(
                "UPDATE clock_operations SET state=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE operation_id=? AND state='recovery_claimed'",
                (operation_state, row["operation_id"]),
            )
            if result.rowcount != 1:
                raise ClockJournalError("clock recovery no longer owns the journal operation")
            result = conn.execute(
                "UPDATE clock_recovery_attempts SET state=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE attempt_id=? AND state='claimed'",
                (attempt_state, attempt_id),
            )
            if result.rowcount != 1:
                raise ClockJournalError("clock recovery attempt state changed")
            conn.execute(
                "INSERT INTO clock_events(operation_id,state) VALUES (?,?)",
                (row["operation_id"], f"recovery_{attempt_state}:{attempt_id}"),
            )

    def recovery_attempts(self, resource_id: str) -> list[dict]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM clock_recovery_attempts WHERE resource_id=? ORDER BY updated_at",
                (resource_id,),
            )]
