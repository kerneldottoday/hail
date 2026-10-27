"""The hail journal: every message, thread and background task, in one SQLite file.

SQLite ships with Python and handles several hail processes writing at once
(an agent consulting another while a background task finishes). One short
connection per operation keeps it safe across threads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, thread TEXT NOT NULL, hop INTEGER NOT NULL,
  sender TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread);
CREATE TABLE IF NOT EXISTS threads (
  thread TEXT PRIMARY KEY, created REAL NOT NULL, title TEXT NOT NULL DEFAULT '',
  stopped INTEGER NOT NULL DEFAULT 0, topic_id INTEGER
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
  request TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox_seen (
  agent TEXT NOT NULL, item TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (agent, item)
);
"""
MESSAGE_COLUMNS = "id, ts, thread, hop, sender, recipient, kind, mode, text"


@dataclass(frozen=True)
class Message:
    id: int
    ts: float
    thread: str
    hop: int
    sender: str
    recipient: str
    kind: str  # ask | reply | error | refused | human
    mode: str
    text: str


@dataclass(frozen=True)
class ThreadSummary:
    thread: str
    started: float
    last: float
    count: int
    stopped: bool


@dataclass(frozen=True)
class Task:
    id: str
    status: str  # submitted | working | completed | failed
    request: dict[str, Any]
    result: str
    error: str
    created: float
    updated: float


def _budget_reason(db: sqlite3.Connection, thread: str, now: float, max_calls: int, rate: int) -> str | None:
    stopped = db.execute("SELECT stopped FROM threads WHERE thread = ?", (thread,)).fetchone()
    if stopped and stopped[0]:
        return f"thread {thread} was stopped by the human (hail resume {thread} to reopen it)"
    asked = db.execute("SELECT COUNT(*) FROM messages WHERE thread = ? AND kind = 'ask'", (thread,)).fetchone()[0]
    if asked >= max_calls:
        return f"thread budget used ({max_calls} calls)"
    recent = db.execute("SELECT COUNT(*) FROM messages WHERE kind = 'ask' AND ts >= ?", (now - 60,)).fetchone()[0]
    if recent >= rate:
        return f"circuit breaker: more than {rate} calls in the last minute"
    return None


def hail_home() -> Path:
    env = os.environ.get("HAIL_HOME")
    return Path(env).expanduser() if env else Path.home() / ".hail"


