"""hail 0.5: documented exit codes, --json everywhere, `hail help agents`, one retry when SSH cannot connect."""

import io
import json
import subprocess

import pytest
from conftest import FakeProc

from hail import cli, runner, tasks
from hail.config import parse_agent
from hail.errors import EXIT
from hail.runner import HailError, Reply, ask

CONFIG = """
[agents.claude]
preset = "claude"
skills = ["code-review"]
description = "local reviewer"

[agents.hermes]
preset = "hermes"
host = "vps"
"""


@pytest.fixture
def fake(agents_file, monkeypatch):
    agents_file(CONFIG)

    def fake_ask(agent, message, env=None, mode="action", drop_env=()):
        return Reply(agent.name, f"ok from {agent.name}", "", 0)

    monkeypatch.setattr("hail.hub.ask", fake_ask)
    monkeypatch.setattr(tasks, "spawn", lambda task_id: None)


@pytest.fixture
def procs(monkeypatch):
    """subprocess.run returns (or raises) the queued results in order."""
    queue: list = []
    calls: list = []

    def run(argv, **kwargs):
        calls.append(argv)
        result = queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("subprocess.run", run)
    return queue, calls


def lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# exit codes -------------------------------------------------------------------


def test_exit_code_table_is_stable():
    assert EXIT == {
        "ok": 0,
        "error": 1,
        "usage": 2,
        "config": 3,
        "agent_error": 4,
        "not_found": 5,
        "agent_unusable": 6,
        "refused": 7,
        "temporary": 75,
    }


def test_no_agents_file_exits_3(capsys):
    assert cli.main(["list"]) == 3
    assert "hail init" in capsys.readouterr().err


def test_unknown_agent_task_thread_exit_5(fake, capsys):
    assert cli.main(["ghost", "hi"]) == 5
    assert cli.main(["status", "k-none"]) == 5
    assert cli.main(["thread", "t-none"]) == 5
    assert cli.main(["ask", "--skill", "cooking", "hi"]) == 5


