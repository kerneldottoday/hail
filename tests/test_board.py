"""board.py: what `hail tui` shows, read from the journal and the agents file (no Textual needed)."""

from hail import board
from hail.config import load_config
from hail.store import default_store

CONFIG = """
[agents.claude]
preset = "claude"
skills = ["code-review"]

[agents.hermes]
preset = "hermes"
host = "vps"
max_calls_per_day = 40
"""


def test_thread_rows_newest_first_with_stop_mark(tmp_path):
    store = default_store()
    store.add("t-old", 0, "user", "claude", "ask", "action", "first")
    store.add("t-new", 1, "hermes", "claude", "ask", "advice", "second")
    store.set_stopped("t-old", True)
    rows = board.thread_rows(store, 10)
    assert [r.thread for r in rows] == ["t-new", "t-old"]
    assert rows[1].stopped and not rows[0].stopped and rows[0].messages == 1
    assert rows[0].agents == "claude, hermes" and rows[1].agents == "claude"


def test_message_lines_are_cleaned_and_marked(tmp_path):
    store = default_store()
    store.add("t-1", 1, "user", "hermes", "ask", "action", "status?")
    store.add("t-1", 1, "hermes", "user", "reply", "action", "ok\x1b]0;pwned\x07 [red]all good[/red]")
    lines = board.message_lines(store, "t-1")
    assert lines[0].who == "user → hermes" and lines[0].kind == "ask"
    assert "\x1b" not in lines[1].text and "[red]" in lines[1].text  # kept as text, never as markup
    assert lines[1].untrusted and not lines[0].untrusted


def test_reply_target_is_the_last_agent_that_was_asked(tmp_path):
    store = default_store()
    store.add("t-1", 1, "user", "hermes", "ask", "action", "a")
    store.add("t-1", 1, "hermes", "user", "reply", "action", "b")
    store.add("t-1", 2, "hermes", "claude", "ask", "advice", "c")
    assert board.reply_target(store.thread("t-1")) == "claude"
    assert board.reply_target(()) is None


def test_agent_rows_show_daily_budget(agents_file):
    agents_file(CONFIG)
    store = default_store()
    store.add("t-1", 1, "user", "hermes", "ask", "action", "a")
    store.add("t-1", 1, "user", "hermes", "ask", "action", "b")
    rows = {r.name: r for r in board.agent_rows(load_config(), store)}
    assert rows["hermes"].calls_today == 2 and rows["hermes"].daily_cap == 40
    assert rows["hermes"].where == "vps" and rows["claude"].where == "local"
    assert rows["claude"].daily_cap is None and rows["claude"].skills == "code-review"
    assert rows["hermes"].advice_enforced and rows["claude"].advice_enforced
