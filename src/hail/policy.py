"""Guard rails for agent-to-agent calls: who may consult whom, and how much.

Defaults: any agent may ask any other for *advice*; nobody but the user may ask
an agent to *act* unless ``[policy.act]`` says so. Chains stop after
``max_hops``, a thread after ``max_calls_per_thread`` asks, and the whole
machine after ``rate_per_minute`` asks (circuit breaker against loops that
lose their thread id).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .envelope import DEFAULT_MAX_HOPS, MODES, USER

KNOWN_KEYS = frozenset({"max_hops", "max_calls_per_thread", "rate_per_minute", "talk", "act"})
EMPTY: Mapping[str, frozenset[str]] = MappingProxyType({})


class PolicyError(ValueError):
    """The [policy] table is invalid."""


@dataclass(frozen=True)
class Policy:
    max_hops: int = DEFAULT_MAX_HOPS
    max_calls_per_thread: int = 12
    rate_per_minute: int = 30
    talk: Mapping[str, frozenset[str]] = field(default_factory=lambda: EMPTY)
    act: Mapping[str, frozenset[str]] = field(default_factory=lambda: EMPTY)

    def check(self, sender: str, target: str, mode: str) -> str | None:
        """Return why ``sender`` may not reach ``target`` in ``mode``, or None if allowed."""
        if mode not in MODES:
            return f"unknown mode '{mode}' (use {' or '.join(MODES)})"
        if sender == USER:
            return None
        if sender == target:
            return f"'{sender}' cannot consult itself"
        acts = self.act.get(sender, frozenset())
        if mode == "action" and target not in acts:
            return f"'{sender}' may not ask '{target}' for an action; use mode advice or allow it in [policy.act]"
        if sender in self.talk and target not in self.talk[sender] and target not in acts:
            return f"'{sender}' is not allowed to consult '{target}' (see [policy.talk])"
        return None


def parse_policy(raw: Any) -> Policy:
    if raw is None:
        return Policy()
    if not isinstance(raw, dict):
        raise PolicyError("[policy] must be a table")
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        raise PolicyError(f"[policy]: unknown key(s) {', '.join(sorted(unknown))}")
    defaults = Policy()
    return Policy(
        max_hops=_positive(raw, "max_hops", defaults.max_hops),
        max_calls_per_thread=_positive(raw, "max_calls_per_thread", defaults.max_calls_per_thread),
        rate_per_minute=_positive(raw, "rate_per_minute", defaults.rate_per_minute),
        talk=_lists(raw, "talk"),
        act=_lists(raw, "act"),
    )


def _positive(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyError(f"[policy] {key} must be a positive integer")
    return value


def _lists(raw: dict[str, Any], key: str) -> Mapping[str, frozenset[str]]:
    table = raw.get(key, {})
    if not isinstance(table, dict):
        raise PolicyError(f"[policy.{key}] must be a table of agent = [agents]")
    parsed: dict[str, frozenset[str]] = {}
    for name, targets in table.items():
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise PolicyError(f"[policy.{key}] {name} must be a list of agent names")
        parsed[name] = frozenset(targets)
    return MappingProxyType(parsed)
