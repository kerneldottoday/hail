"""Mask secrets (API keys, tokens, passwords, private keys) before text is stored or posted.

Pattern-based: a safety net, not a guarantee.
"""

from __future__ import annotations

import re

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)
KEY_VALUE = re.compile(
    r"(?i)([\"']?)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key)\1(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return KEY_VALUE.sub(r"\1\2\1\3[redacted]", text)
