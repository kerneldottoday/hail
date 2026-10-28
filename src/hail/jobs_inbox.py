"""``hail jobs`` and ``hail inbox``: what agents have scheduled and what they delivered (read-only)."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from . import feeds
from .config import Agent, load_config
from .runner import HailError
from .store import Store, default_store

UNTRUSTED = "[untrusted: written by an agent, verify before relying on it]"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
FAR = datetime(9999, 1, 1, tzinfo=timezone.utc)
MARK_KEY = "inbox_mark:"
CLOCK_SKEW = timedelta(minutes=10)
ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")  # CSI, OSC, others
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \t and \n


def clean(value: object) -> str:
    """Feed text as printable: no terminal escape sequences from what an agent wrote."""
    return CONTROL.sub("", ESCAPES.sub("", str(value)))


@dataclass(frozen=True)
class Options:
    words: tuple[str, ...]
    show_all: bool
    as_json: bool
    tz: str | None


def parse_options(argv: list[str]) -> Options:
    words: list[str] = []
    flags = {"--all": False, "--json": False}
    tz = None
    rest = list(argv)
    while rest:
        word = rest.pop(0)
        if word in flags:
            flags[word] = True
        elif word == "--tz":
            if not rest:
                raise HailError("--tz needs a value (e.g. Europe/Paris)")
            tz = rest.pop(0)
        elif word.startswith("--"):
            raise HailError(f"unknown option {word}")
        else:
            words.append(word)
    return Options(tuple(words), flags["--all"], flags["--json"], tz)


def feed_agents(name: str | None = None) -> list[Agent]:
    agents = load_config().agents
    if name is None:
        return [a for a in agents.values() if feeds.has_feed(a)]
    agent = agents.get(name)
    if agent is None or not feeds.has_feed(agent):
        raise HailError(f"'{name}' has no feed (built in for hermes; others: set feed_command)")
    return [agent]


def _gather(agents: Iterable[Agent], args: list[str]) -> tuple[list[tuple[Agent, Any]], list[str]]:
    results, errors = [], []
    for agent in agents:
        try:
            data = feeds.run(agent, args)
        except HailError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(data, list):
            errors.append(f"'{agent.name}' feed: expected a list")
            continue
        results.append((agent, data))
    return results, errors


# --- jobs ---------------------------------------------------------------------


def collect_jobs(agents: Iterable[Agent], show_all: bool) -> tuple[list[dict[str, Any]], list[str]]:
    results, errors = _gather(agents, ["jobs"])
    rows = [
        {
            **{
                k: job.get(k)
                for k in ("id", "name", "state", "enabled", "schedule", "next_run", "last_run", "last_status")
            },
            "agent": agent.name,
        }
        for agent, data in results
        for job in data
        if isinstance(job, dict)
    ]
    if not show_all:
        rows = [r for r in rows if _active(r)]
    rows.sort(key=lambda r: (not _active(r), feeds.parse_time(r.get("next_run")) or FAR))
    return rows, errors


def _active(row: dict[str, Any]) -> bool:
    return bool(row.get("enabled", True)) and row.get("state") not in ("paused", "completed")


def render_jobs(rows: list[dict[str, Any]], tz: tzinfo | None) -> str:
    if not rows:
        return "no scheduled jobs"
    lines = [UNTRUSTED]
    for r in rows:
        last = clean(r.get("last_status") or "never run")
        lines.append(
            f"{r['agent']}/{clean(r.get('id', ''))}  {clean(r.get('name', ''))}  [{clean(r.get('state') or '-')}]\n"
            f"    next: {feeds.format_when(feeds.parse_time(r.get('next_run')), tz)}"
            f"  · schedule: {clean(r.get('schedule') or '-')}  · last: {last}"
        )
    return "\n".join(lines)


def cmd_jobs(argv: list[str]) -> int:
    opts = parse_options(argv)
    tz = feeds.resolve_tz(opts.tz)
    rows, errors = collect_jobs(feed_agents(opts.words[0] if opts.words else None), opts.show_all)
    if opts.as_json:
        print(json.dumps({"jobs": rows, "errors": errors}, ensure_ascii=False))
    else:
        print(render_jobs(rows, tz))
    for err in errors:
        print(f"hail: {err}", file=sys.stderr)
    return 1 if errors else 0


# --- inbox --------------------------------------------------------------------


def collect_inbox(agents: Iterable[Agent], store: Store, show_all: bool) -> tuple[list[dict[str, Any]], list[str]]:
    results, errors = _gather(agents, ["inbox"])
    rows = []
    for agent, data in results:
        seen, mark = store.seen_items(agent.name), read_mark(agent.name, store)
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                created = feeds.parse_time(item.get("created"))
                old = mark is not None and created is not None and created <= mark
                rows.append({**item, "agent": agent.name, "seen": old or item["id"] in seen})
    if not show_all:
        rows = [r for r in rows if not r["seen"]]
    rows.sort(key=lambda r: feeds.parse_time(r.get("created")) or EPOCH, reverse=True)
    return rows, errors


def render_inbox(rows: list[dict[str, Any]], tz: tzinfo | None) -> str:
    new = sum(1 for r in rows if not r["seen"])
    if not rows:
        return "inbox: nothing new"
    lines = [f"inbox: {new} new" + (f", {len(rows) - new} already read" if len(rows) > new else ""), UNTRUSTED]
    for r in rows:
        mark = " " if r["seen"] else "*"
        job = clean(r.get("job_name") or r.get("job") or "")
        when = feeds.format_when(feeds.parse_time(r.get("created")), tz)
        lines.append(
            f"{mark} {r['agent']} {clean(r['id'])}  ({job}, {when})\n    {clean(r.get('preview') or '')[:160]}"
        )
    lines.append("read one: hail inbox read <agent> <id>  · mark all read: hail inbox clear")
    return "\n".join(lines)


def read_item(agent_name: str, item_id: str, store: Store) -> str:
    feeds.check_item_id(item_id)
    agent = feed_agents(agent_name)[0]
    data = feeds.run(agent, ["read", item_id])
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise HailError(f"'{agent_name}' feed: unexpected reply for {item_id}")
    store.mark_seen(agent.name, [item_id])
    return f"{UNTRUSTED}\n--- {agent.name} {item_id} ---\n{clean(data['text'][: feeds.MAX_READ_CHARS])}"


def read_mark(agent_name: str, store: Store) -> datetime | None:
    """Everything an agent delivered up to this time was cleared, even if its feed no longer lists it."""
    return feeds.parse_time(store.get_meta(MARK_KEY + agent_name))


def clear(agent_name: str | None, store: Store) -> tuple[int, list[str]]:
    rows, errors = collect_inbox(feed_agents(agent_name), store, show_all=True)
    new = [r for r in rows if not r["seen"]]
    horizon = datetime.now(timezone.utc) + CLOCK_SKEW  # a future-dated report must not hide what comes next
    for agent in {r["agent"] for r in rows}:
        store.mark_seen(agent, [r["id"] for r in new if r["agent"] == agent])
        times = [t for r in rows if r["agent"] == agent and (t := feeds.parse_time(r.get("created"))) and t <= horizon]
        old = read_mark(agent, store)
        latest = max([*times, *([old] if old else [])], default=None)
        if latest is not None:
            store.set_meta(MARK_KEY + agent, latest.isoformat())
    return len(new), errors


def cmd_inbox(argv: list[str]) -> int:
    opts = parse_options(argv)
    store = default_store()
    words = opts.words
    if words and words[0] == "read":
        if len(words) != 3:
            raise HailError("usage: hail inbox read <agent> <id>")
        print(read_item(words[1], words[2], store))
        return 0
    if words and words[0] == "clear":
        count, errors = clear(words[1] if len(words) > 1 else None, store)
        print(f"marked {count} item(s) as read")
        return _report(errors)
    tz = feeds.resolve_tz(opts.tz)
    rows, errors = collect_inbox(feed_agents(words[0] if words else None), store, opts.show_all)
    if opts.as_json:
        print(json.dumps({"items": rows, "errors": errors}, ensure_ascii=False))
    else:
        print(render_inbox(rows, tz))
    return _report(errors)


def _report(errors: list[str]) -> int:
    for err in errors:
        print(f"hail: {err}", file=sys.stderr)
    return 1 if errors else 0
