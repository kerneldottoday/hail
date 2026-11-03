"""`hail tui`: watch and steer agent conversations from the keyboard (needs the 'tui' extra: Textual).

Three numbered panes: [0] folders (all, one per agent, stopped, today), [1] threads, [2] the open thread
(or the shimmering logo when none is open). A status bar shows the mode (NORMAL / INSERT) and the keys.

Reads the journal (~/.hail/hail.db) and the agents file (once, at start); never logs in anywhere. A reply
goes out as a background task, so the screen never waits for an agent. Everything that comes from the
journal is shown as plain text (rich Text), never parsed as markup.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Input, RichLog, Static

from . import board, tui_logo, tui_state
from .config import Config
from .errors import HailError
from .store import Store

Submit = Callable[[str, str, str], str]  # (agent, thread, message) -> task id
REFRESH_SECONDS = 3.0
SHIMMER_SECONDS = 0.08
THREAD_LIMIT = 50
LOG_MIN_WIDTH = 20
KIND_STYLE = {"ask": "bold", "reply": "", "error": "red", "refused": "yellow", "human": "cyan", "council": "bold"}
JOURNAL_ERRORS = (sqlite3.Error, OSError)
KEYS_NORMAL = "h/l pane · j/k move · ⏎ open · r reply · s stop · a agents · esc close · q quit"
KEYS_INSERT = "⏎ send · esc cancel"


class HailBoard(App[None]):
    TITLE = "hail"
    CSS = """
    #panes { height: 1fr; }
    .pane { border: round $panel; border-title-color: $text-muted; }
    .pane:focus-within { border: round $accent; border-title-color: $accent; border-title-style: bold; }
    #pane0 { width: 24; }
    #pane1 { width: 52; }
    #pane2 { width: 1fr; }
    #folders, #threads, #log, #agents { height: 1fr; border: none; }
    #logo { width: 1fr; height: 1fr; content-align: center middle; }
    #reply { height: 3; }
    #bar { height: 1; }
    #mode { width: auto; padding: 0 1; background: $accent; color: $text; text-style: bold; }
    #mode.insert { background: $warning; }
    #keys { width: 1fr; padding: 0 1; color: $text-muted; }
    #status { width: auto; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("j", "move(1)", "Down"),
        ("k", "move(-1)", "Up"),
        ("h", "pane(-1)", "Left pane"),
        ("l", "pane(1)", "Right pane"),
        ("0", "focus_pane(0)", "Folders"),
        ("1", "focus_pane(1)", "Threads"),
        ("2", "focus_pane(2)", "Thread"),
        ("r", "reply", "Reply"),
        ("i", "reply", "Reply"),
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
        self.current: str | None = None  # the open thread; None shows the logo
        self.folder = board.ALL
        self._view: tuple[tuple[board.ThreadRow, str], ...] = ()  # rows shown in [1], with their flag
        self._folders: tuple[board.FolderRow, ...] = ()
        self._agents: tuple[board.AgentRow, ...] = ()
        self._seen: tui_state.Seen = {}  # thread -> agent message count when last seen (unread dots)
        self._baseline = False  # threads present at start are not "unread"
        self._shown: tuple[str, int] | None = None  # (thread, last message id) in the log
        self._reply_to: tuple[str, str] | None = None  # (thread, agent) fixed when 'r' was pressed
        self._show_agents = False
        # set while a table is rebuilt, so its highlight events are not taken as your choice
        self._refilling_folders = False
        self._refilling_threads = False
        self._step = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="panes"):
            with Vertical(id="pane0", classes="pane"):
                yield DataTable(id="folders", cursor_type="row", show_header=False)
            with Vertical(id="pane1", classes="pane"):
                yield DataTable(id="threads", cursor_type="row", zebra_stripes=True)
            with Vertical(id="pane2", classes="pane"):
                yield Static(tui_logo.frame(0), id="logo")
                # min_width: wrap to the pane, not to RichLog's default 78 columns
                yield RichLog(id="log", wrap=True, markup=False, highlight=False, min_width=LOG_MIN_WIDTH)
                yield DataTable(id="agents", cursor_type="row")
        yield Input(placeholder="reply (enter to send, esc to cancel)", id="reply")
        with Horizontal(id="bar"):
            yield Static("NORMAL", id="mode")
            yield Static(KEYS_NORMAL, id="keys")
            yield Static("", id="status")

    def on_mount(self) -> None:
        self.query_one("#pane0").border_title = "[0] Folders"
        self.query_one("#pane1").border_title = "[1] Threads"
        self.query_one("#folders", DataTable).add_columns("folder", "n")
        self.query_one("#threads", DataTable).add_columns("thread", "agents", "last", "msgs", "")
        self.query_one("#agents", DataTable).add_columns("agent", "preset", "where", "advice", "today", "skills")
        self.query_one("#reply", Input).display = False
        self.action_reload()
        self._refresh_pane2()
        self.query_one("#threads", DataTable).focus()
        self.set_interval(REFRESH_SECONDS, self.action_reload)
        self.set_interval(SHIMMER_SECONDS, self._shimmer)

    # data ------------------------------------------------------------------------------

    def action_reload(self) -> None:
        try:
            rows = board.thread_rows(self.store, THREAD_LIMIT, keep=self.current)
            self._fill_folders(rows)
            self._fill_threads(rows)
            self._fill_agents()
            if self.current:
                self._show(self.current)
        except JOURNAL_ERRORS as exc:  # a busy or missing journal must not close the screen
            self._status(f"journal unavailable: {exc} (retrying)")

    def _fill_folders(self, rows: Sequence[board.ThreadRow]) -> None:
        folders = board.folder_rows(rows, tuple(self.config.agents), time.time(), keep=self.folder)
        if self.folder not in {f.key for f in folders}:
            self.folder = board.ALL
        if folders == self._folders:
            return
        self._folders = folders
        table = self.query_one("#folders", DataTable)
        self._refilling_folders = True
        table.clear()
        for f in folders:
            table.add_row(Text(f.label), Text(f"[{f.count}]" if f.count else "", justify="right"), key=f.key)
        table.move_cursor(row=table.get_row_index(self.folder))
        self.call_after_refresh(self._folders_refilled)

    def _fill_threads(self, rows: Sequence[board.ThreadRow]) -> None:
        self._seen = tui_state.mark_seen(self._seen, rows, self.current, first=not self._baseline)
        self._baseline = True
        now = time.time()
        view = tuple((r, tui_state.flag(r, self._seen)) for r in rows if board.in_folder(r, self.folder, now))
        if view != self._view:
            self._view = view
            self._refilling_threads = True
            table = self.query_one("#threads", DataTable)
            table.clear()
            for r, flag in view:
                cells = (r.thread, r.agents, r.last, str(r.messages))
                style = "green" if tui_state.UNREAD in flag else "yellow"
                table.add_row(*(Text(c) for c in cells), Text(flag, style=style), key=r.thread)
            if self.current in {r.thread for r, _ in view}:
                table.move_cursor(row=table.get_row_index(self.current))
            self.call_after_refresh(self._threads_refilled)
        self._status(self._count_text(rows, view))

    @staticmethod
    def _count_text(rows: Sequence[board.ThreadRow], view: Sequence[object]) -> str:
        if not rows:
            return 'no threads yet: ask an agent with hail <agent> "..."'
        return f"{len(view)} thread(s)" if view else "no thread in this folder"

    def _folders_refilled(self) -> None:
        self._refilling_folders = False

    def _threads_refilled(self) -> None:
        self._refilling_threads = False

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

    def _refresh_pane2(self) -> None:
        """[2] shows the agents table, the open thread, or the logo, in that order of priority."""
        agents, log, logo = (self.query_one(i) for i in ("#agents", "#log", "#logo"))
        agents.display = self._show_agents
        log.display = not self._show_agents and self.current is not None
        logo.display = not self._show_agents and self.current is None
        title = "agents" if self._show_agents else (escape(self.current) if self.current else "(no thread)")
        self.query_one("#pane2").border_title = f"[2] {title}"  # a thread id is never markup

    def _shimmer(self) -> None:
        found = self.query("#logo")  # the timer can fire once more while the screen closes
        if not found:
            return
        logo = found.first(Static)
        if logo.display:
            self._step += 1
            logo.update(tui_logo.frame(self._step))

    def _set_mode(self, insert: bool) -> None:
        mode = self.query_one("#mode", Static)
        mode.update("INSERT" if insert else "NORMAL")
        mode.set_class(insert, "insert")
        self.query_one("#keys", Static).update(KEYS_INSERT if insert else KEYS_NORMAL)

    # events and actions --------------------------------------------------------------------

    @on(DataTable.RowHighlighted, "#folders")
    def _folder_picked(self, event: DataTable.RowHighlighted) -> None:
        picked = event.row_key.value
        if self._refilling_folders or not picked or picked == self.folder:
            return
        if event.cursor_row != event.data_table.cursor_row:  # a late event from a rebuild, not your choice
            return
        self.folder = str(picked)
        self.action_reload()

    @on(DataTable.RowSelected, "#folders")
    def _folder_chosen(self, event: DataTable.RowSelected) -> None:
        self.query_one("#threads", DataTable).focus()

    @on(DataTable.RowSelected, "#threads")
    def _thread_chosen(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.open_thread(str(event.row_key.value))

    def open_thread(self, thread: str) -> None:
        self.current = thread
        self._show_agents = False
        self._shown = None  # rewrite from the start once the log is visible and has its real width
        self._refresh_pane2()
        self.call_after_refresh(self.action_reload)  # writes the thread, clears its unread dot

    def _panes(self) -> tuple[Widget, ...]:
        """The focusable widget of each pane, left to right ([2] has none while it shows the logo)."""
        pane2 = "#agents" if self._show_agents else "#log"
        right = (self.query_one(pane2),) if self._show_agents or self.current else ()
        return (self.query_one("#folders"), self.query_one("#threads"), *right)

    def action_focus_pane(self, index: int) -> None:
        panes = self._panes()
        if index < len(panes):
            panes[index].focus()

    def action_pane(self, step: int) -> None:
        panes = self._panes()
        here = panes.index(self.focused) if self.focused in panes else 1
        panes[max(0, min(len(panes) - 1, here + step))].focus()

    def action_move(self, step: int) -> None:
        if self._show_agents:
            table = self.query_one("#agents", DataTable)
        elif self.focused is self.query_one("#log"):
            self.query_one("#log", RichLog).scroll_relative(y=step)
            return
        elif self.focused is self.query_one("#folders"):
            table = self.query_one("#folders", DataTable)
        else:
            table = self.query_one("#threads", DataTable)
        table.move_cursor(row=max(0, table.cursor_row + step))

    def action_toggle_agents(self) -> None:
        self._show_agents = not self._show_agents
        self._refresh_pane2()
        self._keep_focus_visible()

    def _keep_focus_visible(self) -> None:
        """If the focused widget was just hidden, move to what [2] shows now, else to the threads."""
        if self.focused is not None and self.focused.display:
            return
        target = "#agents" if self._show_agents else ("#log" if self.current else "#threads")
        self.query_one(target).focus()

    def action_toggle_stop(self) -> None:
        if not self.current:
            self._status("open a thread first (enter)")
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
            self._status("nothing to reply to: open a thread where an agent was asked")
            return
        self._reply_to = (self.current, target)
        box = self.query_one("#reply", Input)
        box.placeholder = f"reply to {target} in {self.current} (enter to send, esc to cancel)"
        box.display = True
        box.focus()
        self._set_mode(True)

    def action_cancel(self) -> None:
        box = self.query_one("#reply", Input)
        if box.display:
            box.value = ""
            box.display = False
            self._set_mode(False)
        elif self._show_agents:
            self._show_agents = False
        else:  # close the open thread: back to the logo
            self.current, self._shown = None, None
        self._refresh_pane2()
        if self.focused is None or not self.focused.display or self.focused is box:
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
