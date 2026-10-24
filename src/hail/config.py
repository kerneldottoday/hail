"""Load and validate the agents file (TOML)."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

from . import parsers
from .policy import Policy, PolicyError, parse_policy
from .presets import get_preset

DEFAULT_TIMEOUT = 180
DEFAULT_MAX_CHARS = 8000
RESERVED_NAMES = frozenset(
    {
        "list",
        "init",
        "add",
        "doctor",
        "mcp",
        "help",
        "version",
        "ask",
        "send",
        "status",
        "council",
        "log",
        "thread",
        "threads",
        "stop",
        "resume",
        "telegram",
    }
)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(r"^[A-Za-z0-9_.@:\[\]-]+$")
LOCAL_HOSTS = frozenset({"", "local", "localhost"})
KNOWN_KEYS = frozenset(
    {"preset", "host", "timeout", "max_chars", "command", "parser", "path", "description", "skills", "cost"}
)
COSTS = ("low", "medium", "high")


class ConfigError(ValueError):
    """The agents file is missing or invalid."""


@dataclass(frozen=True)
class Agent:
    name: str
    preset: str
    host: str | None = None
    timeout: int = DEFAULT_TIMEOUT
    max_chars: int = DEFAULT_MAX_CHARS
    command: str | None = None
    parser: str = "text"
    path: tuple[str, ...] = ()
    description: str = ""
    variables: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    skills: tuple[str, ...] = ()
    cost: str = "medium"

    @property
    def is_remote(self) -> bool:
        return self.host is not None


def config_path() -> Path:
    env = os.environ.get("HAIL_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "hail" / "agents.toml"


@dataclass(frozen=True)
class Config:
    agents: Mapping[str, Agent]
    policy: Policy
    telegram: Mapping[str, Any] | None


def load_agents(path: Path | None = None) -> dict[str, Agent]:
    return dict(load_config(path).agents)


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    data = _read(path)
    table = data.get("agents", {})
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [agents] must be a table")
    try:
        policy = parse_policy(data.get("policy"))
    except PolicyError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    telegram = data.get("telegram")
    if telegram is not None and not isinstance(telegram, dict):
        raise ConfigError(f"{path}: [telegram] must be a table")
    agents = {name: parse_agent(name, raw) for name, raw in table.items()}
    return Config(agents=MappingProxyType(agents), policy=policy, telegram=telegram)


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"no agents file at {path} (run: hail init)")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None


def parse_agent(name: str, raw: Any) -> Agent:
    if not NAME_RE.match(name) or name in RESERVED_NAMES:
        raise ConfigError(f"invalid agent name '{name}' (lowercase, digits, - or _; not a command name)")
    if not isinstance(raw, dict):
        raise ConfigError(f"agent '{name}' must be a table")
    preset_name = str(raw.get("preset", "command"))
    try:
        preset = get_preset(preset_name)
    except ValueError as exc:
        raise ConfigError(f"agent '{name}': {exc}") from None
    command = raw.get("command")
    if command is not None and not isinstance(command, str):
        raise ConfigError(f"agent '{name}': 'command' must be a string")
    if preset_name == "command" and not command:
        raise ConfigError(f"agent '{name}': preset 'command' needs a 'command' value")
    host = _host(name, raw.get("host"))
    parser = str(raw.get("parser", preset.parser))
    try:
        parsers.validate(parser)
    except ValueError as exc:
        raise ConfigError(f"agent '{name}': {exc}") from None
    path = raw.get("path", [])
    if not isinstance(path, list) or not all(isinstance(p, str) for p in path):
        raise ConfigError(f"agent '{name}': 'path' must be a list of strings")
    variables = {**preset.defaults, **_extra_vars(raw)}
    return Agent(
        name=name,
        preset=preset_name,
        host=host,
        timeout=_positive_int(name, raw, "timeout", DEFAULT_TIMEOUT),
        max_chars=_positive_int(name, raw, "max_chars", DEFAULT_MAX_CHARS),
        command=command,
        parser=parser,
        path=tuple(path),
        description=str(raw.get("description", preset.description)),
        variables=MappingProxyType(variables),
        skills=_skills(name, raw.get("skills", [])),
        cost=_cost(name, raw.get("cost", "medium")),
    )


def _skills(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(s, str) and s.strip() for s in value):
        raise ConfigError(f"agent '{name}': 'skills' must be a list of words")
    return tuple(s.strip().lower() for s in value)


def _cost(name: str, value: Any) -> str:
    if value not in COSTS:
        raise ConfigError(f"agent '{name}': 'cost' must be one of {', '.join(COSTS)}")
    return str(value)


def _host(name: str, value: Any) -> str | None:
    if value is None or str(value).strip() in LOCAL_HOSTS:
        return None
    host = str(value).strip()
    if host.startswith("-") or not HOST_RE.match(host):
        raise ConfigError(f"agent '{name}': invalid host '{host}' (use an SSH alias or user@host)")
    return host


def _extra_vars(raw: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in raw.items() if k not in KNOWN_KEYS}


def _positive_int(name: str, raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"agent '{name}': '{key}' must be a positive integer")
    return value
