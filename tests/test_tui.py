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
    assert rows == ["t-b", "t-a"] and current is None  # nothing open: the logo shows


def test_j_moves_to_the_next_thread_and_shows_it_as_plain_text(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("j", "enter")
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
        await pilot.press("j", "enter", "r")
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
        await pilot.press("enter", "r", *"x", "enter")
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
        await pilot.press("enter", "s")
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
        await pilot.press("enter")
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
        await pilot.press("j", "enter")
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
        await pilot.press("j", "enter", "r")
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
        await pilot.press("j", "enter")
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


# tele-style layout -----------------------------------------------------------------------


def shown(app, widget_id: str) -> bool:
    return bool(app.query_one(widget_id).display)


def test_starts_on_the_logo_and_enter_opens_then_escape_closes(journal):
    app = board(journal)

    async def steps(pilot):
        start = (shown(app, "#logo"), shown(app, "#log"), str(app.query_one("#pane2").border_title))
        await pilot.press("enter")
        await pilot.pause()
        opened = (shown(app, "#logo"), shown(app, "#log"), app.current)
        await pilot.press("escape")
        await pilot.pause()
        return start, opened, (shown(app, "#logo"), app.current)

    start, opened, closed = drive(app, steps)
    assert start == (True, False, "[2] (no thread)")
    assert opened == (False, True, "t-b")
    assert closed == (True, None)


def test_folders_count_threads_per_agent(journal):
    app = board(journal)

    async def steps(pilot):
        table = app.query_one("#folders", DataTable)
        return {str(table.get_row_at(i)[0]): str(table.get_row_at(i)[1]) for i in range(table.row_count)}

    folders = drive(app, steps)
    assert folders == {"All": "[2]", "claude": "[1]", "hermes": "[1]", "Stopped": "", "Today": "[2]"}


def test_picking_a_folder_filters_the_threads(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("0", "j", "j")  # All -> claude -> hermes
        await pilot.pause()
        table = app.query_one("#threads", DataTable)
        return app.folder, [str(table.get_row_at(i)[0]) for i in range(table.row_count)]

    assert drive(app, steps) == ("agent:hermes", ["t-a"])


def test_new_messages_get_an_unread_dot_until_opened(journal):
    app = board(journal)

    async def steps(pilot):
        def flag() -> str:
            table = app.query_one("#threads", DataTable)
            return str(table.get_row_at(table.get_row_index("t-a"))[4])

        before = flag()
        journal.add("t-a", 2, "hermes", "user", "reply", "action", "news")
        await pilot.press("g")
        await pilot.pause()
        dotted = flag()
        app.open_thread("t-a")
        await pilot.pause()
        return before, dotted, flag()

    assert drive(app, steps) == ("", "●", "")


def test_mode_shows_insert_while_replying(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("enter", "r")
        await pilot.pause()
        insert = str(app.query_one("#mode", Static).render())
        await pilot.press("escape")
        await pilot.pause()
        return insert, str(app.query_one("#mode", Static).render())

    assert drive(app, steps) == ("INSERT", "NORMAL")


def test_h_l_and_numbers_move_between_panes(journal):
    app = board(journal)

    async def steps(pilot):
        seen = []
        for keys in (("l",), ("enter", "l"), ("h",), ("0",), ("2",)):
            await pilot.press(*keys)
            await pilot.pause()
            seen.append(app.focused.id)
        return seen

    # no thread open: [2] holds only the logo, so "l" stays on threads
    assert drive(app, steps) == ["threads", "log", "threads", "folders", "log"]


# review fixes (tele layout) --------------------------------------------------------------


def test_the_logo_shimmers_only_while_shown(journal):
    app = board(journal)

    async def steps(pilot):
        first = app._step
        app._shimmer()
        moved = app._step
        await pilot.press("enter")  # a thread hides the logo: the animation pauses
        await pilot.pause()
        hidden = app._step  # the real timer may have ticked before the logo was hidden
        app._shimmer()
        await pilot.pause(0.2)  # a few timer ticks while hidden
        return first, moved, hidden, app._step

    first, moved, hidden, later = drive(app, steps)
    assert moved > first and later == hidden


def test_the_chosen_folder_survives_refreshes_that_change_the_counts(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("0", "j", "j")  # hermes
        await pilot.pause()
        for n in range(3):
            journal.add(f"t-new{n}", 1, "user", "claude", "ask", "action", "more")
            await pilot.press("g")
            await pilot.pause()
        table = app.query_one("#threads", DataTable)
        return app.folder, [str(table.get_row_at(i)[0]) for i in range(table.row_count)]

    assert drive(app, steps) == ("agent:hermes", ["t-a"])


def test_your_own_messages_do_not_light_the_dot_and_stop_keeps_it(journal):
    app = board(journal)

    async def steps(pilot):
        def flag(thread: str) -> str:
            table = app.query_one("#threads", DataTable)
            return str(table.get_row_at(table.get_row_index(thread))[4])

        start = flag("t-a")
        journal.add("t-a", 2, "user", "hermes", "ask", "action", "sent from another terminal")
        await pilot.press("g")
        await pilot.pause()
        own = flag("t-a")
        journal.add("t-a", 2, "hermes", "user", "reply", "action", "news")
        journal.set_stopped("t-a", True)
        await pilot.press("g")
        await pilot.pause()
        return start, own, flag("t-a")

    assert drive(app, steps) == ("", "", "● STOP")


def test_escape_closes_the_agents_view_and_focus_stays_visible(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("enter", "l", "a")  # focus on the log, then the agents view hides it
        await pilot.pause()
        on_agents = app.focused.id
        await pilot.press("escape")
        await pilot.pause()
        return on_agents, shown(app, "#agents"), shown(app, "#log"), app.focused.id

    assert drive(app, steps) == ("agents", False, True, "threads")


def test_escape_in_the_folders_keeps_the_focus_there(journal):
    app = board(journal)

    async def steps(pilot):
        await pilot.press("enter", "0", "escape")
        await pilot.pause()
        return app.current, app.focused.id

    assert drive(app, steps) == (None, "folders")


def test_markup_in_agent_names_is_literal_in_the_folders(agents_file):
    agents_file(CONFIG)
    store = default_store()
    store.add("t-1", 1, "user", "[red]x[/red]", "ask", "action", "hi")
    app = board(store)

    async def steps(pilot):
        table = app.query_one("#folders", DataTable)
        return [str(table.get_row_at(i)[0]) for i in range(table.row_count)], app.is_running

    labels, running = drive(app, steps)
    assert running and "[red]x[/red]" in labels
