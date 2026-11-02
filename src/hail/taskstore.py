"""Background tasks in the journal: create (with an optional idempotency ref), claim, update, reap."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import UsageError
from .redact import redact

ACTIVE = "('submitted', 'working')"


@dataclass(frozen=True)
class Task:
    id: str
    status: str  # submitted | working | completed | failed
    request: dict[str, Any]
    result: str
    error: str
    created: float
    updated: float
    ref: str | None = None  # the --ref that names it, if any


def _ref_hit(db: sqlite3.Connection, ref: str, digest: str) -> str | None:
    """The live task already named ``ref`` (a failed one may be retried), or None."""
    row = db.execute(
        "SELECT r.task, r.digest, t.status FROM task_refs r LEFT JOIN tasks t ON t.id = r.task WHERE r.ref = ?",
        (ref,),
    ).fetchone()
    if row is None or row[2] is None:
        return None
    if row[1] != digest:
        raise UsageError(f"ref '{ref}' already names another request: use a new ref")
    return None if row[2] == "failed" else str(row[0])


class TaskStore:
    """Task methods of ``Store``, which provides the database helpers below."""

    path: Path

    def _db(self) -> AbstractContextManager[sqlite3.Connection]:
        raise NotImplementedError

    def _one(self, sql: str, args: tuple[Any, ...]) -> tuple[Any, ...] | None:
        raise NotImplementedError

    def count_active_tasks(self) -> int:
        row = self._one(f"SELECT COUNT(*) FROM tasks WHERE status IN {ACTIVE}", ())
        return int(row[0]) if row else 0

    def reap_stale(self, max_age: float) -> int:
        """Fail tasks not updated for ``max_age`` seconds: their worker died (reboot, kill)."""
        now = time.time()
        with self._db() as db:
            cur = db.execute(
                f"UPDATE tasks SET status = 'failed', error = ?, updated = ? WHERE status IN {ACTIVE} AND updated < ?",
                ("stale: the background worker stopped without reporting", now, now - max_age),
            )
            return cur.rowcount

    def create_task(self, task_id: str, request: dict[str, Any], ref: str | None = None, digest: str = "") -> str:
        """Record a task and return its id: the existing one if ``ref`` already names a live task."""
        now = time.time()
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")  # two sends with the same ref cannot both create a task
            existing = _ref_hit(db, ref, digest) if ref else None
            if existing is None:
                db.execute(
                    "INSERT INTO tasks (id, created, updated, status, request) VALUES (?,?,?,?,?)",
                    (task_id, now, now, "submitted", json.dumps(request, ensure_ascii=False)),
                )
                if ref:
                    db.execute(
                        "INSERT OR REPLACE INTO task_refs (ref, task, digest) VALUES (?,?,?)", (ref, task_id, digest)
                    )
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return existing or task_id

    def task_for_ref(self, ref: str, digest: str) -> str | None:
        with self._db() as db:
            return _ref_hit(db, ref, digest)

    def update_task(self, task_id: str, status: str, result: str = "", error: str = "") -> None:
        with self._db() as db:
            db.execute(
                "UPDATE tasks SET status = ?, result = ?, error = ?, updated = ? WHERE id = ?",
                (status, redact(result), redact(error), time.time(), task_id),
            )

    def claim_task(self, task_id: str) -> bool:
        """Move a submitted task to 'working'; False if something else already did."""
        with self._db() as db:
            cur = db.execute(
                "UPDATE tasks SET status = 'working', updated = ? WHERE id = ? AND status = 'submitted'",
                (time.time(), task_id),
            )
            return cur.rowcount == 1

    def get_task(self, task_id: str) -> Task | None:
        row = self._one(
            "SELECT t.id, t.status, t.request, t.result, t.error, t.created, t.updated, r.ref "
            "FROM tasks t LEFT JOIN task_refs r ON r.task = t.id WHERE t.id = ?",
            (task_id,),
        )
        if not row:
            return None
        return Task(row[0], row[1], json.loads(row[2]), row[3], row[4], row[5], row[6], row[7])
