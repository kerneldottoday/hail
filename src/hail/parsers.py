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
MAX_FOOTER_LINES = 5  # a footer is a status line or two, never the answer itself
MAX_FOOTER_CHARS = 500  # longer lines are never tried: a status line is short, and a bad regex stays cheap


def split_footer(text: str, pattern: str | None) -> tuple[str, str]:
    """Move the trailing lines that match ``pattern`` (the agent's own status line) out of the reply.

    Only the last lines (split on newlines) are looked at, blank lines between them are dropped,
    at most MAX_FOOTER_LINES are taken, and the reply before them is kept exactly as it was.
    Returns (reply, footer); footer is "" when nothing matched.
    """
    if not pattern:
        return text, ""
    regex = re.compile(pattern)
    footer: list[str] = []
    cut = len(text)  # the reply is text[:cut]
    while cut > 0 and len(footer) < MAX_FOOTER_LINES:
        start = text.rfind("\n", 0, cut) + 1
        line = text[start:cut].rstrip("\r")
        if line.strip():
            if len(line) > MAX_FOOTER_CHARS or not regex.search(line):
                break
            footer.insert(0, line)
        cut = max(start - 1, 0)
    if not footer:
        return text, ""
    return text[:cut].rstrip(), "\n".join(footer)


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
