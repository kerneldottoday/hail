"""Unread marks for hail tui: pure functions."""

from hail import tui_state
from hail.board import ThreadRow


def row(thread, others, stopped=False):
    return ThreadRow(thread, "", others, stopped, "", 0.0, others)


def test_first_load_sees_everything_then_news_is_unread():
    seen = tui_state.mark_seen({}, (row("a", 2),), None, first=True)
    assert seen == {"a": 2} and not tui_state.is_unread(row("a", 2), seen)
    assert tui_state.is_unread(row("a", 3), seen) and tui_state.is_unread(row("b", 1), seen)


def test_the_open_thread_is_always_seen_and_memory_is_bounded():
    seen = tui_state.mark_seen({"a": 1, "gone": 4}, (row("a", 5), row("b", 2)), "a", first=False)
    assert seen == {"a": 5}  # "gone" left the screen; "b" was never seen


def test_flag_shows_the_dot_stop_or_both():
    assert tui_state.flag(row("a", 1), {"a": 1}) == ""
    assert tui_state.flag(row("a", 2), {"a": 1}) == "●"
    assert tui_state.flag(row("a", 1, stopped=True), {"a": 1}) == "STOP"
    assert tui_state.flag(row("a", 2, stopped=True), {"a": 1}) == "● STOP"