def test_usage_errors_exit_2(fake, monkeypatch):
    assert cli.main(["log", "-n", "x"]) == 2
    assert cli.main(["status"]) == 2
    assert cli.main(["stop"]) == 2
    assert cli.main(["prune", "--days", "x"]) == 2
    assert cli.main(["claude", "--thread"]) == 2
    assert cli.main(["list", "--nope"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    assert cli.main(["claude"]) == 2  # empty message


def test_policy_refusal_exits_7(fake, monkeypatch, capsys):
    monkeypatch.setenv("HAIL_SELF", "claude")
    monkeypatch.setenv("HAIL_THREAD", "t-1")
    monkeypatch.setenv("HAIL_HOP", "1")
    assert cli.main(["hermes", "--mode", "action", "do it"]) == 7
    assert "action" in capsys.readouterr().err


def test_stopped_thread_exits_7(fake, capsys):
    assert cli.main(["claude", "--thread", "t-s", "first"]) == 0
    assert cli.main(["stop", "t-s"]) == 0
    assert cli.main(["claude", "--thread", "t-s", "again"]) == 7


def test_rate_limit_and_daily_cap_exit_75(agents_file, monkeypatch):
    agents_file(CONFIG + "max_calls_per_day = 1\n\n[policy]\nrate_per_minute = 50\n")
    monkeypatch.setattr("hail.hub.ask", lambda agent, *a, **k: Reply(agent.name, "ok", "", 0))
    assert cli.main(["hermes", "one"]) == 0
    assert cli.main(["hermes", "two"]) == 75  # daily cap: try again later
    agents_file(CONFIG + "\n[policy]\nrate_per_minute = 1\n")
    assert cli.main(["claude", "one"]) == 75  # circuit breaker counts the two asks above


def test_thread_budget_exits_7(agents_file, monkeypatch):
    agents_file(CONFIG + "\n[policy]\nmax_calls_per_thread = 1\n")
    monkeypatch.setattr("hail.hub.ask", lambda agent, *a, **k: Reply(agent.name, "ok", "", 0))
    assert cli.main(["claude", "--thread", "t-b", "one"]) == 0
    assert cli.main(["claude", "--thread", "t-b", "two"]) == 7  # waiting will not help


def test_agent_error_exits_4_and_still_prints_the_reply(agents_file, procs, capsys):
    agents_file(CONFIG)
    queue, _ = procs
    queue.append(FakeProc(b"Traceback: boom", b"", 1))
    assert cli.main(["hermes", "--json", "hi"]) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "boom" in payload["reply"]


def test_timeout_exits_75(agents_file, procs):
    agents_file(CONFIG)
    procs[0].append(subprocess.TimeoutExpired("ssh", 1))
    assert cli.main(["hermes", "hi"]) == 75


def test_command_not_found_exits_6(agents_file, procs):
    agents_file('[agents.loc]\npreset = "command"\ncommand = "nothing-here --stdin"\n')
    procs[0].append(FileNotFoundError(2, "no such file", "nothing-here"))
    assert cli.main(["loc", "hi"]) == 6


def test_empty_reply_exits_6(agents_file, procs):
    agents_file(CONFIG)
    procs[0].append(FakeProc(b"", b"segfault", 139))
    assert cli.main(["hermes", "hi"]) == 6


def test_config_error_exits_3(agents_file):
    agents_file("[agents.bad]\npreset = 'nope'\n")
    assert cli.main(["list"]) == 3


# one retry when SSH could not connect --------------------------------------------


def test_ssh_failure_is_retried_once(procs, monkeypatch):
    queue, calls = procs
    queue.extend([FakeProc(b"", b"ssh: connect to host vps port 22: Connection refused", 255), FakeProc(b"answer")])
    monkeypatch.setattr(runner, "RETRY_PAUSE", 0)
    reply = ask(parse_agent("h", {"preset": "hermes", "host": "vps"}), "x")
    assert reply.text == "answer" and len(calls) == 2


def test_ssh_failure_twice_is_temporary(procs, monkeypatch):
    queue, calls = procs
    queue.extend([FakeProc(b"", b"Connection refused", 255), FakeProc(b"", b"Connection refused", 255)])
    monkeypatch.setattr(runner, "RETRY_PAUSE", 0)
    with pytest.raises(HailError) as info:
        ask(parse_agent("h", {"preset": "hermes", "host": "vps"}), "x")
    assert info.value.exit_code == 75 and "ssh" in str(info.value) and len(calls) == 2


def test_no_retry_for_local_agents_or_slow_failures(procs, monkeypatch):
    queue, calls = procs
    queue.append(FakeProc(b"", b"x", 255))
    with pytest.raises(HailError):
        ask(parse_agent("l", {"preset": "command", "command": "x"}), "x")
    assert len(calls) == 1
    clock = iter([0.0, 30.0])  # the failed attempt took 30 s: the agent may have run, do not resend
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    queue.append(FakeProc(b"", b"Connection refused", 255))
    with pytest.raises(HailError):
        ask(parse_agent("h", {"preset": "hermes", "host": "vps"}), "x")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "stderr",
    [b"Connection reset by 1.2.3.4 port 22", b"client_loop: send disconnect: Broken pipe", b"", b"agent crashed"],
)
def test_no_retry_when_the_connection_may_have_reached_the_agent(procs, monkeypatch, stderr):
    queue, calls = procs
    queue.append(FakeProc(b"", stderr, 255))  # dropped mid-run, or the agent itself exited 255
    monkeypatch.setattr(runner, "RETRY_PAUSE", 0)
    with pytest.raises(HailError):
        ask(parse_agent("h", {"preset": "hermes", "host": "vps"}), "x")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "stderr",
    [
        b"ssh: Could not resolve hostname vps: Name or service not known",
        b"ssh: connect to host vps port 22: Connection timed out",
        b"ssh: connect to host vps port 22: No route to host",
        b"kex_exchange_identification: read: Connection reset by peer",
        b"Connection closed by 1.2.3.4 port 22",
    ],
)
def test_retry_on_failures_before_the_session_started(procs, monkeypatch, stderr):
    queue, calls = procs
    queue.extend([FakeProc(b"", stderr, 255), FakeProc(b"answer")])
    monkeypatch.setattr(runner, "RETRY_PAUSE", 0)
    assert ask(parse_agent("h", {"preset": "hermes", "host": "vps"}), "x").text == "answer"
    assert len(calls) == 2


def test_no_retry_when_the_agent_printed_something(procs):
    queue, calls = procs
    queue.append(FakeProc(b"partial answer", b"", 255))
    reply = ask(parse_agent("h", {"preset": "hermes", "host": "vps"}), "x")
    assert reply.text == "partial answer" and not reply.ok and len(calls) == 1


# --json -----------------------------------------------------------------------


def test_list_json(fake, capsys):
    assert cli.main(["list", "--json"]) == 0
    rows = {r["name"]: r for r in lines(capsys.readouterr().out)}
    assert rows["claude"]["skills"] == ["code-review"] and "host" not in rows["claude"]
    assert rows["claude"]["description"] == "local reviewer"
    assert rows["hermes"]["host"] == "vps" and "skills" not in rows["hermes"]
    assert rows["hermes"]["advice_enforced"] is True


