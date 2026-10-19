"""Turn an agent's raw stdout into the reply text."""

from __future__ import annotations

import json
import re
from typing import Any

HERMES_NOISE = (
    re.compile(r"^session_id:"),
    re.compile(r"^\S+ \[[▰▱]+\]"),  # usage / cost bar
    re.compile(r"^Warning: Unknown toolsets"),
)


def validate(parser: str) -> None:
    if parser in ("text", "hermes") or (parser.startswith("json:") and len(parser) > 5):
        return
    raise ValueError(f"unknown parser '{parser}' (use text, hermes or json:<key>)")


def parse(parser: str, raw: str) -> str:
    validate(parser)
    if parser == "text":
        return raw.strip()
    if parser == "hermes":
        return _hermes(raw)
    return _json_key(raw, parser[len("json:") :])


def _hermes(raw: str) -> str:
    lines = [line for line in raw.splitlines() if not any(p.match(line.strip()) for p in HERMES_NOISE)]
    return "\n".join(lines).strip()


def _json_key(raw: str, key: str) -> str:
    """Return ``key`` from the first JSON object in ``raw`` that has it, else ``raw``."""
    decoder = json.JSONDecoder()
    start = raw.find("{")
    while start >= 0:
        try:
            data, _end = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            data = None
        found = find_key(data, key)
        if isinstance(found, str):
            return found.strip()
        start = raw.find("{", start + 1)
    return raw.strip()


def find_key(node: Any, key: str) -> Any:
    """Depth-first search for the first value stored under ``key``."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        children = list(node.values())
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        hit = find_key(child, key)
        if hit is not None:
            return hit
    return None
