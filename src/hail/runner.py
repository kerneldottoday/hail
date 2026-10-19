"""Build and run the call to an agent, locally or over SSH."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

from .config import Agent
from .parsers import parse
from .presets import get_preset

SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
MESSAGE = "{message}"
CMD_SHIM_SUFFIXES = (".cmd", ".bat")


class HailError(RuntimeError):
    """The agent could not be reached or returned nothing usable."""


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...] | str
    stdin: str | None
    shell: bool = False
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class Reply:
    agent: str
    text: str
    raw: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def build(agent: Agent, message: str) -> Invocation:
    if agent.host is not None:
        script = _remote_script(agent)
        ssh_argv: tuple[str, ...] = ("ssh", *SSH_OPTIONS, "--", agent.host, "sh -c " + shlex.quote(script))
        return Invocation(argv=ssh_argv, stdin=message)
    env = _local_env(agent)
    if agent.preset == "command":
        return Invocation(argv=agent.command or "", stdin=message, shell=True, env=env)
    template = get_preset(agent.preset).argv
    argv = tuple(_fill(t, agent, message) for t in template)
    program = os.path.expanduser(argv[0])
    search = env["PATH"] if env else None
    resolved = shutil.which(program, path=search) or program
    inline = MESSAGE in template
    if inline and resolved.lower().endswith(CMD_SHIM_SUFFIXES):
        # cmd.exe re-parses .cmd/.bat arguments; a message could inject commands.
        raise HailError(
            f"agent '{agent.name}': {resolved} is a Windows batch shim, which cannot receive "
            "a message safely as an argument. Use preset 'command' with a program that reads stdin."
        )
    return Invocation(argv=(resolved, *argv[1:]), stdin=None if inline else message, env=env)


def _local_env(agent: Agent) -> dict[str, str] | None:
    if not agent.path:
        return None
    dirs = [os.path.expanduser(p) for p in agent.path]
    return {**os.environ, "PATH": os.pathsep.join([*dirs, os.environ.get("PATH", "")])}


def _remote_script(agent: Agent) -> str:
    prefix = ""
    if agent.path:
        dirs = ":".join(remote_quote(p) for p in agent.path)
        prefix = f'export PATH={dirs}:"$PATH"; '
    if agent.preset == "command":
        return prefix + (agent.command or "")
    tokens = get_preset(agent.preset).argv
    rendered = " ".join('"$msg"' if t == MESSAGE else remote_quote(_fill(t, agent, "")) for t in tokens)
    if MESSAGE not in tokens:
        return f"{prefix}exec {rendered}"  # message stays on stdin
    return f"{prefix}msg=$(cat); exec {rendered}"


def _fill(token: str, agent: Agent, message: str) -> str:
    if token == MESSAGE:
        return message
    try:
        return token.format_map(agent.variables)
    except KeyError as exc:
        raise HailError(f"agent '{agent.name}': missing setting {exc}") from None


def remote_quote(token: str) -> str:
    """Quote for a POSIX shell, keeping a leading ~/ expandable."""
    if token.startswith("~/"):
        rest = token[2:].replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        return f'"$HOME/{rest}"'
    return shlex.quote(token)


def ask(agent: Agent, message: str) -> Reply:
    message = message.strip()
    if not message:
        raise HailError("empty message")
    if "\x00" in message:
        raise HailError("message contains a NUL byte")
    if len(message) > agent.max_chars:
        raise HailError(f"message too long ({len(message)} > {agent.max_chars} characters)")
    inv = build(agent, message)
    try:
        proc = subprocess.run(
            inv.argv,
            input=inv.stdin.encode("utf-8") if inv.stdin is not None else None,
            capture_output=True,
            timeout=agent.timeout,
            shell=inv.shell,
            env=inv.env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HailError(f"'{agent.name}' did not answer within {agent.timeout}s") from None
    except FileNotFoundError as exc:
        raise HailError(f"'{agent.name}': command not found ({exc.filename})") from None
    except OSError as exc:
        raise HailError(f"'{agent.name}': could not start ({exc})") from None
    raw = proc.stdout.decode("utf-8", errors="replace")
    text = parse(agent.parser, raw)
    if not text:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise HailError(f"'{agent.name}' returned nothing (exit {proc.returncode}): {err}")
    return Reply(agent=agent.name, text=text, raw=raw, exit_code=proc.returncode)
