"""Read what an agent has scheduled (jobs) and delivered (inbox), without touching the agent.

A feed is a small read-only program run on the agent's machine (locally or over
SSH) that prints JSON. Hermes has a built-in feed: a stdlib Python script sent on
stdin that reads ``~/.hermes/cron/jobs.json`` and ``~/.hermes/cron/output/``.
Any other agent can expose one with ``feed_command`` in its config: hail runs it
with ``HAIL_FEED_ARGS`` set to a JSON list (``["jobs"]``, ``["inbox"]`` or
``["read", "<item id>"]``) and expects the same JSON shapes back.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from .config import Agent
from .runner import SSH_OPTIONS, HailError, Invocation, remote_quote

FEED_TIMEOUT = 60
MAX_READ_CHARS = 200_000
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,120})?$")

HERMES_FEED = r"""
import json, os, sys
from datetime import datetime, timezone

MAX_ITEMS, PREVIEW, MAX_READ = 50, 240, 200000


def out(value, code=0):
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    sys.stdout.flush()
    sys.exit(code)


home = os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")
cron = os.path.join(home, "cron")
outputs = os.path.realpath(os.path.join(cron, "output"))
try:
    args = json.loads(os.environ.get("HAIL_FEED_ARGS") or "[]")
except ValueError:
    out({"error": "bad HAIL_FEED_ARGS"}, 2)


