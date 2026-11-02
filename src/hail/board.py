"""What `hail tui` shows, read from the journal and the agents file. Plain data: no Textual here."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config
from .envelope import USER
from .jobs_inbox import clean
from .runner import advice_enforced
from .store import DAY, Message, Store

AGENT_KINDS = frozenset({"reply", "error"})  # written by an agent: shown as untrusted


@dataclass(frozen=True)
class ThreadRow:
    thread: str
    last: str
    messages: int
    stopped: bool
    agents: str  # who took part, besides you


@dataclass(frozen=True)
class Line:
    id: int
    when: str
    hop: int
    kind: str
    who: str
    mode: str
    text: str
    untrusted: bool


@dataclass(frozen=True)
class AgentRow:
    name: str
    preset: str
    where: str
    skills: str
    advice_enforced: bool
    calls_today: int
    daily_cap: int | None


def thread_rows(store: Store, limit: int, keep: str | None = None) -> tuple[ThreadRow, ...]:
    """The latest threads; ``keep`` (the open one) is added at the end if it is older than those."""
    threads = store.threads(limit)
    if keep and keep not in {t.thread for t in threads}:
        threads += store.threads(1, only=keep)
    people = store.participants(tuple(t.thread for t in threads))
    return tuple(
        ThreadRow(
            clean(t.thread),
            time.strftime("%m-%d %H:%M", time.localtime(t.last)),
            t.count,
            t.stopped,
            _people(people.get(t.thread, set())),
        )
        for t in threads
    )


def _people(names: set[str]) -> str:
    # a council's header names several agents at once ("a, b"): the agents also appear on their own
    return clean(", ".join(sorted(n for n in names if n != USER and "," not in n)))


def message_lines(store: Store, thread: str, after: int = 0) -> tuple[Line, ...]:
    """The thread's messages, or only those newer than message id ``after``."""
    return tuple(_line(m) for m in store.thread(thread, after))


def _line(m: Message) -> Line:
    who = f"{m.sender} → {m.recipient}" if m.recipient else m.sender
    untrusted = m.kind in AGENT_KINDS and m.sender != USER
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(m.ts))
    return Line(m.id, when, m.hop, m.kind, clean(who), m.mode, clean(m.text), untrusted)


def reply_target(messages: Sequence[Message]) -> str | None:
    """The agent asked last in a thread: who a reply from the TUI goes to."""
    for m in reversed(messages):
        if m.kind == "ask" and m.recipient and m.recipient != USER:
            return m.recipient
    return None


def agent_rows(config: Config, store: Store) -> tuple[AgentRow, ...]:
    since = time.time() - DAY
    return tuple(
        AgentRow(
            name=a.name,
            preset=a.preset,
            where=a.host or "local",
            skills=", ".join(a.skills),
            advice_enforced=advice_enforced(a),
            calls_today=store.asks_to(a.name, since),
            daily_cap=a.max_calls_per_day,
        )
        for a in config.agents.values()
    )
