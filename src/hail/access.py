"""[policy] cli_access: what an agent that runs hail itself may do. You (no HAIL_SELF) are never held to it.

all  = everything (default) · read = look, never send, change or mark anything · none = nothing but help.
An agent reaching hail over MCP is always held to it. It guards hail's own commands, not the files:
a program running as you can still read ~/.hail or the agents file directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from .config import config_path, load_config
from .envelope import USER, from_env
from .errors import Refused

LEVELS = ("all", "read", "none")
READ_COMMANDS = frozenset({"list", "doctor", "log", "threads", "thread", "status", "jobs", "inbox"})
READ_TOOLS = frozenset({"hail_list", "hail_status", "hail_jobs", "hail_inbox", "hail_thread"})
WRITE_INBOX = frozenset({"read", "clear"})  # reading an item marks it as read
# mcp: each tool is checked on its own. _task: starts a task that was allowed when sent, at most once.
EXEMPT = frozenset({"mcp", "_task"})


def command_name(argv: Sequence[str], known: frozenset[str]) -> str:
    """The command as cli_access sees it: 'inbox read' writes, an unknown first word is an ask."""
    name = argv[0] if argv[0] in known else "ask"
    if name == "inbox":  # options may come first (hail inbox --all clear): look at every word
        writes = [w for w in argv[1:] if w in WRITE_INBOX]
        return f"inbox {writes[0]}" if writes else name
    return name


def level() -> str:
    """The configured level; 'all' when there is no agents file yet."""
    if not config_path().is_file():
        return "all"
    return load_config().policy.cli_access


def is_agent(environ: Mapping[str, str] | None = None) -> bool:
    return from_env(os.environ if environ is None else environ).name != USER


def enforce(action: str, reading: bool) -> None:
    current = level()
    if current == "all" or (current == "read" and reading):
        return
    what = "only lets agents read" if current == "read" else "lets agents run nothing"
    raise Refused(f"'{action}' refused: [policy] cli_access = {current} {what} (ask the user)")


def enforce_cli(argv: Sequence[str], known: frozenset[str]) -> None:
    name = command_name(argv, known)
    if name not in EXEMPT and is_agent():
        enforce(f"hail {name}", name in READ_COMMANDS)


def enforce_tool(tool: str) -> None:
    enforce(tool, tool in READ_TOOLS)
