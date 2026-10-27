"""v0.3: stale tasks, journal retention and redaction, remote timeout, new presets."""

import sqlite3

import pytest

from hail import tasks
from hail.config import ConfigError, load_config, parse_agent
from hail.hub import Request
from hail.runner import build
from hail.store import Store

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "hail.db")


def _age(store: Store, table: str, column: str, seconds: float) -> None:
    with sqlite3.connect(store.path) as db:
        db.execute(f"UPDATE {table} SET {column} = {column} - ?", (seconds,))


# --- stale background tasks ----------------------------------------------------


def test_reap_stale_marks_old_working_tasks_failed(store):
    store.create_task("k-old", {"agent": "a"})
    store.update_task("k-old", "working")
    store.create_task("k-new", {"agent": "a"})
    _age(store, "tasks", "updated", 7200)
    store.update_task("k-new", "working")
    assert store.reap_stale(3600) == 1
    old = store.get_task("k-old")
    assert old.status == "failed" and "stale" in old.error
    assert store.get_task("k-new").status == "working"
    assert store.count_active_tasks() == 1


def test_submit_reaps_dead_workers_before_counting(store, monkeypatch):
    monkeypatch.setattr(tasks, "spawn", lambda task_id: None)
    for i in range(tasks.MAX_ACTIVE):
        store.create_task(f"k-dead{i}", {"agent": "a"})
    _age(store, "tasks", "updated", tasks.STALE_AFTER + 60)
    task_id, _thread = tasks.submit(store, Request(message="hi", agent="a"), "t-1", "user")
    assert store.get_task(task_id).status == "submitted"
    assert store.count_active_tasks() == 1


# --- journal: WAL, redaction, prune -----------------------------------------------


def test_journal_uses_wal(store):
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_journal_redacts_secrets(store):
    store.add("t-1", 1, "user", "a", "reply", "action", f"key is {SECRET}")
    store.try_ask("t-1", 1, "user", "a", "action", f"password = hunter2 and {SECRET}", 12, 30)
    texts = [m.text for m in store.thread("t-1")]
    assert all(SECRET not in t and "hunter2" not in t for t in texts)
    assert all("[redacted]" in t for t in texts)


def test_prune_removes_old_messages_threads_and_finished_tasks(store):
    store.add("t-old", 1, "user", "a", "ask", "action", "old")
    store.create_task("k-done", {"agent": "a"})
    store.update_task("k-done", "completed", result="r")
    store.create_task("k-live", {"agent": "a"})
    store.update_task("k-live", "working")
    for table, column in (("messages", "ts"), ("threads", "created"), ("tasks", "updated")):
        _age(store, table, column, 40 * 86400)
    store.add("t-new", 1, "user", "a", "ask", "action", "new")
    removed = store.prune(older_than_days=30)
    assert removed == 1
    assert store.thread("t-old") == () and len(store.thread("t-new")) == 1
    assert [t.thread for t in store.threads(10)] == ["t-new"]
    assert store.get_task("k-done") is None
    assert store.get_task("k-live") is not None  # never delete a task that may still be running


def test_prune_rejects_non_positive_days(store):
    with pytest.raises(ValueError):
        store.prune(older_than_days=0)


def test_auto_prune_runs_at_most_once_a_day(store):
    store.add("t-old", 1, "user", "a", "ask", "action", "old")
    _age(store, "messages", "ts", 40 * 86400)
    assert store.auto_prune(30) == 1
    store.add("t-old2", 1, "user", "a", "ask", "action", "old")
    _age(store, "messages", "ts", 40 * 86400)
    assert store.auto_prune(30) == 0  # already pruned today
    assert store.auto_prune(None) == 0


# --- config [journal] ------------------------------------------------------------------


def test_journal_retention_config(agents_file):
    agents_file("[journal]\nretention_days = 45\n")
    assert load_config().retention_days == 45
    agents_file("")
    assert load_config().retention_days is None


@pytest.mark.parametrize("body", ["retention_days = 0", "retention_days = 'x'", "keep = 3"])
def test_journal_config_is_validated(agents_file, body):
    agents_file(f"[journal]\n{body}\n")
    with pytest.raises(ConfigError, match="journal"):
        load_config()


# --- remote timeout -----------------------------------------------------------------------


def test_remote_call_is_bounded_on_the_server():
    agent = parse_agent("c", {"preset": "claude", "host": "vps", "timeout": 90})
    script = build(agent, "hi").argv[-1]
    assert "timeout -k 1 1 true >/dev/null 2>&1 && set -- timeout -k 5 90" in script
    assert 'exec "$@" claude -p' in script


