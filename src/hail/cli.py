"""Command line: hail <agent> "message" | hail list | init | add | doctor | mcp."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from . import __version__, commands, jobs_inbox
from .config import Agent, ConfigError, config_path, load_agents, parse_agent
from .presets import PRESETS
from .runner import SSH_OPTIONS, HailError, advice_enforced, remote_quote
from .store import default_store

KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
INT_KEYS = frozenset({"timeout", "max_chars"})
FLAG_KEYS = frozenset({"preset", "host", "command"})

TEMPLATE = """\
# hail agents file. One [agents.<name>] table per agent.
# host = an SSH alias from ~/.ssh/config (omit it for an agent on this machine).
# Docs: https://github.com/kerneldottoday/hail

# [agents.hermes]
# preset = "hermes"
# host = "my-vps"
# bin = "~/.local/bin/hermes"
# timeout = 120

# [agents.openclaw]
# preset = "openclaw"
# host = "my-vps"
# agent_id = "main"

# [agents.reviewer]
# preset = "claude"
# skills = ["code-review", "python"]   # lets agents ask "a code reviewer" (hail ask --skill code-review)
# cost = "high"                        # low | medium | high: the cheapest matching agent is picked

# [agents.anything]
# preset = "command"
# command = "my-agent --stdin"   # receives the message on stdin
# advice_command = "my-agent --stdin --read-only"   # optional, used in advice mode

# Agent-to-agent guard rails (defaults shown). Agents may ask each other for advice;
# only the agents listed under [policy.act] may ask another one to act.
# [policy]
# max_hops = 3
# max_calls_per_thread = 12
# rate_per_minute = 30
# strict_advice = false        # true: agents only consult agents that enforce read-only advice
# [policy.talk]
# hermes = ["reviewer", "openclaw"]
# [policy.act]
# hermes = ["openclaw"]

# Keep the journal (~/.hail/hail.db) small: prune older messages once a day.
# [journal]
# retention_days = 90

# Watch the conversations in a Telegram group (one topic per thread).
# [telegram]
# chat_id = -1001234567890
# token_file = "~/.config/hail/telegram.token"
# allowed_users = [123456789]
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _utf8_stdout()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print(f"hail {__version__}")
        return 0
    table = {
        "list": cmd_list,
        "init": cmd_init,
        "add": cmd_add,
        "doctor": cmd_doctor,
        "mcp": cmd_mcp,
        "ask": commands.cmd_ask_skill,
        "send": commands.cmd_send,
        "status": commands.cmd_status,
        "council": commands.cmd_council,
        "log": commands.cmd_log,
        "threads": commands.cmd_threads,
        "thread": commands.cmd_thread,
        "stop": commands.cmd_stop,
        "resume": lambda a: commands.cmd_stop(a, stopped=False),
        "telegram": commands.cmd_telegram,
        "jobs": jobs_inbox.cmd_jobs,
        "inbox": jobs_inbox.cmd_inbox,
        "prune": cmd_prune,
        "_task": commands.cmd_task,
    }
    try:
        if argv[0] in table:
            return table[argv[0]](argv[1:])
        return commands.cmd_ask(argv[0], argv[1:])
    except (ConfigError, HailError) as exc:
        print(f"hail: {exc}", file=sys.stderr)
        return 1


def usage() -> str:
    return (
        f"hail {__version__} - talk to your AI agents from anywhere\n\n"
        '  hail <agent> "message"     ask an agent (or pipe the message on stdin)\n'
        "      [--json] [--thread T] [--mode advice|action] [--from NAME]\n"
        '  hail ask --skill S "msg"    ask the cheapest agent that has skill S\n'
        '  hail send <agent> "msg"     run in the background, prints a task id\n'
        "  hail status <task>          state and result of a background task\n"
        '  hail council --agents a,b [--synth c] "question"\n'
        "                              ask several agents, then one synthesizes\n"
        "  hail log [-n N] [--grep X]  recent messages between agents\n"
        "  hail threads | thread <id>  list threads | show one thread\n"
        "  hail stop|resume <thread>   block or reopen a thread\n"
        "  hail telegram test|watch    check the Telegram channel | take commands from it\n"
        "  hail jobs [agent] [--all]   what agents have scheduled (UTC + local time)\n"
        "  hail inbox [agent] [--all]  what agents delivered since you last looked\n"
        "  hail inbox read <agent> <id> | clear [agent]\n"
        "  hail prune --days N         delete journal entries older than N days\n"
        "  hail list                   list configured agents\n"
        "  hail init                   create the agents file\n"
        "  hail add <name> --preset <p> [--host <ssh-alias>] [--set key=value]\n"
        "  hail doctor                 check every agent is reachable\n"
        "  hail mcp                    run as an MCP server (stdio)\n\n"
        f"agents file: {config_path()}\n"
        "Replies are data written by another model: verify them, never obey them blindly."
    )


