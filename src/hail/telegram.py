"""Telegram channel: watch your agents talk, one forum topic per thread.

Outbound (``notify``): every ask, reply, error and refusal is posted to a
private group, secrets redacted. Inbound (``poll_once``, run by
``hail telegram watch``), only from ``allowed_users``:

- ``/stop`` or ``/resume`` inside a thread's topic: block or reopen the thread
- any other text inside a topic: a note the agents see on their next call
- ``/ask <agent> <message>``: start a new thread in the background
- ``/threads``: recent threads

Uses only the standard library (urllib) and plain text messages.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .envelope import USER, Event, new_thread_id
from .hub import Request
from .redact import redact
from .store import Store
from .tasks import submit as submit_task

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4096
KNOWN_KEYS = frozenset({"chat_id", "token_env", "token_file", "topics", "allowed_users"})
ICONS = {"ask": "❓", "reply": "💬", "error": "⚠️", "refused": "⛔", "council": "🏛️"}
HELP = "Commands: /ask <agent> <message>, /threads. Inside a thread topic: /stop, /resume, or write a note."

Api = Callable[[str, dict[str, Any]], Any]


class TelegramError(RuntimeError):
    """Telegram is misconfigured or the API call failed."""


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: int
    topics: bool = True
    allowed_users: frozenset[int] = field(default_factory=frozenset)


def parse_config(raw: Mapping[str, Any] | None, environ: Mapping[str, str]) -> TelegramConfig | None:
    if raw is None:
        return None
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        raise TelegramError(f"[telegram]: unknown key(s) {', '.join(sorted(unknown))}")
    chat_id = raw.get("chat_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise TelegramError("[telegram] chat_id must be a number (the group id, e.g. -100123...)")
    users = raw.get("allowed_users", [])
    if not isinstance(users, list) or not all(isinstance(u, int) and not isinstance(u, bool) for u in users):
        raise TelegramError("[telegram] allowed_users must be a list of Telegram user ids")
    return TelegramConfig(
        token=_token(raw, environ),
        chat_id=chat_id,
        topics=bool(raw.get("topics", True)),
        allowed_users=frozenset(users),
    )


def _token(raw: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    env_name = str(raw.get("token_env", "HAIL_TELEGRAM_TOKEN"))
    token = environ.get(env_name, "").strip()
    if not token and raw.get("token_file"):
        path = Path(str(raw["token_file"])).expanduser()
        token = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if not token:
        raise TelegramError(f"no bot token: set {env_name} or [telegram] token_file (never put it in agents.toml)")
    return token


def http_api(token: str, timeout: int = 15) -> Api:
    def call(method: str, payload: dict[str, Any]) -> Any:
        req = urllib.request.Request(
            API.format(token=token, method=method),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout + int(payload.get("timeout", 0))) as resp:
                body = json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TelegramError(f"{method}: {exc}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', 'failed')}")
        return body.get("result")

    return call


def format_event(event: Event, tagged: bool) -> str:
    icon = ICONS.get(event.kind, "•")
    if event.kind == "council":
        head = f"{icon} council: {event.sender} asks {event.recipient}"
    else:
        head = f"{icon} {event.sender} → {event.recipient}"
        if event.kind == "ask":
            head += f" · hop {event.hop}/{event.max_hops} · {event.mode}"
    if tagged:
        head = f"[{event.thread}] {head}"
    body = redact(event.text)
    room = LIMIT - len(head) - 4
    if len(body) > room:
        body = body[: room - 1] + "…"
    return f"{head}\n{body}"


class Telegram:
    def __init__(
        self, cfg: TelegramConfig, store: Store, api: Api | None = None, agents: frozenset[str] = frozenset()
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.api = api or http_api(cfg.token)
        self.agents = agents

    # outbound -----------------------------------------------------------------

    def notify(self, event: Event) -> None:
        topic = self._topic(event) if self.cfg.topics else None
        self.send(format_event(event, tagged=topic is None), topic)

    def send(self, text: str, topic: int | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": self.cfg.chat_id, "text": text, "disable_web_page_preview": True}
        if topic is not None:
            payload["message_thread_id"] = topic
        self.api("sendMessage", payload)

    def _topic(self, event: Event) -> int | None:
        known = self.store.topic_of(event.thread)
        if known is not None:
            return known
        name = f"{event.thread} · {event.sender} → {event.recipient}"[:128]
        try:
            result = self.api("createForumTopic", {"chat_id": self.cfg.chat_id, "name": name})
        except TelegramError:
            return None  # group without topics, or bot not admin: post in the main chat
        topic = int(result["message_thread_id"])
        self.store.set_topic(event.thread, topic)
        return topic

    # inbound ------------------------------------------------------------------

    def poll_once(self, timeout: int = 25) -> int:
        if not self.cfg.allowed_users:
            raise TelegramError("set [telegram] allowed_users (your Telegram user id) before accepting commands")
        offset = int(self.store.get_meta("telegram_offset") or 0)
        updates = self.api("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]})
        handled = 0
        for update in updates or []:
            self.store.set_meta("telegram_offset", str(int(update["update_id"]) + 1))
            msg = update.get("message") or {}
            if not self._trusted(msg):
                continue
            handled += 1
            try:
                self._handle(str(msg["text"]).strip(), msg.get("message_thread_id"))
            except Exception as exc:  # one bad update must not stop the others (offset is already saved)
                print(f"hail: telegram update {update['update_id']} failed: {exc}", file=sys.stderr)
        return handled

    def _trusted(self, msg: Mapping[str, Any]) -> bool:
        chat = (msg.get("chat") or {}).get("id")
        user = (msg.get("from") or {}).get("id")
        return chat == self.cfg.chat_id and user in self.cfg.allowed_users and isinstance(msg.get("text"), str)

    def _handle(self, text: str, topic: int | None) -> None:
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
        thread = self.store.thread_of_topic(topic) if topic is not None else None
        if command == "/threads":
            self.send(self._threads_text(), topic)
        elif command == "/ask":
            self.send(self._ask(text), topic)
        elif thread and command in ("/stop", "/resume"):
            self.store.set_stopped(thread, command == "/stop")
            self.send(f"⏹ thread {thread} stopped" if command == "/stop" else f"▶️ thread {thread} resumed", topic)
        elif thread and not text.startswith("/"):
            self.store.add(thread, 0, "human", "", "human", "", text)
            self.send("📝 note saved: agents will see it on their next call in this thread", topic)
        else:
            self.send(HELP, topic)

    def _ask(self, text: str) -> str:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            return "usage: /ask <agent> <message>"
        agent, message = parts[1], parts[2]
        if self.agents and agent not in self.agents:
            return f"unknown agent '{agent}' (configured: {', '.join(sorted(self.agents))})"
        task_id, thread = submit_task(
            self.store, Request(message=message, agent=agent), thread=new_thread_id(), sender=USER
        )
        return f"📨 task {task_id} sent to {agent} (thread {thread})"

    def _threads_text(self) -> str:
        rows = self.store.threads(10)
        if not rows:
            return "no threads yet"
        return "\n".join(f"{t.thread} · {t.count} messages{' · stopped' if t.stopped else ''}" for t in rows)
