"""Fill a throwaway hail home with a made-up journal for the `hail tui` demo (docs/demo/tui.tape).

Usage: HAIL_HOME=<dir>/home HAIL_CONFIG=<dir>/agents.toml python docs/demo/seed.py
Everything here is fictional; never point it at a real hail home.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from hail.store import default_store

AGENTS = """[agents.claude]
preset = "claude"

[agents.hermes]
preset = "hermes"
host = "vps"
max_calls_per_day = 40

[agents.openclaw]
preset = "openclaw"
host = "vps"
"""

THREADS = (
    ("t-scraper", "user", "hermes", "ask", "Is the bus feed scraper still running? Last file looks old."),
    ("t-scraper", "hermes", "user", "reply", "It stopped at 06:12: the feed answers 503. Restarted, next pull 06:40."),
    ("t-review", "user", "claude", "ask", "Review the diff of the exit-code change before I tag 0.5."),
    ("t-review", "claude", "user", "reply", "Two issues: no code 75 on SSH timeout, and the docs say 6."),
    ("t-review", "claude", "hermes", "ask", "Can you rerun the SSH timeout test on the VPS?"),
    ("t-review", "hermes", "claude", "reply", "Ran it 3 times: exit 255 from ssh, hail maps it to 4, not 75."),
    ("t-backup", "user", "openclaw", "ask", "Did last night's backup of the notes server finish?"),
    ("t-backup", "openclaw", "user", "reply", "Yes, 412 MB, checksum verified, kept 7 copies."),
)


def main() -> int:
    config = os.environ.get("HAIL_CONFIG", "")
    if not config or not os.environ.get("HAIL_HOME"):
        print("set HAIL_HOME and HAIL_CONFIG to a throwaway directory first", file=sys.stderr)
        return 2
    Path(config).write_text(AGENTS, encoding="utf-8")
    store = default_store()
    for thread, sender, recipient, kind, text in THREADS:
        store.add(thread, 1, sender, recipient, kind, "action", text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
