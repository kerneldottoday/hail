"""hail jobs / hail inbox: read what agents scheduled and delivered, without touching them."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from hail import feeds, jobs_inbox
from hail.config import parse_agent
from hail.mcp import handle_line
from hail.runner import HailError
from hail.store import Store

CET = timezone(timedelta(hours=1), "CET")
CEST = timezone(timedelta(hours=2), "CEST")


# --- the Hermes feed script, run for real against a fake ~/.hermes -----------------


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / "hermes"
    cron = home / "cron"
    (cron / "output" / "job2").mkdir(parents=True)
    jobs = {
        "jobs": [
            {
                "id": "job1",
                "name": "Passe 2",
                "enabled": True,
                "state": "scheduled",
                "schedule": {"kind": "once", "display": "once at 2026-10-27 12:00"},
                "next_run_at": "2026-10-27T12:00:00+00:00",
                "last_run_at": None,
                "last_status": None,
            },
            {
                "id": "job2",
                "name": "Weekly scan",
                "enabled": True,
                "state": "scheduled",
                "schedule_display": "every 10080m",
                "next_run_at": "2026-10-28T19:26:06.107160+00:00",
                "last_run_at": "2026-10-21T19:26:06+00:00",
                "last_status": "ok",
            },
            {"id": "job3", "name": "Old", "enabled": False, "state": "paused", "next_run_at": None},
        ]
    }
    (cron / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    old = cron / "output" / "job1_20261010_110236.txt"
    old.write_text("old report", encoding="utf-8")
    os.utime(old, (1_760_000_000, 1_760_000_000))
    new = cron / "output" / "job2" / "2026-10-21_11-01-29.md"
    new.write_text("# Weekly\n\nAll good, 3 skills found.", encoding="utf-8")
    os.utime(new, (1_761_000_000, 1_761_000_000))
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    return home


def run_script(home, *args):
    env = {**os.environ, "HERMES_HOME": str(home), "HAIL_FEED_ARGS": json.dumps(list(args))}
    proc = subprocess.run(
        [sys.executable, "-"], input=feeds.HERMES_FEED.encode(), capture_output=True, env=env, check=False
    )
    return proc.returncode, json.loads(proc.stdout.decode() or "null")


def test_hermes_script_lists_jobs(hermes_home):
    code, jobs = run_script(hermes_home, "jobs")
    assert code == 0
    first = jobs[0]
    assert first["id"] == "job1" and first["name"] == "Passe 2"
    assert first["schedule"] == "once at 2026-10-27 12:00"
    assert first["next_run"] == "2026-10-27T12:00:00+00:00"
    assert jobs[1]["schedule"] == "every 10080m" and jobs[1]["last_status"] == "ok"
    assert jobs[2]["state"] == "paused"


def test_hermes_script_lists_outputs_newest_first(hermes_home):
    code, items = run_script(hermes_home, "inbox")
    assert code == 0
    assert [i["id"] for i in items] == ["job2/2026-10-21_11-01-29.md", "job1_20261010_110236.txt"]
    assert items[0]["job"] == "job2" and items[0]["job_name"] == "Weekly scan"
    assert items[1]["job"] == "job1" and items[1]["job_name"] == "Passe 2"
    assert "3 skills found" in items[0]["preview"]
    assert items[0]["created"].endswith("+00:00")


def test_hermes_script_reads_one_output(hermes_home):
    code, item = run_script(hermes_home, "read", "job2/2026-10-21_11-01-29.md")
    assert code == 0 and item["text"].startswith("# Weekly")


@pytest.mark.parametrize("bad", ["../../secret.txt", "/etc/passwd", "job2", "nope.md"])
def test_hermes_script_refuses_paths_outside_outputs(hermes_home, bad):
    code, out = run_script(hermes_home, "read", bad)
    assert code != 0 and "error" in out


def test_hermes_script_without_cron_dir(tmp_path):
    code, jobs = run_script(tmp_path / "none", "jobs")
    assert code == 0 and jobs == []
    assert run_script(tmp_path / "none", "inbox") == (0, [])


# --- how hail calls a feed -----------------------------------------------------------


def test_remote_hermes_feed_is_sent_on_stdin():
    agent = parse_agent("hermes", {"preset": "hermes", "host": "vps", "path": ["~/bin"]})
    inv = feeds.invocation(agent, ["read", "a/b.md"])
    assert inv.argv[0] == "ssh" and inv.argv[-2] == "vps"
    script = inv.argv[-1]
    assert "HAIL_FEED_ARGS=" in script and "python3 -" in script and '"$HOME/bin"' in script
    assert "a/b.md" in script  # quoted inside the export, never as a bare argument
    assert inv.stdin == feeds.HERMES_FEED


def test_local_hermes_feed_uses_this_python(monkeypatch):
    agent = parse_agent("hermes", {"preset": "hermes", "hermes_home": "~/h"})
    inv = feeds.invocation(agent, ["jobs"])
    assert inv.argv == (sys.executable, "-")
    assert json.loads(inv.env["HAIL_FEED_ARGS"]) == ["jobs"]
    assert inv.env["HERMES_HOME"] == os.path.expanduser("~/h")


def test_feed_command_for_any_agent():
    agent = parse_agent("bot", {"preset": "command", "command": "bot", "feed_command": "bot-feed"})
    assert feeds.has_feed(agent)
    inv = feeds.invocation(agent, ["jobs"])
    assert inv.argv == "bot-feed" and inv.shell and json.loads(inv.env["HAIL_FEED_ARGS"]) == ["jobs"]
    assert not feeds.has_feed(parse_agent("plain", {"preset": "command", "command": "x"}))
    assert not feeds.has_feed(parse_agent("c", {"preset": "claude"}))


def test_run_parses_json_and_reports_errors(fake_run):
    _calls, state = fake_run
    agent = parse_agent("hermes", {"preset": "hermes", "host": "vps"})
    state["proc"] = type(state["proc"])(stdout=b'[{"id": "x"}]')
    assert feeds.run(agent, ["jobs"]) == [{"id": "x"}]
    state["proc"] = type(state["proc"])(stdout=b'{"error": "unknown item"}', returncode=2)
    with pytest.raises(HailError, match="unknown item"):
        feeds.run(agent, ["read", "x"])
    state["proc"] = type(state["proc"])(stdout=b"not json", stderr=b"python3: not found", returncode=127)
    with pytest.raises(HailError, match="python3: not found"):
        feeds.run(agent, ["jobs"])


def test_run_timeout(fake_run):
    _calls, state = fake_run
    state["proc"] = subprocess.TimeoutExpired("ssh", 1)
    with pytest.raises(HailError, match="did not answer"):
        feeds.run(parse_agent("hermes", {"preset": "hermes", "host": "vps"}), ["jobs"])


@pytest.mark.parametrize("bad", ["", "../x", "a/../../b", "a\nb", "-rf", "x" * 300])
def test_item_ids_are_validated(bad):
    with pytest.raises(HailError):
        feeds.check_item_id(bad)


# --- times: UTC and local, the 13h/14h trap ------------------------------------------


def test_when_shows_utc_and_local_time():
    winter = feeds.parse_time("2026-10-27T12:00:00+00:00")
    assert feeds.format_when(winter, CET) == "2026-10-27 12:00 UTC (13:00 CET)"
    summer = feeds.parse_time("2026-10-20T12:00:00+00:00")
    assert feeds.format_when(summer, CEST) == "2026-10-20 12:00 UTC (14:00 CEST)"
    late = feeds.parse_time("2026-10-27T23:30:00Z")
    assert feeds.format_when(late, CET) == "2026-10-27 23:30 UTC (2026-10-28 00:30 CET)"
    assert feeds.format_when(None, CET) == "-"


def test_parse_time_rejects_garbage():
    assert feeds.parse_time("soon") is None
    assert feeds.parse_time(None) is None
    naive = feeds.parse_time("2026-10-27T12:00:00")
    assert naive is not None and naive.tzinfo is not None  # naive = UTC


def test_unknown_timezone_is_a_clear_error():
    with pytest.raises(HailError, match="tzdata"):
        feeds.resolve_tz("Mars/Olympus")
    assert feeds.resolve_tz(None) is None


# --- the seen list --------------------------------------------------------------------


def test_store_remembers_seen_items(tmp_path):
    store = Store(tmp_path / "h.db")
    assert store.seen_items("hermes") == set()
    store.mark_seen("hermes", ["a.md", "b.md"])
    store.mark_seen("hermes", ["a.md"])
    assert store.seen_items("hermes") == {"a.md", "b.md"}
    assert store.seen_items("other") == set()


# --- CLI ------------------------------------------------------------------------------

JOBS = [
    {
        "id": "j1",
        "name": "Passe 2",
        "state": "scheduled",
        "enabled": True,
        "schedule": "once",
        "next_run": "2026-10-27T12:00:00+00:00",
        "last_run": None,
        "last_status": None,
    },
    {
        "id": "j2",
        "name": "Paused one",
        "state": "paused",
        "enabled": False,
        "schedule": "daily",
        "next_run": "2026-10-04T08:00:00+00:00",
        "last_run": None,
        "last_status": "ok",
    },
]
ITEMS = [
    {
        "id": "j1/2026-10-27_12-05-00.md",
        "job": "j1",
        "job_name": "Passe 2",
        "created": "2026-10-27T12:05:00+00:00",
        "size": 120,
        "preview": "Diametres: 28 mm",
    },
    {
        "id": "j0_20261001_100000.txt",
        "job": "j0",
        "job_name": "",
        "created": "2026-10-01T10:00:00+00:00",
        "size": 10,
        "preview": "old",
    },
]


@pytest.fixture
def hermes_agents(agents_file, monkeypatch):
    agents_file('[agents.hermes]\npreset = "hermes"\nhost = "vps"\n[agents.moi]\npreset = "command"\ncommand = "x"\n')
    calls = []

    def fake(agent, args):
        calls.append((agent.name, list(args)))
        if args[0] == "jobs":
            return JOBS
        if args[0] == "inbox":
            return ITEMS
        return {"id": args[1], "text": "full report text"}

    monkeypatch.setattr(feeds, "run", fake)
    monkeypatch.setattr(feeds, "resolve_tz", lambda name: CET)
    return calls


def test_cmd_jobs_shows_active_jobs_with_both_times(hermes_agents, capsys):
    assert jobs_inbox.cmd_jobs([]) == 0
    out = capsys.readouterr().out
    assert "Passe 2" in out and "2026-10-27 12:00 UTC (13:00 CET)" in out
    assert "Paused one" not in out
    assert hermes_agents == [("hermes", ["jobs"])]  # agents without a feed are skipped


def test_cmd_jobs_all_and_json(hermes_agents, capsys):
    assert jobs_inbox.cmd_jobs(["--all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [j["id"] for j in data["jobs"]] == ["j1", "j2"] and data["jobs"][0]["agent"] == "hermes"


def test_cmd_inbox_lists_new_then_read_marks_seen(hermes_agents, capsys):
    assert jobs_inbox.cmd_inbox([]) == 0
    out = capsys.readouterr().out
    assert "2 new" in out and "Diametres: 28 mm" in out
    assert jobs_inbox.cmd_inbox(["read", "hermes", "j1/2026-10-27_12-05-00.md"]) == 0
    out = capsys.readouterr().out
    assert "full report text" in out and "untrusted" in out.lower()
    assert jobs_inbox.cmd_inbox([]) == 0
    out = capsys.readouterr().out
    assert "1 new" in out and "Diametres" not in out


def test_cmd_inbox_clear_and_all(hermes_agents, capsys):
    assert jobs_inbox.cmd_inbox(["clear"]) == 0
    capsys.readouterr()
    assert jobs_inbox.cmd_inbox([]) == 0
    assert "nothing new" in capsys.readouterr().out
    assert jobs_inbox.cmd_inbox(["--all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["items"]) == 2 and all(i["seen"] for i in data["items"])


def test_cmd_inbox_unknown_agent(hermes_agents):
    with pytest.raises(HailError, match="no feed"):
        jobs_inbox.cmd_inbox(["read", "moi", "x.md"])


def test_one_failing_agent_does_not_hide_the_others(agents_file, monkeypatch, capsys):
    agents_file('[agents.a]\npreset = "hermes"\nhost = "vps"\n[agents.b]\npreset = "hermes"\nhost = "vps2"\n')

    def fake(agent, args):
        if agent.name == "a":
            raise HailError("ssh: connect to host vps port 22: timed out")
        return JOBS

    monkeypatch.setattr(feeds, "run", fake)
    monkeypatch.setattr(feeds, "resolve_tz", lambda name: CET)
    assert jobs_inbox.cmd_jobs([]) == 1
    captured = capsys.readouterr()
    assert "Passe 2" in captured.out and "timed out" in captured.err


# --- MCP ------------------------------------------------------------------------------


def call(name, arguments):
    line = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    return handle_line(line)["result"]


def test_mcp_jobs_inbox_and_read(hermes_agents):
    text = call("hail_jobs", {})["content"][0]["text"]
    assert "Passe 2" in text and "13:00 CET" in text
    text = call("hail_inbox", {})["content"][0]["text"]
    assert "2 new" in text
    result = call("hail_inbox_read", {"agent": "hermes", "id": "j1/2026-10-27_12-05-00.md"})
    assert "full report text" in result["content"][0]["text"] and not result["isError"]
    bad = call("hail_inbox_read", {"agent": "hermes", "id": "../x"})
    assert bad["isError"]


def test_format_when_default_is_local_time():
    dt = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    text = feeds.format_when(dt, None)
    assert text.startswith("2026-10-27 12:00 UTC (")


def test_long_windows_zone_names_become_offsets():
    long_name = timezone(timedelta(hours=2), "Paris, Madrid (heure d'été)")
    dt = datetime(2026, 10, 24, 11, 1, tzinfo=timezone.utc)
    assert feeds.format_when(dt, long_name) == "2026-10-24 11:01 UTC (13:01 UTC+2)"
    india = timezone(timedelta(hours=5, minutes=30), "India Standard Time")
    assert feeds.format_when(dt, india).endswith("(16:31 UTC+5:30)")
