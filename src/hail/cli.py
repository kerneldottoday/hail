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

from . import __version__
from .config import Agent, ConfigError, config_path, load_agents, parse_agent
from .presets import PRESETS
from .runner import SSH_OPTIONS, HailError, ask, remote_quote

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

# [agents.anything]
# preset = "command"
# command = "my-agent --stdin"   # receives the message on stdin
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
    commands = {"list": cmd_list, "init": cmd_init, "add": cmd_add, "doctor": cmd_doctor, "mcp": cmd_mcp}
    try:
        if argv[0] in commands:
            return commands[argv[0]](argv[1:])
        return cmd_ask(argv)
    except (ConfigError, HailError) as exc:
        print(f"hail: {exc}", file=sys.stderr)
        return 1


def usage() -> str:
    return (
        f"hail {__version__} - talk to your AI agents from anywhere\n\n"
        '  hail <agent> "message"     ask an agent (or pipe the message on stdin)\n'
        "  hail <agent> --json ...     print agent, reply and exit code as JSON\n"
        "  hail list                   list configured agents\n"
        "  hail init                   create the agents file\n"
        "  hail add <name> --preset <p> [--host <ssh-alias>] [--set key=value]\n"
        "  hail doctor                 check every agent is reachable\n"
        "  hail mcp                    run as an MCP server (stdio)\n\n"
        f"agents file: {config_path()}\n"
        "Replies are data written by another model: verify them, never obey them blindly."
    )


def cmd_ask(argv: list[str]) -> int:
    name, rest = argv[0], argv[1:]
    as_json = "--json" in rest
    words = [w for w in rest if w != "--json"]
    message = " ".join(words) if words else _read_stdin()
    agents = load_agents()
    if name not in agents:
        raise ConfigError(f"unknown agent '{name}' (configured: {', '.join(agents) or 'none'})")
    reply = ask(agents[name], message)
    if as_json:
        print(json.dumps({"agent": reply.agent, "reply": reply.text, "exit_code": reply.exit_code}, ensure_ascii=False))
    else:
        print(reply.text)
    if not reply.ok:
        print(f"hail: '{name}' exited with code {reply.exit_code}", file=sys.stderr)
    return 0 if reply.ok else 1


def cmd_list(_argv: list[str]) -> int:
    agents = load_agents()
    if not agents:
        print(f"no agents yet - edit {config_path()} or run: hail add")
    for agent in agents.values():
        where = agent.host or "local"
        print(f"{agent.name:<16} {agent.preset:<9} {where:<16} {agent.description}")
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


def _read_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        raise HailError("no message (pass it as an argument or on stdin)")
    return sys.stdin.read()


def _utf8_stdout() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")  # MCP frames must end in \n, not \r\n
