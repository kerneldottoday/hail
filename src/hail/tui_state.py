"""Unread marks for `hail tui`: which threads got agent messages since you last saw them. Pure functions.

Only messages you did not write count (a question you sent from another terminal is not "news"), and the
memory is kept to the threads on screen plus the open one, so it never grows over a long session.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .board import ThreadRow

UNREAD = "●"
STOP = "STOP"

Seen = Mapping[str, int]  # thread -> agent message count when last seen


def mark_seen(seen: Seen, rows: Sequence[ThreadRow], current: str | None, first: bool) -> dict[str, int]:
    """New memory after a refresh: the first load sees everything; the open thread is always seen."""
    on_screen = {r.thread for r in rows} | ({current} if current else set())
    start = {r.thread: r.from_others for r in rows} if first else {}
    opened = {r.thread: r.from_others for r in rows if r.thread == current}
    merged = {**start, **seen, **opened}
    return {thread: count for thread, count in merged.items() if thread in on_screen}


def is_unread(row: ThreadRow, seen: Seen) -> bool:
    return row.from_others > seen.get(row.thread, 0)


def flag(row: ThreadRow, seen: Seen) -> str:
    """The last cell of a thread row: the unread dot, STOP, or both."""
    return " ".join(part for part in (UNREAD if is_unread(row, seen) else "", STOP if row.stopped else "") if part)