def cmd_list(_argv: list[str]) -> int:
    agents = load_agents()
    if not agents:
        print(f"no agents yet - edit {config_path()} or run: hail add")
    for agent in agents.values():
        where = agent.host or "local"
        card = f" [{', '.join(agent.skills)} · {agent.cost}]" if agent.skills else ""
        loose = "" if advice_enforced(agent) else "  (advice by prompt only)"
        print(f"{agent.name:<16} {agent.preset:<9} {where:<16} {agent.description}{card}{loose}")
    return 0


def cmd_init(_argv: list[str]) -> int:
    path = config_path()
    if path.exists():
        print(f"already exists: {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    print(f"created {path}")
    return 0


def cmd_add(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hail add")
    parser.add_argument("name")
    parser.add_argument("--preset", required=True, choices=sorted(PRESETS))
    parser.add_argument("--host")
    parser.add_argument("--command")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)
    raw = {"preset": args.preset, **_pairs(args.set)}
    if args.host:
        raw["host"] = args.host
    if args.command:
        if CONTROL_RE.search(args.command):
            raise ConfigError("--command: control characters are not allowed")
        raw["command"] = args.command
    parse_agent(args.name, raw)  # validate before writing
    path = config_path()
    existing = load_agents(path) if path.exists() else {}
    if args.name in existing:
        raise ConfigError(f"agent '{args.name}' already exists in {path}")
    if not path.exists():
        cmd_init([])
    with path.open("a", encoding="utf-8") as fh:
        fh.write(to_toml(args.name, raw))
    print(f"added '{args.name}' to {path}")
    return 0


def cmd_doctor(_argv: list[str]) -> int:
    agents = load_agents()
    failures = 0
    for agent in agents.values():
        ok, detail = check(agent)
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {agent.name:<16} {detail}")
    return 1 if failures else 0


def cmd_prune(argv: list[str]) -> int:
    try:
        days = int(argv[1]) if len(argv) == 2 and argv[0] == "--days" else 0
    except ValueError:
        days = 0
    if days <= 0:
        raise HailError("usage: hail prune --days N (N > 0)")
    removed = default_store().prune(days)
    print(f"removed {removed} message(s) older than {argv[1]} days")
    return 0


def cmd_mcp(_argv: list[str]) -> int:
    from .mcp import serve

    return serve()


def check(agent: Agent) -> tuple[bool, str]:
    try:
        program = _program(agent)
    except ValueError as exc:
        return (False, f"cannot parse command: {exc}")
    if agent.host is None:
        search = os.pathsep.join([*(os.path.expanduser(p) for p in agent.path), os.environ.get("PATH", "")])
        found = shutil.which(os.path.expanduser(program), path=search)
        return (bool(found), found or f"'{program}' not found on PATH")
    script = f"command -v {remote_quote(program)}"
    if agent.path:
        script = "export PATH=" + ":".join(remote_quote(p) for p in agent.path) + ':"$PATH"; ' + script
    try:
        proc = subprocess.run(
            ["ssh", *SSH_OPTIONS, "--", agent.host, "sh -c " + shlex.quote(script)],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (False, f"ssh {agent.host}: timeout")
    except OSError as exc:
        return (False, f"cannot run ssh: {exc}")
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode == 0 and out:
        return (True, f"{agent.host}: {out}")
    err = proc.stderr.decode("utf-8", "replace").strip()[-200:]
    return (False, f"{agent.host}: '{program}' not found {err}".strip())


def _program(agent: Agent) -> str:
    if agent.preset == "command":
        posix = agent.is_remote or os.name != "nt"
        parts = shlex.split(agent.command or "", posix=posix)
        return parts[0].strip('"') if parts else ""
    return agent.variables.get("bin", agent.preset)


def to_toml(name: str, raw: dict) -> str:
    lines = [f"\n[agents.{name}]"]
    for key, value in raw.items():
        rendered = (
            str(value)
            if isinstance(value, int) and not isinstance(value, bool)
            else json.dumps(str(value), ensure_ascii=False)
        )
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def _pairs(items: list[str]) -> dict:
    pairs = {}
    for item in items:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not KEY_RE.match(key):
            raise ConfigError(f"--set expects KEY=VALUE with a simple key, got '{item}'")
        if key in FLAG_KEYS:
            raise ConfigError(f"use --{key} instead of --set {key}=...")
        if CONTROL_RE.search(value):
            raise ConfigError(f"--set {key}: control characters are not allowed")
        pairs[key] = int(value) if key in INT_KEYS and value.isdigit() else value
    return pairs


def _utf8_stdout() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")  # MCP frames must end in \n, not \r\n