def test_remote_command_preset_is_bounded_too():
    agent = parse_agent("c", {"preset": "command", "host": "vps", "command": "ask-x | tee log"})
    script = build(agent, "m").argv[-1]
    assert "set -- timeout -k 5 180" in script
    assert "exec \"$@\" sh -c 'ask-x | tee log'" in script.replace("'\"'\"'", "'")


# --- presets -------------------------------------------------------------------------------


def test_codex_preset_reads_stdin_and_is_read_only_in_advice():
    agent = parse_agent("x", {"preset": "codex", "bin": "nope-codex"})
    action = build(agent, "hi")
    assert action.argv == ("nope-codex", "exec", "--skip-git-repo-check", "-") and action.stdin == "hi"
    advice = build(agent, "hi", mode="advice")
    assert advice.argv[-2:] == ("--sandbox", "read-only")


def test_gemini_preset_reads_stdin_without_plan_mode():
    inv = build(parse_agent("g", {"preset": "gemini", "bin": "nope-gemini"}), "hi", mode="advice")
    assert inv.argv == ("nope-gemini",) and inv.stdin == "hi"  # plan mode turns into yolo when headless


def test_ollama_preset_uses_model_setting():
    inv = build(parse_agent("o", {"preset": "ollama", "bin": "nope-ollama", "model": "qwen3:4b"}), "hi")
    assert inv.argv == ("nope-ollama", "run", "qwen3:4b") and inv.stdin == "hi"
    default = build(parse_agent("o", {"preset": "ollama", "bin": "nope-ollama"}), "hi")
    assert default.argv[-1] == "llama3.2"


def test_prune_command(agents_file, capsys):
    from hail.cli import main

    agents_file("")
    assert main(["prune", "--days", "30"]) == 0
    assert "removed 0" in capsys.readouterr().out
    assert main(["prune", "--days", "x"]) == 1


# --- review fixes -----------------------------------------------------------------------------

ESC = "\x1b]0;pwned\x07\x1b[2J"


def test_listings_are_labelled_untrusted_and_stripped_of_control_chars():
    from hail.jobs_inbox import UNTRUSTED, render_inbox, render_jobs

    inbox = render_inbox([{"agent": "h", "id": "a1", "seen": False, "preview": f"hi{ESC} there"}], None)
    jobs = render_jobs([{"agent": "h", "id": "j1", "name": f"n{ESC}", "state": "scheduled"}], None)
    for text in (inbox, jobs):
        assert UNTRUSTED in text and "\x1b" not in text and "\x07" not in text
    assert "hi there" in inbox
    assert UNTRUSTED not in render_inbox([], None)  # nothing to label


def test_read_item_strips_control_chars(monkeypatch, store):
    from hail import jobs_inbox

    agent = parse_agent("h", {"preset": "hermes"})
    monkeypatch.setattr(jobs_inbox, "feed_agents", lambda name=None: [agent])
    monkeypatch.setattr(jobs_inbox.feeds, "run", lambda a, args: {"text": f"line1\n\tline2{ESC}"})
    out = jobs_inbox.read_item("h", "a1", store)
    assert out.endswith("line1\n\tline2") and "\x1b" not in out


def test_task_results_and_errors_are_redacted(store):
    store.create_task("k-1", {"agent": "a"})
    store.update_task("k-1", "failed", result=f"here {SECRET}", error=f"token={SECRET}")
    task = store.get_task("k-1")
    assert SECRET not in task.result and SECRET not in task.error


def test_status_reaps_stale_tasks(store, monkeypatch):
    store.create_task("k-dead", {"agent": "a"})
    store.update_task("k-dead", "working")
    _age(store, "tasks", "updated", tasks.STALE_AFTER + 60)
    assert tasks.status(store, "k-dead").status == "failed"
    assert tasks.status(store, "k-nope") is None


def test_late_completion_still_wins_over_stale(store):
    store.create_task("k-slow", {"agent": "a"})
    store.update_task("k-slow", "working")
    _age(store, "tasks", "updated", 7200)
    store.reap_stale(3600)
    store.update_task("k-slow", "completed", result="done")  # the worker was alive after all
    assert store.get_task("k-slow").status == "completed"


def test_remote_timeout_is_probed_not_just_found():
    script = build(parse_agent("c", {"preset": "claude", "host": "vps"}), "hi").argv[-1]
    assert "timeout -k 1 1 true >/dev/null 2>&1 && set -- timeout -k 5 180" in script


def test_auto_prune_retries_after_a_failure(store, monkeypatch):
    def boom(days):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "prune", boom)
    with pytest.raises(sqlite3.OperationalError):
        store.auto_prune(30)
    assert store.get_meta("last_prune") is None  # next call tries again


def test_prune_command_rejects_odd_digits(agents_file):
    from hail.cli import main

    agents_file("")
    assert main(["prune", "--days", "²"]) == 1
