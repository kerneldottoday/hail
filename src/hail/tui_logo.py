"""The shimmering "hail" logo shown in the right pane of `hail tui` when no thread is open.

Pure functions: ``frame(step)`` returns the logo as rich Text with a band of light that sweeps
diagonally across it; the app calls it on a timer. No Textual import here.
"""

from __future__ import annotations

from rich.text import Text

LOGO = (
    r" _           _ _ ",
    r"| |__   __ _(_) |",
    r"| '_ \ / _` | | |",
    r"| | | | (_| | | |",
    r"|_| |_|\__,_|_|_|",
)
TAGLINE = "threads between your agents"
BASE = "#3d5f94"  # resting colour of the letters
GLOW = ("#ffffff", "#cfe0ff", "#94b6ec", "#6d90c8")  # centre of the band outwards
BAND = len(GLOW)
WIDTH = max(len(line) for line in LOGO)
PERIOD = WIDTH + len(LOGO) + 2 * BAND + 12  # sweep across, then a short rest before the next pass


def _colour(col: int, row: int, step: int) -> str:
    head = step % PERIOD - BAND  # where the band is on this frame (it starts off the left edge)
    distance = abs(col + row - head)
    return GLOW[distance] if distance < BAND else BASE


def frame(step: int) -> Text:
    """The logo and its tagline for animation step ``step`` (any int; the sweep repeats)."""
    out = Text(justify="center")
    for row, line in enumerate(LOGO):
        for col, char in enumerate(line.ljust(WIDTH)):
            out.append(char, style="" if char == " " else f"bold {_colour(col, row, step)}")
        out.append("\n")
    out.append("\n" + TAGLINE, style="dim")
    return out