def test_doctor_json(agents_file, procs, capsys):
    agents_file('[agents.hermes]\npreset = "hermes"\nhost = "vps"\n')
    procs[0].append(FakeProc(b"/usr/bin/hermes\n"))
    assert cli.main(["doctor", "--json"]) == 0
    (row,) = lines(capsys.readouterr().out)
    assert row["name"] == "hermes" and row["ok"] is True and "/usr/bin/hermes" in row["detail"]


def test_log_threads_thread_json(fake, capsys):
    assert cli.main(["claude", "--thread", "t-j", "first"]) == 0
    capsys.readouterr()
    assert cli.main(["log", "--json"]) == 0
    msgs = lines(capsys.readouterr().out)
    assert [m["kind"] for m in msgs] == ["ask", "reply"]  # oldest first
    assert msgs[0]["text"] == "first" and msgs[0]["date"].endswith("Z") and "T" in msgs[0]["date"]
    assert cli.main(["threads", "--json"]) == 0
    (t,) = lines(capsys.readouterr().out)
    assert t["thread"] == "t-j" and t["messages"] == 2 and t["stopped"] is False
    assert cli.main(["thread", "t-j", "--json"]) == 0
    full = lines(capsys.readouterr().out)
    assert full[1]["text"] == "ok from claude" and full[1]["sender"] == "claude"


def test_status_json(fake, capsys):
    assert cli.main(["send", "claude", "job"]) == 0
    task_id = capsys.readouterr().out.split()[0]
    assert cli.main(["_task", task_id]) == 0
    assert cli.main(["status", task_id, "--json"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["id"] == task_id and row["status"] == "completed" and row["result"] == "ok from claude"
    assert row["agent"] == "claude" and "error" not in row


def test_send_json(fake, capsys):
    assert cli.main(["send", "claude", "--json", "job"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["id"] and row["thread"].startswith("t-") and row["status"] == "submitted"


def test_council_json(fake, capsys):
    assert cli.main(["council", "--json", "--agents", "claude,hermes", "--synth", "claude", "which?"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert [a["agent"] for a in row["answers"]] == ["claude", "hermes"]
    assert row["synthesis"]["agent"] == "claude" and row["thread"].startswith("t-")


def test_failed_command_prints_nothing_on_stdout_in_json_mode(fake, capsys):
    assert cli.main(["status", "k-none", "--json"]) == 5
    out = capsys.readouterr()
    assert out.out == "" and "unknown task" in out.err


# hail help agents ------------------------------------------------------------


def test_help_agents_guide(fake, capsys):
    assert cli.main(["help", "agents"]) == 0
    out = capsys.readouterr().out
    for needle in ("--json", "75", "untrusted", "hail list", "claude", "hermes", "code-review"):
        assert needle in out


def test_help_agents_without_config(capsys):
    assert cli.main(["help", "agents"]) == 0
    assert "no agents file" in capsys.readouterr().out


def test_help_unknown_topic(capsys):
    assert cli.main(["help", "nope"]) == 2


def test_usage_mentions_the_guide(capsys):
    assert cli.main(["--help"]) == 0
    assert "hail help agents" in capsys.readouterr().out


# preset agy (Antigravity CLI replaced Gemini CLI on 2026-06-18) ----------------------------


def test_agy_preset_prints_json_and_reads_the_response():
    from hail.parsers import parse
    from hail.runner import advice_enforced, build

    agent = parse_agent("g", {"preset": "agy", "bin": "nope-agy"})
    inv = build(agent, "hi", mode="advice")
    assert inv.argv == ("nope-agy", "-p", "hi", "--output-format", "json") and inv.stdin is None
    assert not advice_enforced(agent)  # --mode plan only prefixes the prompt: no read-only switch
    raw = r'{"conversation_id": "c", "status": "SUCCESS", "response": "pong\n"}'
    assert parse(agent.parser, raw) == "pong"


def test_gemini_preset_is_marked_legacy():
    from hail.presets import PRESETS

    assert "legacy" in PRESETS["gemini"].description.lower()


# review fixes ---------------------------------------------------------------------------


def test_invalid_thread_id_and_council_size_are_usage_errors(fake):
    assert cli.main(["claude", "--thread", "bad id!", "hi"]) == 2
    assert cli.main(["council", "--agents", "claude", "q"]) == 2


def test_council_where_no_agent_ran_keeps_the_real_exit_code(fake):
    assert cli.main(["council", "--agents", "ghost,nobody", "q"]) == 5


def test_council_with_mixed_failures_exits_4(agents_file, monkeypatch):
    agents_file(CONFIG)
    monkeypatch.setattr("hail.hub.ask", lambda agent, *a, **k: Reply(agent.name, "crash", "", 2))
    assert cli.main(["council", "--agents", "claude,ghost", "q"]) == 4
