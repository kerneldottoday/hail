"""Built-in agent presets.

A preset is an argv template. Tokens in braces are filled from the agent's
config; ``{message}`` is the message itself. A template without ``{message}``
receives the message on stdin. The same template is used for local runs (argv,
no shell) and remote runs (a shell script that reads the message from stdin, so
the message is never part of a command line).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class Preset:
    argv: tuple[str, ...]
    parser: str = "text"
    defaults: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    description: str = ""
    advice_argv: tuple[str, ...] = ()  # appended in advice mode (read-only answers)


PRESETS: Mapping[str, Preset] = MappingProxyType(
    {
        "hermes": Preset(
            argv=("{bin}", "chat", "-q", "{message}", "-Q", "--max-turns", "{max_turns}"),
            parser="hermes",
            defaults=MappingProxyType({"bin": "hermes", "max_turns": "8"}),
            description="Nous Research Hermes Agent (hermes chat -q)",
        ),
        "openclaw": Preset(
            argv=(
                "{bin}",
                "agent",
                "--agent",
                "{agent_id}",
                "--session-key",
                "{session}",
                "--message",
                "{message}",
                "--json",
            ),
            parser="json:finalAssistantVisibleText",
            defaults=MappingProxyType({"bin": "openclaw", "agent_id": "main", "session": "hail"}),
            description="OpenClaw agent (openclaw agent --json), isolated 'hail' session",
        ),
        "claude": Preset(
            argv=("{bin}", "-p"),
            defaults=MappingProxyType({"bin": "claude"}),
            advice_argv=("--permission-mode", "plan"),
            description="Claude Code in print mode (claude -p, prompt on stdin)",
        ),
        "command": Preset(
            argv=(),
            description="Any shell command; the message is sent on stdin",
        ),
    }
)


def get_preset(name: str) -> Preset:
    try:
        return PRESETS[name]
    except KeyError:
        known = ", ".join(PRESETS)
        raise ValueError(f"unknown preset '{name}' (known: {known})") from None
