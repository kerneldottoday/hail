"""Background asks: hail_send returns a task id at once, a detached worker does the call.

The worker is ``python -m hail _task <id>``; it inherits the caller's
environment, so the hail identity (thread, hop, sender) is preserved.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .errors import AgentUnusable, NotFound, Temporary, UsageError
from .hub import Outcome, Request
from .store import Store, Task

MAX_ACTIVE = 8  # background tasks submitted or working at once
STALE_AFTER = 6 * 3600  # a task silent this long has lost its worker
REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


@dataclass(frozen=True)
class Submitted:
    id: str
    thread: str
    ref: str | None = None
    reused: bool = False  # the ref already named this task: nothing new was started


class Consults(Protocol):
    def consult(self, req: Request) -> Outcome: ...


def submit(store: Store, req: Request, thread: str, sender: str, ref: str | None = None) -> Submitted:
    """Start a background ask. With ``ref``, sending the same request again returns the same task."""
    if ref is not None and not REF_RE.fullmatch(ref):
        raise UsageError(f"invalid ref '{ref[:80]}' (1-64 letters, digits, '.', '_', ':' or '-')")
    store.reap_stale(STALE_AFTER)
    digest = _digest(req, sender)
    existing = store.task_for_ref(ref, digest) if ref else None
    if existing:
        return _again(store, existing, ref)
    if store.count_active_tasks() >= MAX_ACTIVE:
        raise Temporary(f"too many background tasks ({MAX_ACTIVE} running): wait for one to finish")
    new_id = "k-" + secrets.token_hex(4)
    task_id = store.create_task(new_id, {**asdict(req), "thread": thread, "sender": sender}, ref=ref, digest=digest)
    if task_id != new_id:  # another send with the same ref won the race
        return _again(store, task_id, ref)
    try:
        spawn(task_id)
    except OSError as exc:  # fail it, so the same ref can try again instead of waiting for a dead task
        store.update_task(task_id, "failed", error=f"could not start the worker: {exc}")
        raise AgentUnusable(f"could not start the background worker: {exc}") from None
    return Submitted(task_id, thread, ref)


def _again(store: Store, task_id: str, ref: str | None) -> Submitted:
    task = store.get_task(task_id)
    thread = str(task.request.get("thread")) if task else ""
    return Submitted(task_id, thread, ref, reused=True)


def _digest(req: Request, sender: str) -> str:
    """What makes two sends 'the same request' for a ref (the thread may be new each time)."""
    key = [req.agent, req.skill, req.message.strip(), req.mode, sender]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()


def status(store: Store, task_id: str) -> Task | None:
    """A task as ``hail status`` shows it, after failing the ones whose worker died."""
    store.reap_stale(STALE_AFTER)
    return store.get_task(task_id)


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
        raise NotFound(f"unknown task '{task_id}'")
    if not store.claim_task(task_id):
        return  # already started or finished: a task runs once, however often its worker is called
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
