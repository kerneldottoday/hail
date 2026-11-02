"""`hail tui`: the Textual screen, driven with Textual's Pilot (skipped when Textual is not installed)."""

import asyncio
import sqlite3

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input, RichLog, Static  # noqa: E402

from hail import cli, commands  # noqa: E402
from hail.config import load_config  # noqa: E402
from hail.errors import HailError  # noqa: E402
from hail.store import default_store  # noqa: E402
from hail.tui import HailBoard  # noqa: E402

CONFIG = """
[agents.claude]
preset = "claude"

[agents.hermes]
preset = "hermes"
host = "vps"
max_calls_per_day = 40
"""


@pytest.fixture
def journal(agents_file):
    agents_file(CONFIG)
    store = default_store()
    store.add("t-a", 1, "user", "hermes", "ask", "action", "is the scraper up?")
    store.add("t-a", 1, "hermes", "user", "reply", "action", "yes [bold]really[/bold]")
    store.add("t-b", 1, "user", "claude", "ask", "action", "review this")
    return store


def drive(app, steps):
    """Run the app headless, give it the pilot, return whatever ``steps`` returns."""

    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return await steps(pilot)

    return asyncio.run(go())


def board(store, sent=None, fail=False):
    def submit(agent, thread, message):
        if fail:
            raise HailError("too many background tasks")
        sent.append((agent, thread, message))
        return "k-1"

    return HailBoard(store, load_config(), submit)


def status_text(app) -> str:
    return str(app.query_one("#status", Static).render())


def test_lists_threads_newest_first_and_shows_the_first(journal):
    app = board(journal)

    async def steps(pilot):
        table = app.query_one("#threads", DataTable)
        return [str(table.get_row_at(i)[0]) for i in range(table.row_count)], app.current

    rows, current = drive(app, steps)
    assert rows == ["t-b", "t-a"] and current == "t-b"


