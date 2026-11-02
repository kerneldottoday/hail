"""`hail tui`: watch and steer agent conversations from the keyboard (needs the 'tui' extra: Textual).

Reads the journal (~/.hail/hail.db) and the agents file (once, at start); never logs in anywhere. A reply
goes out as a background task, so the screen never waits for an agent. Everything that comes from the
journal is shown as plain text (rich Text), never parsed as markup.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from . import board
from .config import Config
from .errors import HailError
from .store import Store

Submit = Callable[[str, str, str], str]  # (agent, thread, message) -> task id
REFRESH_SECONDS = 3.0
THREAD_LIMIT = 50
KIND_STYLE = {"ask": "bold", "reply": "", "error": "red", "refused": "yellow", "human": "cyan", "council": "bold"}
JOURNAL_ERRORS = (sqlite3.Error, OSError)


class HailBoard(App[None]):
    TITLE = "hail"
    SUB_TITLE = "threads between your agents"
    CSS = """
    #threads { width: 56; }
    #side { width: 1fr; }
    #log { height: 1fr; border: round $panel; }
    #agents { height: 1fr; }
    #status { height: 1; color: $text-muted; }
    #reply { dock: bottom; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("j", "move(1)", "Down"),
        ("k", "move(-1)", "Up"),
        ("r", "reply", "Reply"),
        ("s", "toggle_stop", "Stop/resume"),
        ("a", "toggle_agents", "Agents"),
        ("g", "reload", "Refresh"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, store: Store, config: Config, submit: Submit) -> None:
        super().__init__()
        self.store = store
        self.config = config
        self.submit = submit
        self.current: str | None = None
        self._threads: tuple[board.ThreadRow, ...] = ()
        self._agents: tuple[board.AgentRow, ...] = ()
        self._shown: tuple[str, int] | None = None  # (thread, last message id) in the log
        self._reply_to: tuple[str, str] | None = None  # (thread, agent) fixed when 'r' was pressed
        self._refilling = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="threads", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield RichLog(id="log", wrap=True, markup=False, highlight=False)
                yield DataTable(id="agents", cursor_type="row")
                yield Static("", id="status")
        yield Input(placeholder="reply (enter to send, esc to cancel)", id="reply")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#threads", DataTable).add_columns("thread", "agents", "last", "msgs", "")
        agents = self.query_one("#agents", DataTable)
        agents.add_columns("agent", "preset", "where", "advice", "today", "skills")
        agents.display = False
        self.query_one("#reply", Input).display = False
        self.action_reload()
        self.query_one("#threads", DataTable).focus()
        self.set_interval(REFRESH_SECONDS, self.action_reload)

    # data ------------------------------------------------------------------------------

    def action_reload(self) -> None:
        try:
            self._fill_threads()
            self._fill_agents()
            if self.current:
                self._show(self.current)
        except JOURNAL_ERRORS as exc:  # a busy or missing journal must not close the screen
            self._status(f"journal unavailable: {exc} (retrying)")

    def _fill_threads(self) -> None:
        rows = board.thread_rows(self.store, THREAD_LIMIT, keep=self.current)
        if self.current is None and rows:
            self.current = rows[0].thread
        if rows != self._threads:
            self._threads = rows
            self._refilling = True  # highlight events of the rebuild must not change the open thread
            table = self.query_one("#threads", DataTable)
            table.clear()
            for r in rows:
                cells = (r.thread, r.agents, r.last, str(r.messages), "STOP" if r.stopped else "")
                table.add_row(*(Text(c) for c in cells), key=r.thread)
            if self.current in {r.thread for r in rows}:
                table.move_cursor(row=table.get_row_index(self.current))
            self.call_after_refresh(self._refilled)
        self._status(f"{len(rows)} thread(s)" if rows else 'no threads yet: ask an agent with hail <agent> "..."')

    def _refilled(self) -> None:
        self._refilling = False

    def _fill_agents(self) -> None:
        rows = board.agent_rows(self.config, self.store)
        if rows == self._agents:
            return
        self._agents = rows
        table = self.query_one("#agents", DataTable)
        keep = table.cursor_row
        table.clear()
        for a in rows:
            today = f"{a.calls_today}/{a.daily_cap}" if a.daily_cap else str(a.calls_today)
            advice = "enforced" if a.advice_enforced else "by prompt"
            table.add_row(*(Text(c) for c in (a.name, a.preset, a.where, advice, today, a.skills)), key=a.name)
        table.move_cursor(row=min(keep, max(0, len(rows) - 1)))

    def _show(self, thread: str) -> None:
        """Write the thread, or only what arrived since it was last shown (so the scroll stays put)."""
        log = self.query_one("#log", RichLog)
        after = self._shown[1] if self._shown and self._shown[0] == thread else 0
        lines = board.message_lines(self.store, thread, after)
        if after == 0:
            log.clear()
            log.border_title = escape(thread)  # a thread id is never markup
        elif not lines:
            return
        self._write(log, lines)
        self._shown = (thread, lines[-1].id if lines else after)

    @staticmethod
    def _write(log: RichLog, lines: Sequence[board.Line]) -> None:
        for line in lines:
            head = f"{line.when} h{line.hop} {line.kind:<7} {line.who} [{line.mode}]"
            log.write(Text(head, style=KIND_STYLE.get(line.kind, "")))
            body = Text(line.text, style="italic" if line.untrusted else "")
            log.write(Text("untrusted · ", style="dim") + body if line.untrusted else body)

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    # events and actions --------------------------------------------------------------------

    @on(DataTable.RowHighlighted, "#threads")
    def _picked(self, event: DataTable.RowHighlighted) -> None:
        picked = event.row_key.value
        if self._refilling or not picked or picked == self.current:
            return
        self.current = str(picked)
        try:
            self._show(self.current)
        except JOURNAL_ERRORS as exc:
            self._status(f"journal unavailable: {exc}")

    def action_move(self, step: int) -> None:
        table = self.query_one("#agents" if self.query_one("#agents").display else "#threads", DataTable)
        table.move_cursor(row=max(0, table.cursor_row + step))

    def action_toggle_agents(self) -> None:
        agents = self.query_one("#agents", DataTable)
        agents.display = not agents.display
        self.query_one("#log", RichLog).display = not agents.display

    def action_toggle_stop(self) -> None:
        if not self.current:
            return
        try:
            stopped = not self.store.is_stopped(self.current)
            self.store.set_stopped(self.current, stopped)
        except JOURNAL_ERRORS as exc:
            self._status(f"not changed: {exc}")
            return
        self.action_reload()
        self._status(f"thread {self.current} {'stopped' if stopped else 'resumed'}")

    def action_reply(self) -> None:
        try:
            target = board.reply_target(self.store.thread(self.current)) if self.current else None
        except JOURNAL_ERRORS as exc:
            self._status(f"journal unavailable: {exc}")
            return
        if self.current is None or target is None:
            self._status("nothing to reply to: pick a thread where an agent was asked")
            return
        self._reply_to = (self.current, target)
        box = self.query_one("#reply", Input)
        box.placeholder = f"reply to {target} in {self.current} (enter to send, esc to cancel)"
        box.display = True
        box.focus()

    def action_cancel(self) -> None:
        box = self.query_one("#reply", Input)
        box.value = ""
        box.display = False
        self.query_one("#threads", DataTable).focus()

    @on(Input.Submitted, "#reply")
    def _send(self, event: Input.Submitted) -> None:
        message, reply_to = event.value.strip(), self._reply_to
        self._reply_to = None
        self.action_cancel()
        if not message or reply_to is None:
            return
        thread, target = reply_to
        try:
            task_id = self.submit(target, thread, message)
        except (HailError, *JOURNAL_ERRORS) as exc:
            self._status(f"not sent: {exc}")
            return
        self._status(f"task {task_id} sent to {target} (thread {thread}); its reply will appear here")
