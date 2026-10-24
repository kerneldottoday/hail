"""Background asks: hail_send returns a task id at once, a detached worker does the call.

The worker is ``python -m hail _task <id>``; it inherits the caller's
environment, so the hail identity (thread, hop, sender) is preserved.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
from dataclasses import asdict
from typing import Any, Protocol

from .hub import Outcome, Request
from .runner import HailError
from .store import Store, Task

MAX_ACTIVE = 8  # background tasks submitted or working at once


class Consults(Protocol):
    def consult(self, req: Request) -> Outcome: ...


def submit(store: Store, req: Request, thread: str, sender: str) -> tuple[str, str]:
    if store.count_active_tasks() >= MAX_ACTIVE:
        raise HailError(f"too many background tasks ({MAX_ACTIVE} running): wait for one to finish")
    task_id = "k-" + secrets.token_hex(4)
    store.create_task(task_id, {**asdict(req), "thread": thread, "sender": sender})
    spawn(task_id)
    return task_id, thread


def spawn(task_id: str) -> None:
    subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "hail", "_task", task_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **_detached(),
    )


def _detached() -> dict[str, Any]:
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def run(hub: Consults, store: Store, task_id: str) -> None:
    task = store.get_task(task_id)
    if task is None:
        raise HailError(f"unknown task '{task_id}'")
    store.update_task(task_id, "working")
    try:
        req = Request(**task.request)
    except TypeError as exc:
        store.update_task(task_id, "failed", error=f"invalid task request: {exc}")
        return
    try:
        outcome = hub.consult(req)
    except Exception as exc:  # whatever happens, the task must not stay 'working'
        store.update_task(task_id, "failed", error=str(exc) or type(exc).__name__)
        return
    if outcome.ok:
        store.update_task(task_id, "completed", result=outcome.text)
    else:
        store.update_task(task_id, "failed", result=outcome.text, error="agent exited with an error")


def describe(task: Task) -> str:
    req = task.request
    age = int(time.time() - task.created)
    lines = [
        f"task {task.id}: {task.status} (agent {req.get('agent') or req.get('skill')}, "
        f"thread {req.get('thread')}, {age}s ago)"
    ]
    if task.result:
        lines.append(task.result)
    if task.error:
        lines.append(f"error: {task.error}")
    return "\n".join(lines)