def load_jobs():
    try:
        with open(os.path.join(cron, "jobs.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)]


def schedule_text(job):
    sched = job.get("schedule")
    if isinstance(sched, dict):
        return str(sched.get("display") or sched.get("expr") or sched.get("kind") or "")
    return str(job.get("schedule_display") or sched or "")


def iso_mtime(path):
    return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat()


def item_job(rel):
    head = rel.split("/")[0]
    return head if "/" in rel else head.split("_")[0]


def list_items(names):
    found = []
    for root, _dirs, files in os.walk(outputs):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, outputs).replace(os.sep, "/")
            if rel.count("/") > 1 or name.startswith(".") or not os.path.isfile(path):
                continue  # a FIFO or socket would block open()
            try:
                found.append((os.path.getmtime(path), rel, path))
            except OSError:
                continue  # removed by a cron cleanup meanwhile
    found.sort(reverse=True)
    items = []
    for _mtime, rel, path in found[:MAX_ITEMS]:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                preview = " ".join(fh.read(PREVIEW * 2).split())[:PREVIEW]
            created, size = iso_mtime(path), os.path.getsize(path)
        except OSError:
            continue
        job = item_job(rel)
        items.append({"id": rel, "job": job, "job_name": names.get(job, ""), "created": created,
                      "size": size, "preview": preview})
    return items


def read_item(rel):
    path = os.path.realpath(os.path.join(outputs, rel))
    if not path.startswith(outputs + os.sep) or not os.path.isfile(path):
        out({"error": "unknown item: " + rel}, 2)
    with open(path, encoding="utf-8", errors="replace") as fh:
        out({"id": rel, "text": fh.read(MAX_READ)})


command = args[0] if args else ""
if command == "jobs":
    out([{"id": str(j.get("id", "")), "name": str(j.get("name", "")), "state": str(j.get("state", "")),
          "enabled": bool(j.get("enabled", True)), "schedule": schedule_text(j), "next_run": j.get("next_run_at"),
          "last_run": j.get("last_run_at"), "last_status": j.get("last_status")} for j in load_jobs()])
elif command == "inbox":
    names = {str(j.get("id", "")): str(j.get("name", "")) for j in load_jobs()}
    out(list_items(names) if os.path.isdir(outputs) else [])
elif command == "read" and len(args) == 2:
    read_item(str(args[1]))
else:
    out({"error": "usage: jobs | inbox | read <id>"}, 2)
"""

BUILTIN_FEEDS = {"hermes": HERMES_FEED}


def has_feed(agent: Agent) -> bool:
    return agent.preset in BUILTIN_FEEDS or bool(agent.variables.get("feed_command"))


def check_item_id(item: str) -> str:
    if not ITEM_ID_RE.match(item) or ".." in item.split("/"):
        raise HailError(f"invalid item id '{item[:40]}'")
    return item


def invocation(agent: Agent, args: list[str]) -> Invocation:
    env = {"HAIL_FEED_ARGS": json.dumps(args)}
    home = agent.variables.get("hermes_home")
    if home and agent.preset in BUILTIN_FEEDS:
        env["HERMES_HOME"] = home
    command = agent.variables.get("feed_command")
    script = None if command else BUILTIN_FEEDS.get(agent.preset)
    if agent.host is not None:
        exports = "export " + " ".join(
            f"{k}={remote_quote(v) if k == 'HERMES_HOME' else shlex.quote(v)}" for k, v in env.items()
        )
        if agent.path:
            exports += "; export PATH=" + ":".join(remote_quote(p) for p in agent.path) + ':"$PATH"'
        body = f"exec {command}" if command else "exec python3 -"
        remote = f"{exports}; {body}"
        argv = ("ssh", *SSH_OPTIONS, "--", agent.host, "sh -c " + shlex.quote(remote))
        return Invocation(argv=argv, stdin=script)
    local = {**os.environ, **{k: os.path.expanduser(v) if k == "HERMES_HOME" else v for k, v in env.items()}}
    if agent.path:
        local["PATH"] = os.pathsep.join([*(os.path.expanduser(p) for p in agent.path), local.get("PATH", "")])
    if command:
        return Invocation(argv=command, stdin=None, shell=True, env=local)
    return Invocation(argv=(sys.executable, "-"), stdin=script, env=local)


def run(agent: Agent, args: list[str]) -> Any:
    if not has_feed(agent):
        raise HailError(f"agent '{agent.name}' has no feed (built in for hermes; others: set feed_command)")
    inv = invocation(agent, args)
    try:
        proc = subprocess.run(
            inv.argv,
            input=inv.stdin.encode("utf-8") if inv.stdin is not None else None,
            capture_output=True,
            timeout=min(agent.timeout, FEED_TIMEOUT),
            shell=inv.shell,
            env=inv.env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HailError(f"'{agent.name}' feed did not answer within {min(agent.timeout, FEED_TIMEOUT)}s") from None
    except OSError as exc:
        raise HailError(f"'{agent.name}' feed could not start ({exc})") from None
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(stdout) if stdout else None
    except ValueError:
        data = None
    if isinstance(data, dict) and "error" in data:
        raise HailError(f"'{agent.name}' feed: {data['error']}")
    if proc.returncode != 0 or data is None:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[-300:] or stdout[:300]
        raise HailError(f"'{agent.name}' feed failed (exit {proc.returncode}): {err}")
    return data


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_tz(name: str | None) -> tzinfo | None:
    """None means the computer's local time; a name needs the IANA database (pip install tzdata on Windows)."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - Python < 3.9
        raise HailError("--tz needs Python 3.9+") from None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HailError(f"unknown time zone '{name}' (on Windows: pip install tzdata)") from None


def format_when(dt: datetime | None, tz: tzinfo | None) -> str:
    """'2026-10-27 12:00 UTC (13:00 CET)': UTC first, so nobody mixes up summer and winter time."""
    if dt is None:
        return "-"
    utc = dt.astimezone(timezone.utc)
    local = dt.astimezone(tz) if tz is not None else dt.astimezone()
    same_day = local.date() == utc.date()
    shown = local.strftime("%H:%M") if same_day else local.strftime("%Y-%m-%d %H:%M")
    return f"{utc.strftime('%Y-%m-%d %H:%M')} UTC ({shown} {_zone_label(local)})"


def _zone_label(local: datetime) -> str:
    """Short names (CET, CEST) as is; long Windows names ('Paris, Madrid (heure d'été)') become UTC+2."""
    name = local.tzname() or ""
    if name and len(name) <= 6 and " " not in name:
        return name
    minutes = int((local.utcoffset() or timedelta()).total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")