def default_store() -> Store:
    return Store(hail_home() / "hail.db")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    # messages ---------------------------------------------------------------

    def add(self, thread: str, hop: int, sender: str, recipient: str, kind: str, mode: str, text: str) -> int:
        self.ensure_thread(thread, "")
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO messages (ts, thread, hop, sender, recipient, kind, mode, text) VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), thread, hop, sender, recipient, kind, mode, text),
            )
            return int(cur.lastrowid or 0)

    def try_ask(
        self, thread: str, hop: int, sender: str, recipient: str, mode: str, text: str, max_calls: int, rate: int
    ) -> str | None:
        """Check the thread and machine budgets and record the ask in one transaction.

        Returns why the ask was refused (the refusal is recorded instead), or None.
        """
        self.ensure_thread(thread, "")
        now = time.time()
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            reason = _budget_reason(db, thread, now, max_calls, rate)
            kind, body = ("refused", reason) if reason else ("ask", text)
            db.execute(
                "INSERT INTO messages (ts, thread, hop, sender, recipient, kind, mode, text) VALUES (?,?,?,?,?,?,?,?)",
                (now, thread, hop, sender, recipient, kind, mode, body),
            )
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return reason

    def count_active_tasks(self) -> int:
        row = self._one("SELECT COUNT(*) FROM tasks WHERE status IN ('submitted', 'working')", ())
        return int(row[0]) if row else 0

    def thread(self, thread: str) -> tuple[Message, ...]:
        return self._messages("WHERE thread = ? ORDER BY id", (thread,))

    def recent(self, limit: int, grep: str | None = None) -> tuple[Message, ...]:
        where, args = ("WHERE text LIKE ?", (f"%{grep}%",)) if grep else ("", ())
        rows = self._messages(f"{where} ORDER BY id DESC LIMIT ?", (*args, limit))
        return tuple(reversed(rows))

    def history(self, thread: str, agent: str) -> tuple[tuple[str, str], ...]:
        """Past asks to ``agent`` and its replies in ``thread``, oldest first."""
        rows = self._messages(
            "WHERE thread = ? AND ((recipient = ? AND kind = 'ask') OR (sender = ? AND kind = 'reply')) ORDER BY id",
            (thread, agent, agent),
        )
        return tuple((m.sender, m.text) for m in rows)

    def notes(self, thread: str) -> tuple[str, ...]:
        return tuple(m.text for m in self._messages("WHERE thread = ? AND kind = 'human' ORDER BY id", (thread,)))

    def depth_of(self, thread: str, agent: str) -> int:
        """Hop at which ``agent`` was last asked something in ``thread`` (0 = never)."""
        row = self._one(
            "SELECT hop FROM messages WHERE thread = ? AND recipient = ? AND kind = 'ask' ORDER BY id DESC LIMIT 1",
            (thread, agent),
        )
        return int(row[0]) if row else 0

    def count_asks(self, thread: str) -> int:
        row = self._one("SELECT COUNT(*) FROM messages WHERE thread = ? AND kind = 'ask'", (thread,))
        return int(row[0]) if row else 0

    def asks_since(self, since: float) -> int:
        row = self._one("SELECT COUNT(*) FROM messages WHERE kind = 'ask' AND ts >= ?", (since,))
        return int(row[0]) if row else 0

    def threads(self, limit: int) -> tuple[ThreadSummary, ...]:
        with self._db() as db:
            rows = db.execute(
                "SELECT m.thread, MIN(m.ts), MAX(m.ts), COUNT(*), COALESCE(t.stopped, 0) FROM messages m "
                "LEFT JOIN threads t ON t.thread = m.thread GROUP BY m.thread ORDER BY MAX(m.id) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(ThreadSummary(r[0], r[1], r[2], r[3], bool(r[4])) for r in rows)

    # threads ----------------------------------------------------------------

    def ensure_thread(self, thread: str, title: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO threads (thread, created, title) VALUES (?,?,?)", (thread, time.time(), title)
            )

    def set_stopped(self, thread: str, stopped: bool) -> None:
        self.ensure_thread(thread, "")
        with self._db() as db:
            db.execute("UPDATE threads SET stopped = ? WHERE thread = ?", (int(stopped), thread))

    def is_stopped(self, thread: str) -> bool:
        row = self._one("SELECT stopped FROM threads WHERE thread = ?", (thread,))
        return bool(row and row[0])

    def set_topic(self, thread: str, topic_id: int) -> None:
        self.ensure_thread(thread, "")
        with self._db() as db:
            db.execute("UPDATE threads SET topic_id = ? WHERE thread = ?", (topic_id, thread))

    def topic_of(self, thread: str) -> int | None:
        row = self._one("SELECT topic_id FROM threads WHERE thread = ?", (thread,))
        return int(row[0]) if row and row[0] is not None else None

    def thread_of_topic(self, topic_id: int) -> str | None:
        row = self._one("SELECT thread FROM threads WHERE topic_id = ?", (topic_id,))
        return str(row[0]) if row else None

    # tasks ------------------------------------------------------------------

    def create_task(self, task_id: str, request: dict[str, Any]) -> None:
        now = time.time()
        with self._db() as db:
            db.execute(
                "INSERT INTO tasks (id, created, updated, status, request) VALUES (?,?,?,?,?)",
                (task_id, now, now, "submitted", json.dumps(request, ensure_ascii=False)),
            )

    def update_task(self, task_id: str, status: str, result: str = "", error: str = "") -> None:
        with self._db() as db:
            db.execute(
                "UPDATE tasks SET status = ?, result = ?, error = ?, updated = ? WHERE id = ?",
                (status, result, error, time.time(), task_id),
            )

    def get_task(self, task_id: str) -> Task | None:
        row = self._one(
            "SELECT id, status, request, result, error, created, updated FROM tasks WHERE id = ?", (task_id,)
        )
        if not row:
            return None
        return Task(row[0], row[1], json.loads(row[2]), row[3], row[4], row[5], row[6])

    # meta -------------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return str(row[0]) if row else None

    # inbox ------------------------------------------------------------------

    def mark_seen(self, agent: str, items: list[str]) -> None:
        now = time.time()
        with self._db() as db:
            db.executemany(
                "INSERT OR IGNORE INTO inbox_seen (agent, item, ts) VALUES (?,?,?)", [(agent, i, now) for i in items]
            )

    def seen_items(self, agent: str) -> set[str]:
        with self._db() as db:
            rows = db.execute("SELECT item FROM inbox_seen WHERE agent = ?", (agent,)).fetchall()
        return {str(r[0]) for r in rows}

    # helpers ----------------------------------------------------------------

    def _messages(self, tail: str, args: tuple[Any, ...]) -> tuple[Message, ...]:
        with self._db() as db:
            rows = db.execute(f"SELECT {MESSAGE_COLUMNS} FROM messages {tail}", args).fetchall()
        return tuple(Message(*r) for r in rows)

    def _one(self, sql: str, args: tuple[Any, ...]) -> tuple[Any, ...] | None:
        with self._db() as db:
            return db.execute(sql, args).fetchone()