def test_j_moves_to_the_next_thread_and_shows_it_as_plain_text(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("j")
        await pilot.pause()
        log = app.query_one("#log", RichLog)
        return app.current, "\n".join(line.text for line in log.lines)

    current, text = drive(app, steps)
    assert current == "t-a"
    assert "[bold]really[/bold]" in text and "untrusted" in text  # markup is never interpreted


def test_reply_goes_to_the_last_agent_asked_as_a_background_task(journal):
    sent: list = []
    app = board(journal, sent)

    async def steps(pilot):
        await pilot.press("j", "r")
        await pilot.press(*"go on")
        await pilot.press("enter")
        await pilot.pause()
        return status_text(app), app.query_one("#reply", Input).display

    status, visible = drive(app, steps)
    assert sent == [("hermes", "t-a", "go on")]
    assert "k-1" in status and visible is False


def test_reply_error_is_shown_not_raised(journal):
    app = board(journal, fail=True)

    async def steps(pilot):
        await pilot.press("r", *"x", "enter")
        await pilot.pause()
        return status_text(app)

    assert "not sent" in drive(app, steps)


def test_escape_cancels_a_reply(journal):
    sent: list = []
    app = board(journal, sent)

    async def steps(pilot):
        await pilot.press("r", *"oops", "escape")
        await pilot.pause()
        return app.query_one("#reply", Input).display

    assert drive(app, steps) is False and sent == []


def test_s_stops_and_resumes_the_thread(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("s")
        stopped = journal.is_stopped("t-b")
        await pilot.press("s")
        return stopped, journal.is_stopped("t-b")

    assert drive(app, steps) == (True, False)


def test_a_shows_agents_with_their_daily_budget(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("a")
        table = app.query_one("#agents", DataTable)
        rows = {str(table.get_row_at(i)[0]): [str(c) for c in table.get_row_at(i)] for i in range(table.row_count)}
        return table.display, rows

    shown, rows = drive(app, steps)
    assert shown and rows["hermes"][4] == "1/40" and rows["claude"][3] == "enforced"


def test_nothing_to_reply_to_and_an_empty_journal(agents_file):
    agents_file(CONFIG)
    app = board(default_store())

    async def steps(pilot):
        first = status_text(app)
        await pilot.press("r", "g")
        return first, status_text(app)

    first, after = drive(app, steps)
    assert "no threads yet" in first and "no threads yet" in after


def test_new_messages_appear_on_refresh(journal):
    app = board(journal)

    async def steps(pilot):
        journal.add("t-c", 1, "user", "claude", "ask", "action", "fresh")
        await pilot.press("g")
        table = app.query_one("#threads", DataTable)
        return str(table.get_row_at(0)[0]), app.current

    first, current = drive(app, steps)
    assert first == "t-c" and current == "t-b"  # the open thread stays open


def test_cli_tui_runs_the_board(agents_file, monkeypatch):
    agents_file(CONFIG)
    ran: list = []
    monkeypatch.setattr("hail.tui.HailBoard.run", lambda self: ran.append(self.submit))
    assert cli.main(["tui"]) == 0 and len(ran) == 1
    assert cli.main(["tui", "extra"]) == 2


def test_cli_tui_without_textual_says_how_to_install(agents_file, monkeypatch, capsys):
    agents_file(CONFIG)
    monkeypatch.setattr(commands.importlib.util, "find_spec", lambda name: None)
    assert cli.main(["tui"]) == 1
    assert "hail-agent[tui]" in capsys.readouterr().err


# review fixes ---------------------------------------------------------------------------


def log_lines(app) -> list[str]:
    return [line.text for line in app.query_one("#log", RichLog).lines]


def test_refresh_without_news_keeps_the_log_and_appends_new_messages(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("j")
        await pilot.pause()
        log = app.query_one("#log", RichLog)
        before = list(log.lines)
        await pilot.press("g")
        await pilot.pause()
        unchanged = log.lines == before
        journal.add("t-a", 2, "hermes", "user", "reply", "action", "and one more")
        await pilot.press("g")
        await pilot.pause()
        return unchanged, len(before), log_lines(app)

    unchanged, count, after = drive(app, steps)
    assert unchanged and len(after) == count + 2 and "and one more" in after[-1]


def test_a_journal_error_is_shown_not_fatal(journal, monkeypatch):
    app = board(journal)

    async def steps(pilot):
        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("hail.board.thread_rows", locked)
        await pilot.press("g")
        await pilot.pause()
        return app.is_running, status_text(app)

    running, status = drive(app, steps)
    assert running and "database is locked" in status


def test_the_agents_cursor_survives_a_refresh(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("a", "j", "g")
        await pilot.pause()
        return app.query_one("#agents", DataTable).cursor_row

    assert drive(app, steps) == 1


def test_a_reply_goes_where_the_prompt_said_even_if_the_journal_moves(journal):
    sent: list = []
    app = board(journal, sent)

    async def steps(pilot):
        await pilot.press("j", "r")
        journal.add("t-a", 2, "hermes", "claude", "ask", "advice", "meanwhile")  # last asked is now claude
        journal.add("t-z", 1, "user", "claude", "ask", "action", "a newer thread")
        app.action_reload()  # the timer firing while you type
        await pilot.press(*"hi", "enter")
        await pilot.pause()
        return sent

    assert drive(app, steps) == [("hermes", "t-a", "hi")]


def test_the_open_thread_stays_open_when_it_leaves_the_list(journal, monkeypatch):
    monkeypatch.setattr("hail.tui.THREAD_LIMIT", 2)
    app = board(journal)

    async def steps(pilot):
        await pilot.press("j")
        await pilot.pause()
        journal.add("t-y", 1, "user", "claude", "ask", "action", "y")
        journal.add("t-z", 1, "user", "claude", "ask", "action", "z")
        await pilot.press("g")
        await pilot.pause()
        table = app.query_one("#threads", DataTable)
        return app.current, str(table.get_row_at(table.cursor_row)[0]), table.row_count

    assert drive(app, steps) == ("t-a", "t-a", 3)  # kept at the end of the list, cursor on it


def test_markup_in_names_and_threads_is_shown_literally(agents_file):
    agents_file(CONFIG)
    store = default_store()
    store.add("t-[link=https://x]y[/link]", 1, "user", "hermes", "ask", "action", "hi")
    app = board(store)

    async def steps(pilot):
        await pilot.pause()
        table = app.query_one("#threads", DataTable)
        return str(table.get_row_at(0)[0]), app.is_running

    first, running = drive(app, steps)
    assert running and "[link=" in first
