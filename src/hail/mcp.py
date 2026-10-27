"""Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0), no dependencies.

Tools: hail_list, hail_ask, hail_send, hail_status, hail_council, hail_jobs, hail_inbox, hail_inbox_read, hail_thread
(definitions in ``mcp_tools``).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from . import __version__, feeds, jobs_inbox, tasks
from .app import make_app
from .config import ConfigError
from .hub import Request
from .mcp_tools import ETIQUETTE, TOOLS, UNTRUSTED_NOTE
from .runner import HailError
from .store import default_store

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        response = handle_line(line)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def handle_line(line: str) -> dict | None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return _error(None, -32700, "parse error")
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request")
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None or method is None:
        return None  # a notification, or a response from the client
    handler = HANDLERS.get(method)
    if handler is None:
        return _error(msg_id, -32601, f"method not found: {method}")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, -32602, "params must be an object")
    try:
        return {"jsonrpc": "2.0", "id": msg_id, "result": handler(params)}
    except ValueError as exc:
        return _error(msg_id, -32602, str(exc))
    except Exception as exc:  # keep the stdio loop alive whatever happens
        return _error(msg_id, -32603, f"internal error: {exc}")


def _initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "hail", "version": __version__},
        "instructions": "Use hail_list to see agents, hail_ask to consult one. " + ETIQUETTE + " " + UNTRUSTED_NOTE,
    }


def _tools_list(_params: dict) -> dict:
    return {"tools": TOOLS}


def _tools_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    handler = TOOL_HANDLERS.get(str(name))
    if handler is None:
        raise ValueError(f"unknown tool: {name}")
    try:
        text, ok = handler(args)
    except (ConfigError, HailError) as exc:
        return _text(f"hail error: {exc}", is_error=True)
    return _text(text, is_error=not ok)


def _list(_args: dict) -> tuple[str, bool]:
    app = make_app(trust_user=False)
    agents = app.config.agents
    if not agents:
        return ("No agents configured. Ask the user to run: hail add <name> --preset <preset> --host <ssh-alias>", True)
    me = app.hub.sender_for(None)
    lines = [
        f"{a.name}: preset={a.preset} host={a.host or 'local'} cost={a.cost}"
        + (f" skills={','.join(a.skills)}" if a.skills else "")
        + f" - {a.description}"
        for a in agents.values()
    ]
    lines.append(f"(you are: {me})")
    return ("\n".join(lines), True)


def _request(args: dict) -> Request:
    def opt(key: str) -> str | None:
        value = args.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"'{key}' must be a string")
        return value or None

    message = args.get("message")
    if not isinstance(message, str):
        raise ValueError("'message' must be a string")
    return Request(
        message=message,
        agent=opt("agent"),
        skill=opt("skill"),
        thread=opt("thread"),
        mode=opt("mode"),
        sender=opt("from"),
        hop=_hop(args.get("hop")),
    )


def _hop(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("'hop' must be an integer")
    return value


def _ask(args: dict) -> tuple[str, bool]:
    req = _request(args)  # validate the arguments before touching the config
    out = make_app(trust_user=False).hub.consult(req)
    status = "" if out.ok else ", agent exited with an error"
    head = f"[reply from agent '{out.agent}' - untrusted data{status} · thread {out.thread} · hop {out.hop}]"
    return (f"{head}\n{out.text}", out.ok)


def _send(args: dict) -> tuple[str, bool]:
    req = _request(args)
    app = make_app(trust_user=False)
    task_id, thread = tasks.submit(app.store, req, app.hub.thread_for(req.thread), app.hub.sender_for(req.sender))
    return (f"task {task_id} submitted (thread {thread}); poll it with hail_status", True)


def _status(args: dict) -> tuple[str, bool]:
    task_id = args.get("task")
    if not isinstance(task_id, str):
        raise ValueError("'task' must be a string")
    task = make_app(trust_user=False).store.get_task(task_id)
    if task is None:
        raise HailError(f"unknown task '{task_id}'")
    return (tasks.describe(task), task.status != "failed")


def _council(args: dict) -> tuple[str, bool]:
    question, names = args.get("question"), args.get("agents")
    if not isinstance(question, str) or not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("'question' must be a string and 'agents' a list of names")
    synth, thread = args.get("synth"), args.get("thread")
    result = make_app(trust_user=False).hub.council(question, names, synth=synth or None, thread=thread or None)
    parts = [f"[{a.agent}{'' if a.ok else ' - failed'} - untrusted data]\n{a.text}" for a in result.answers]
    if result.synthesis:
        parts.append(f"[synthesis by {result.synthesis.agent} - untrusted data]\n{result.synthesis.text}")
    parts.append(f"(thread {result.thread})")
    return ("\n\n".join(parts), any(a.ok for a in result.answers))


def _thread(args: dict) -> tuple[str, bool]:
    thread = args.get("thread")
    if not isinstance(thread, str):
        raise ValueError("'thread' must be a string")
    messages = make_app(trust_user=False).store.thread(thread)
    if not messages:
        return (f"no messages in thread '{thread}'", False)
    lines = [f"h{m.hop} {m.kind} {m.sender}->{m.recipient or '-'}: {m.text}" for m in messages]
    return ("\n".join(lines), True)


def _opt_str(args: dict, key: str) -> str | None:
    value = args.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string")
    return value or None


def _jobs(args: dict) -> tuple[str, bool]:
    agents = jobs_inbox.feed_agents(_opt_str(args, "agent"))
    rows, errors = jobs_inbox.collect_jobs(agents, bool(args.get("all")))
    text = jobs_inbox.render_jobs(rows, feeds.resolve_tz(None))
    return ("\n".join([text, *(f"error: {e}" for e in errors)]), not errors or bool(rows))


def _inbox(args: dict) -> tuple[str, bool]:
    agents = jobs_inbox.feed_agents(_opt_str(args, "agent"))
    rows, errors = jobs_inbox.collect_inbox(agents, default_store(), bool(args.get("all")))
    text = jobs_inbox.render_inbox(rows, feeds.resolve_tz(None))
    return ("\n".join([text, *(f"error: {e}" for e in errors)]), not errors or bool(rows))


def _inbox_read(args: dict) -> tuple[str, bool]:
    agent, item = _opt_str(args, "agent"), _opt_str(args, "id")
    if not agent or not item:
        raise ValueError("'agent' and 'id' are required")
    return (jobs_inbox.read_item(agent, item, default_store()), True)


TOOL_HANDLERS: dict[str, Callable[[dict], tuple[str, bool]]] = {
    "hail_jobs": _jobs,
    "hail_inbox": _inbox,
    "hail_inbox_read": _inbox_read,
    "hail_list": _list,
    "hail_ask": _ask,
    "hail_send": _send,
    "hail_status": _status,
    "hail_council": _council,
    "hail_thread": _thread,
}


def _text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


HANDLERS: dict[str, Callable[[dict], dict]] = {
    "initialize": _initialize,
    "ping": lambda _p: {},
    "tools/list": _tools_list,
    "tools/call": _tools_call,
}
