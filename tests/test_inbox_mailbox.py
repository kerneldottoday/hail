"""Inbox: notes an agent drops in a mailbox folder, and a clear that survives the agent pruning its reports."""

import json
import os
import subprocess
import sys

import pytest

from hail import feeds, jobs_inbox
from hail.config import parse_agent
from hail.store import Store


@pytest.fixture
def hermes_box(tmp_path):
    home = tmp_path / "hermes"
    (home / "cron" / "output" / "job1").mkdir(parents=True)
    (home / "cron" / "jobs.json").write_text('{"jobs": [{"id": "job1", "name": "Passe 3"}]}', encoding="utf-8")
    report = home / "cron" / "output" / "job1" / "2026-10-27_20-03-37.md"
    report.write_text("cron report", encoding="utf-8")
    os.utime(report, (1_761_000_000, 1_761_000_000))
    box = tmp_path / "from-agents" / "hermes"
    (box / "sub").mkdir(parents=True)
    note = box / "2026-10-28-passe-3-finie.md"
    note.write_text("# Passe 3 finie\n\n163 lignes, 0 reference.", encoding="utf-8")
    os.utime(note, (1_762_000_000, 1_762_000_000))
    (box / ".draft.md").write_text("hidden", encoding="utf-8")
    (box / "sub" / "deep.md").write_text("not listed", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    return home, box


def run_script(home, box, *args):
    env = {**os.environ, "HERMES_HOME": str(home), "HAIL_FEED_ARGS": json.dumps(list(args))}
    if box is not None:
        env["HAIL_MAILBOX"] = str(box)
    proc = subprocess.run(
        [sys.executable, "-"], input=feeds.HERMES_FEED.encode(), capture_output=True, env=env, check=False
    )
    return proc.returncode, json.loads(proc.stdout.decode() or "null")


# --- mailbox in the Hermes feed --------------------------------------------------------


def test_mailbox_notes_are_listed_with_cron_reports(hermes_box):
    code, items = run_script(*hermes_box, "inbox")
    assert code == 0
    assert [i["id"] for i in items] == ["mailbox/2026-10-28-passe-3-finie.md", "job1/2026-10-27_20-03-37.md"]
    note = items[0]
    assert note["job"] == "mailbox" and note["job_name"] == "mailbox"
    assert "0 reference" in note["preview"] and note["created"].endswith("+00:00")


def test_mailbox_read_and_its_limits(hermes_box):
    code, item = run_script(*hermes_box, "read", "mailbox/2026-10-28-passe-3-finie.md")
    assert code == 0 and item["text"].startswith("# Passe 3 finie")
    for bad in ["mailbox/../../secret.txt", "mailbox/sub", "mailbox/nope.md", "mailbox/.draft.md"]:
        code, out = run_script(*hermes_box, "read", bad)
        assert code != 0 and "error" in out, bad


def test_without_mailbox_nothing_changes(hermes_box):
    home, _box = hermes_box
    code, items = run_script(home, None, "inbox")
    assert code == 0 and [i["id"] for i in items] == ["job1/2026-10-27_20-03-37.md"]
    code, out = run_script(home, None, "read", "mailbox/2026-10-28-passe-3-finie.md")
    assert code != 0 and "error" in out


def test_missing_mailbox_folder_is_not_an_error(hermes_box, tmp_path):
    home, _box = hermes_box
    code, items = run_script(home, tmp_path / "none", "inbox")
    assert code == 0 and len(items) == 1


def test_mailbox_setting_reaches_the_feed():
    remote = parse_agent("hermes", {"preset": "hermes", "host": "vps", "mailbox": "~/shared/from-agents/hermes"})
    script = feeds.invocation(remote, ["inbox"]).argv[-1]
    assert 'HAIL_MAILBOX="$HOME/shared/from-agents/hermes"' in script
    local = parse_agent("hermes", {"preset": "hermes", "mailbox": "~/box"})
    assert feeds.invocation(local, ["inbox"]).env["HAIL_MAILBOX"] == os.path.expanduser("~/box")
    plain = parse_agent("hermes", {"preset": "hermes", "host": "vps"})
    assert "HAIL_MAILBOX" not in feeds.invocation(plain, ["inbox"]).argv[-1]


# --- clear survives the feed window sliding ---------------------------------------------


def _item(item_id, created):
    return {"id": item_id, "job": "j", "job_name": "", "created": created, "size": 1, "preview": item_id}


@pytest.fixture
def sliding_feed(agents_file, monkeypatch):
    agents_file('[agents.hermes]\npreset = "hermes"\nhost = "vps"\n')
    window = {"items": [_item("new", "2026-10-27T10:00:00+00:00"), _item("mid", "2026-10-20T10:00:00+00:00")]}
    monkeypatch.setattr(feeds, "run", lambda agent, args: window["items"])
    monkeypatch.setattr(feeds, "resolve_tz", lambda name: None)
    return window


def test_clear_also_covers_older_reports_that_slide_into_the_window(sliding_feed, tmp_path):
    store = Store(tmp_path / "hail.db")
    count, errors = jobs_inbox.clear(None, store)
    assert (count, errors) == (2, [])
    # the agent pruned "new"; an old report never shown before now fits in the window
    sliding_feed["items"] = [_item("mid", "2026-10-20T10:00:00+00:00"), _item("old", "2026-10-01T10:00:00+00:00")]
    rows, _ = jobs_inbox.collect_inbox(jobs_inbox.feed_agents(), store, show_all=False)
    assert rows == []
    sliding_feed["items"].insert(0, _item("fresh", "2026-10-28T10:00:00+00:00"))
    rows, _ = jobs_inbox.collect_inbox(jobs_inbox.feed_agents(), store, show_all=False)
    assert [r["id"] for r in rows] == ["fresh"]


def test_clear_never_moves_the_mark_backwards(sliding_feed, tmp_path):
    store = Store(tmp_path / "hail.db")
    jobs_inbox.clear(None, store)
    sliding_feed["items"] = [_item("old", "2026-10-01T10:00:00+00:00")]
    jobs_inbox.clear(None, store)
    sliding_feed["items"] = [_item("between", "2026-10-25T10:00:00+00:00")]
    rows, _ = jobs_inbox.collect_inbox(jobs_inbox.feed_agents(), store, show_all=False)
    assert rows == []


def test_reading_one_item_does_not_hide_older_unread_ones(sliding_feed, tmp_path):
    store = Store(tmp_path / "hail.db")
    store.mark_seen("hermes", ["new"])  # what `hail inbox read` records
    rows, _ = jobs_inbox.collect_inbox(jobs_inbox.feed_agents(), store, show_all=False)
    assert [r["id"] for r in rows] == ["mid"]


# --- review fixes -------------------------------------------------------------------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


@posix_only
def test_symlinks_out_of_the_folders_are_never_listed_or_previewed(hermes_box, tmp_path):
    home, box = hermes_box
    (box / "leak.md").symlink_to(tmp_path / "secret.txt")
    (home / "cron" / "output" / "job1" / "leak.md").symlink_to(tmp_path / "secret.txt")
    code, items = run_script(home, box, "inbox")
    assert code == 0 and not [i for i in items if "leak" in i["id"] or "nope" in i["preview"]]


def test_a_note_copied_with_an_old_mtime_still_counts_as_new(hermes_box):
    home, box = hermes_box
    old = box / "copied-from-backup.md"
    old.write_text("restored note", encoding="utf-8")
    os.utime(old, (1_700_000_000, 1_700_000_000))  # cp -p / rsync -t keep the old mtime
    code, items = run_script(home, box, "inbox")
    note = next(i for i in items if i["id"] == "mailbox/copied-from-backup.md")
    assert not note["created"].startswith("2023")  # arrival time, not the preserved mtime


def test_busy_crons_do_not_push_mailbox_notes_out(hermes_box):
    home, box = hermes_box
    out = home / "cron" / "output" / "job1"
    for n in range(60):
        (out / f"2026-10-29_{n:02d}.md").write_text("cron", encoding="utf-8")
    code, items = run_script(home, box, "inbox")
    assert "mailbox/2026-10-28-passe-3-finie.md" in [i["id"] for i in items]


def test_a_future_dated_report_does_not_freeze_the_inbox(sliding_feed, tmp_path):
    store = Store(tmp_path / "hail.db")
    sliding_feed["items"] = [_item("skewed", "2099-01-01T00:00:00+00:00"), _item("mid", "2026-10-20T10:00:00+00:00")]
    jobs_inbox.clear(None, store)
    sliding_feed["items"] = [_item("fresh", "2026-10-28T10:00:00+00:00")]
    rows, _ = jobs_inbox.collect_inbox(jobs_inbox.feed_agents(), store, show_all=False)
    assert [r["id"] for r in rows] == ["fresh"]
