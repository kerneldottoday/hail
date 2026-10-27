"""CLI commands for agent-to-agent work: ask, send, status, council, log, threads, stop, telegram."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass

from . import tasks
from .app import make_app
from .hub import Request
from .runner import HailError
from .store import Message, default_store
from .telegram import Telegram, TelegramError

VALUE_FLAGS = ("--thread", "--mode", "--skill", "--from", "--hop", "--agents", "--synth", "-n", "--grep")
BOOL_FLAGS = ("--json",)


@dataclass(frozen=True)
class Parsed:
    flags: dict[str, str]
    words: tuple[str, ...]


def parse(argv: list[str]) -> Parsed:
    """Leading --flags, then the message words (``--json`` is accepted anywhere)."""
    flags: dict[str, str] = {}
    rest = list(argv)
    while rest and rest[0] in (*VALUE_FLAGS, *BOOL_FLAGS):
        flag = rest.pop(0)
        if flag in BOOL_FLAGS:
            flags[flag] = "1"
            continue
        if not rest:
            raise HailError(f"{flag} needs a value")
        flags[flag] = rest.pop(0)
    words = [w for w in rest if w not in BOOL_FLAGS]
    if len(words) != len(rest):
        flags["--json"] = "1"
    return Parsed(flags, tuple(words))


def _request(agent: str | None, parsed: Parsed) -> Request:
    message = " ".join(parsed.words) if parsed.words else _read_stdin()
    return Request(
        message=message,
        agent=agent,
        skill=parsed.flags.get("--skill"),
        thread=parsed.flags.get("--thread"),
        mode=parsed.flags.get("--mode"),
        sender=parsed.flags.get("--from"),
        hop=_int(parsed.flags["--hop"]) if "--hop" in parsed.flags else None,
    )


def cmd_ask(agent: str | None, argv: list[str]) -> int:
    parsed = parse(argv)
    out = make_app().hub.consult(_request(agent, parsed))
    if "--json" in parsed.flags:
        payload = {"agent": out.agent, "reply": out.text, "ok": out.ok, "thread": out.thread, "hop": out.hop}
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(out.text)
        print(f"hail: thread {out.thread} · hop {out.hop}", file=sys.stderr)
    if not out.ok:
        print(f"hail: '{out.agent}' exited with an error", file=sys.stderr)
    return 0 if out.ok else 1


def cmd_ask_skill(argv: list[str]) -> int:
    """``hail ask --skill X msg`` or ``hail ask <agent> msg``."""
    if argv and not argv[0].startswith("-"):
        return cmd_ask(argv[0], argv[1:])
    return cmd_ask(None, argv)


def cmd_send(argv: list[str]) -> int:
    agent = argv[0] if argv and not argv[0].startswith("-") else None
    parsed = parse(argv[1:] if agent else argv)
    app = make_app()
    req = _request(agent, parsed)
    task_id, thread = tasks.submit(app.store, req, app.hub.thread_for(req.thread), app.hub.sender_for(req.sender))
    print(f"{task_id} (thread {thread}) - check with: hail status {task_id}")
    return 0


def cmd_status(argv: list[str]) -> int:
    if len(argv) != 1:
        raise HailError("usage: hail status <task-id>")
    task = tasks.status(default_store(), argv[0])
    if task is None:
        raise HailError(f"unknown task '{argv[0]}'")
    print(tasks.describe(task))
    return 0 if task.status != "failed" else 1


def cmd_task(argv: list[str]) -> int:
    if len(argv) != 1:
        raise HailError("usage: hail _task <task-id>")
    app = make_app()
    tasks.run(app.hub, app.store, argv[0])
    return 0


def cmd_council(argv: list[str]) -> int:
    parsed = parse(argv)
    names = [n.strip() for n in parsed.flags.get("--agents", "").split(",") if n.strip()]
    question = " ".join(parsed.words) if parsed.words else _read_stdin()
    result = make_app().hub.council(
        question, names, synth=parsed.flags.get("--synth"), thread=parsed.flags.get("--thread")
    )
    for answer in result.answers:
        print(f"== {answer.agent}{'' if answer.ok else ' (failed)'}\n{answer.text}\n")
    if result.synthesis:
        print(f"== synthesis by {result.synthesis.agent}\n{result.synthesis.text}")
    print(f"hail: thread {result.thread}", file=sys.stderr)
    return 0 if any(a.ok for a in result.answers) else 1


def cmd_log(argv: list[str]) -> int:
    parsed = parse(argv)
    limit = _int(parsed.flags.get("-n", "20"))
    for msg in default_store().recent(limit, grep=parsed.flags.get("--grep")):
        print(_line(msg))
    return 0


def cmd_threads(argv: list[str]) -> int:
    parsed = parse(argv)
    for t in default_store().threads(_int(parsed.flags.get("-n", "10"))):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(t.last))
        print(f"{t.thread:<14} {when}  {t.count:>3} messages{'  STOPPED' if t.stopped else ''}")
    return 0


def cmd_thread(argv: list[str]) -> int:
    if len(argv) != 1:
        raise HailError("usage: hail thread <thread-id>")
    messages = default_store().thread(argv[0])
    if not messages:
        raise HailError(f"no messages in thread '{argv[0]}'")
    for msg in messages:
        print(_line(msg, full=True))
    return 0


def cmd_stop(argv: list[str], stopped: bool = True) -> int:
    if len(argv) != 1:
        raise HailError(f"usage: hail {'stop' if stopped else 'resume'} <thread-id>")
    default_store().set_stopped(argv[0], stopped)
    print(f"thread {argv[0]} {'stopped' if stopped else 'resumed'}")
    return 0


def cmd_telegram(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    app = make_app()
    if app.telegram is None:
        raise HailError("telegram is not configured: add [telegram] chat_id and a token_file (see README)")
    try:
        if action == "test":
            app.telegram.send("✅ hail is connected to this chat.")
            print("test message sent")
            return 0
        if action == "watch":
            return _watch(app.telegram)
    except TelegramError as exc:
        raise HailError(f"telegram: {exc}") from None
    raise HailError("usage: hail telegram test|watch")


def _watch(tg: Telegram) -> int:
    print("watching Telegram (Ctrl+C to stop)", file=sys.stderr)
    delay = 1
    while True:
        try:
            tg.poll_once()
            delay = 1
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # keep the watcher alive (network, database, ...)
            print(f"hail: telegram: {exc} (retry in {delay}s)", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def _line(msg: Message, full: bool = False) -> str:
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(msg.ts))
    arrow = f"{msg.sender} → {msg.recipient}" if msg.recipient else msg.sender
    text = msg.text if full else msg.text.replace("\n", " ")[:140]
    return f"{when} {msg.thread} h{msg.hop} {msg.kind:<7} {arrow}: {text}"


def _int(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise HailError(f"expected a positive number, got '{value}'")
    return int(value)


def _read_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        raise HailError("no message (pass it as an argument or on stdin)")
    return sys.stdin.read()
