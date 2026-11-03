"""Build and run the call to an agent, locally or over SSH."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .config import Agent
from .errors import AgentUnusable, Temporary, UsageError
from .errors import HailError as HailError  # re-exported: older imports use hail.runner.HailError
from .parsers import parse, split_footer
from .presets import get_preset

SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
MESSAGE = "{message}"
CMD_SHIM_SUFFIXES = (".cmd", ".bat")
KILL_GRACE = 5  # seconds between TERM and KILL on the server
ADVICE_COMMAND = "advice_command"  # preset 'command': the read-only variant run in advice mode
SSH_FAILED = 255  # ssh's own exit code when it could not connect or authenticate
RETRY_WINDOW = 15.0  # seconds: an SSH failure this fast happened before the agent could start
RETRY_PAUSE = 2.0  # seconds before the single retry
# ssh messages that only appear before a session (and so the agent) could start. A reset or broken pipe
# after login, or an agent exiting 255 itself, is not in this list: resending could run the agent twice.
BEFORE_SESSION = (
    "could not resolve hostname",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "no route to host",
    "network is unreachable",
    "kex_exchange_identification",
    "connection closed by",
)


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
    footer: str = ""  # the agent's own status line, moved out of text (see the 'footer' setting)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def build(
    agent: Agent,
    message: str,
    env: Mapping[str, str] | None = None,
    mode: str = "action",
    drop_env: Sequence[str] = (),
) -> Invocation:
    """``env`` is exported to the agent (hail context); ``mode`` advice adds the preset's read-only flags;
    ``drop_env`` names variables (secrets such as the Telegram token) the agent must not inherit."""
    extra = dict(env or {})
    if agent.host is not None and agent.forced_command:
        # The server ignores the command line and runs its own (authorized_keys command=), so send nothing
        # but the message. "true" keeps a misconfigured server harmless: it runs, prints nothing, hail errors.
        return Invocation(argv=("ssh", *SSH_OPTIONS, "--", agent.host, "true"), stdin=message)
    if agent.host is not None:
        script = _remote_script(agent, extra, mode)
        ssh_argv: tuple[str, ...] = ("ssh", *SSH_OPTIONS, "--", agent.host, "sh -c " + shlex.quote(script))
        return Invocation(argv=ssh_argv, stdin=message)
    local_env = _local_env(agent, extra, drop_env)
    if agent.preset == "command":
        return Invocation(argv=_command(agent, mode), stdin=message, shell=True, env=local_env)
    template = _template(agent, mode)
    argv = tuple(_fill(t, agent, message) for t in template)
    program = os.path.expanduser(argv[0])
    search = local_env.get("PATH") if local_env else None
    resolved = shutil.which(program, path=search) or program
    inline = MESSAGE in template
    if inline and resolved.lower().endswith(CMD_SHIM_SUFFIXES):
        # cmd.exe re-parses .cmd/.bat arguments; a message could inject commands.
        raise AgentUnusable(
            f"agent '{agent.name}': {resolved} is a Windows batch shim, which cannot receive "
            "a message safely as an argument. Use preset 'command' with a program that reads stdin."
        )
    return Invocation(argv=(resolved, *argv[1:]), stdin=None if inline else message, env=local_env)


def _template(agent: Agent, mode: str) -> tuple[str, ...]:
    preset = get_preset(agent.preset)
    if mode != "advice":
        return preset.argv
    swaps = {f"{{{key}}}": f"{{{alt}}}" for key, alt in preset.advice_swap.items() if agent.variables.get(alt)}
    return tuple(swaps.get(t, t) for t in preset.argv) + preset.advice_argv


def _command(agent: Agent, mode: str) -> str:
    advice = agent.variables.get(ADVICE_COMMAND) if mode == "advice" else None
    return advice or agent.command or ""


def advice_enforced(agent: Agent) -> bool:
    """True when advice mode is enforced by the agent itself (flags, a read-only agent), not just asked for."""
    preset = get_preset(agent.preset)
    if preset.advice_argv or preset.no_tools:
        return True
    if agent.preset == "command":
        return bool(agent.variables.get(ADVICE_COMMAND))
    return any(agent.variables.get(alt) for alt in preset.advice_swap.values())


def _local_env(agent: Agent, extra: dict[str, str], drop: Sequence[str] = ()) -> dict[str, str] | None:
    hidden = [k for k in drop if k in os.environ]
    if not agent.path and not extra and not hidden:
        return None
    env = {k: v for k, v in os.environ.items() if k not in hidden}
    env.update(extra)
    if agent.path:
        dirs = [os.path.expanduser(p) for p in agent.path]
        env["PATH"] = os.pathsep.join([*dirs, os.environ.get("PATH", "")])
    return env


def _remote_script(agent: Agent, extra: dict[str, str], mode: str) -> str:
    prefix = ""
    if extra:
        prefix += "export " + " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(extra.items(), key=_env_order)) + "; "
    if agent.path:
        dirs = ":".join(remote_quote(p) for p in agent.path)
        prefix += f'export PATH={dirs}:"$PATH"; '
    # Bound the agent on the server too: killing the local ssh does not stop it there.
    # Probe rather than look it up: an old BusyBox timeout without -k would refuse to start the agent.
    prefix += f"timeout -k 1 1 true >/dev/null 2>&1 && set -- timeout -k {KILL_GRACE} {agent.timeout}; "
    if agent.preset == "command":
        return f'{prefix}exec "$@" sh -c {shlex.quote(_command(agent, mode))}'
    tokens = _template(agent, mode)
    rendered = " ".join('"$msg"' if t == MESSAGE else remote_quote(_fill(t, agent, "")) for t in tokens)
    if MESSAGE not in tokens:
        return f'{prefix}exec "$@" {rendered}'  # message stays on stdin
    return f'{prefix}msg=$(cat); exec "$@" {rendered}'


def _fill(token: str, agent: Agent, message: str) -> str:
    if token == MESSAGE:
        return message
    try:
        return token.format_map(agent.variables)
    except KeyError as exc:
        raise AgentUnusable(f"agent '{agent.name}': missing setting {exc}") from None


def remote_quote(token: str) -> str:
    """Quote for a POSIX shell, keeping a leading ~/ expandable."""
    if token.startswith("~/"):
        rest = token[2:].replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        return f'"$HOME/{rest}"'
    return shlex.quote(token)


def _env_order(item: tuple[str, str]) -> int:
    order = ("HAIL_THREAD", "HAIL_HOP", "HAIL_SELF")
    return order.index(item[0]) if item[0] in order else len(order)


def ask(
    agent: Agent,
    message: str,
    env: Mapping[str, str] | None = None,
    mode: str = "action",
    drop_env: Sequence[str] = (),
) -> Reply:
    message = message.strip()
    if not message:
        raise UsageError("empty message")
    if "\x00" in message:
        raise UsageError("message contains a NUL byte")
    if len(message) > agent.max_chars:
        raise UsageError(f"message too long ({len(message)} > {agent.max_chars} characters)")
    inv = build(agent, message, env=env, mode=mode, drop_env=drop_env)
    started = time.monotonic()
    proc = _run(agent, inv)
    if _ssh_failed_early(agent, proc, time.monotonic() - started):
        time.sleep(RETRY_PAUSE)  # once, and only when ssh says the session never opened
        proc = _run(agent, inv)
    raw = proc.stdout.decode("utf-8", errors="replace")
    text = parse(agent.parser, raw)
    if not text:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
        if agent.is_remote and proc.returncode == SSH_FAILED:
            raise Temporary(f"'{agent.name}': ssh could not reach {agent.host} (exit 255): {err}")
        raise AgentUnusable(f"'{agent.name}' returned nothing (exit {proc.returncode}): {err}")
    body, footer = split_footer(text, agent.footer)
    if not body:
        raise AgentUnusable(f"'{agent.name}' returned only its footer, no answer (exit {proc.returncode})")
    return Reply(agent=agent.name, text=body, raw=raw, exit_code=proc.returncode, footer=footer)


def _ssh_failed_early(agent: Agent, proc: subprocess.CompletedProcess[bytes], elapsed: float) -> bool:
    if not (agent.is_remote and proc.returncode == SSH_FAILED and not proc.stdout.strip() and elapsed < RETRY_WINDOW):
        return False
    err = proc.stderr.decode("utf-8", errors="replace").lower()
    return any(marker in err for marker in BEFORE_SESSION)


def _run(agent: Agent, inv: Invocation) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            inv.argv,
            input=inv.stdin.encode("utf-8") if inv.stdin is not None else None,
            # no message on stdin: give an empty one, never hail's own (the MCP stream under ``hail mcp``)
            stdin=subprocess.DEVNULL if inv.stdin is None else None,
            capture_output=True,
            timeout=agent.timeout,
            shell=inv.shell,
            env=inv.env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Temporary(f"'{agent.name}' did not answer within {agent.timeout}s") from None
    except FileNotFoundError as exc:
        raise AgentUnusable(f"'{agent.name}': command not found ({exc.filename})") from None
    except OSError as exc:
        raise AgentUnusable(f"'{agent.name}': could not start ({exc})") from None
