"""Machine output for --json: one object per answer, JSON Lines for lists (oldest first).

Field names are snake_case, times RFC 3339 in UTC, and a field that does not apply is left out.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from .config import Agent
from .runner import advice_enforced
from .store import Message, Task, ThreadSummary


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compact(row: Mapping[str, Any]) -> dict[str, Any]:
    """Drop fields that do not apply (None, empty text or list); keep False and 0."""
    return {k: v for k, v in row.items() if v is not None and v != "" and v != [] and v != ()}


def dumps(row: Mapping[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False)


def print_lines(rows: Iterable[Mapping[str, Any]]) -> None:
    for row in rows:
        print(dumps(row))


def agent_row(agent: Agent) -> dict[str, Any]:
    return compact(
        {
            "name": agent.name,
            "preset": agent.preset,
            "host": agent.host,
            "description": agent.description,
            "skills": list(agent.skills),
            "cost": agent.cost,
            "advice_enforced": advice_enforced(agent),
            "forced_command": agent.forced_command,
            "max_calls_per_day": agent.max_calls_per_day,
        }
    )


def message_row(msg: Message) -> dict[str, Any]:
    return compact(
        {
            "id": msg.id,
            "date": iso(msg.ts),
            "thread": msg.thread,
            "hop": msg.hop,
            "sender": msg.sender,
            "recipient": msg.recipient,
            "kind": msg.kind,
            "mode": msg.mode,
            "text": msg.text,
        }
    )


def thread_row(t: ThreadSummary) -> dict[str, Any]:
    return {
        "thread": t.thread,
        "started": iso(t.started),
        "last": iso(t.last),
        "messages": t.count,
        "stopped": t.stopped,
    }


def task_row(task: Task) -> dict[str, Any]:
    req = task.request
    return compact(
        {
            "id": task.id,
            "status": task.status,
            "agent": req.get("agent"),
            "skill": req.get("skill"),
            "thread": req.get("thread"),
            "ref": task.ref,
            "created": iso(task.created),
            "updated": iso(task.updated),
            "result": task.result,
            "error": task.error,
        }
    )
