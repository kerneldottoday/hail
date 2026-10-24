import io
import json

import pytest

from hail import cli, commands, mcp, tasks
from hail.runner import HailError, Reply
from hail.store import default_store

CONFIG = """
[agents.claude]
preset = "claude"
skills = ["code-review"]

[agents.hermes]
preset = "hermes"
host = "vps"
"""


@pytest.fixture
def fake(agents_file, monkeypatch):
    agents_file(CONFIG)
    seen = []

    def ask(agent, message, env=None, mode="action", drop_env=()):
        seen.append({"agent": agent.name, "env": env, "mode": mode})
        return Reply(agent.name, f"ok from {agent.name}", "", 0)

    monkeypatch.setattr("hail.hub.ask", ask)
    monkeypatch.setattr(tasks, "spawn", lambda task_id: None)
    return seen


def rpc(name, arguments):
    line = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    return mcp.handle_line(line)["result"]


def test_parse_flags_and_words():
    p = commands.parse(["--thread", "t-1", "--mode", "advice", "hello", "--json", "world"])
    assert p.flags == {"--thread": "t-1", "--mode": "advice", "--json": "1"}
    assert p.words == ("hello", "world")
    with pytest.raises(HailError):
        commands.parse(["--thread"])


def test_cli_ask_with_thread_and_from(fake, capsys):
    assert cli.main(["claude", "--thread", "t-abc", "--from", "hermes", "help"]) == 0
    assert capsys.readouterr().err.strip().endswith("thread t-abc · hop 1")
    assert fake[0]["mode"] == "advice"


def test_cli_ask_by_skill(fake, capsys):
    assert cli.main(["ask", "--skill", "code-review", "review it"]) == 0
    assert "ok from claude" in capsys.readouterr().out
    assert cli.main(["ask", "hermes", "hi"]) == 0


def test_cli_refusal_exit_code(fake, capsys, monkeypatch):
    monkeypatch.setenv("HAIL_SELF", "claude")
    monkeypatch.setenv("HAIL_THREAD", "t-1")
    monkeypatch.setenv("HAIL_HOP", "1")
    assert cli.main(["hermes", "--mode", "action", "do it"]) == 1
    assert "action" in capsys.readouterr().err


def test_cli_send_and_status(fake, capsys):
    assert cli.main(["send", "claude", "long job"]) == 0
    task_id = capsys.readouterr().out.split()[0]
    assert cli.main(["status", task_id]) == 0
    assert "submitted" in capsys.readouterr().out
    assert cli.main(["_task", task_id]) == 0
    assert cli.main(["status", task_id]) == 0
    assert "ok from claude" in capsys.readouterr().out
    assert cli.main(["status", "k-none"]) == 1
    assert cli.main(["status"]) == 1


def test_cli_council(fake, capsys):
    assert cli.main(["council", "--agents", "claude,hermes", "--synth", "claude", "which?"]) == 0
    out = capsys.readouterr().out
    assert "== claude" in out and "== hermes" in out and "synthesis by claude" in out


def test_cli_log_threads_thread_stop(fake, capsys):
    cli.main(["claude", "first"])
    thread = capsys.readouterr().err.split("thread ")[1].split()[0]
    assert cli.main(["log", "-n", "5"]) == 0
    assert "first" in capsys.readouterr().out
    assert cli.main(["log", "--grep", "nothing-like-this"]) == 0
    assert capsys.readouterr().out == ""
    assert cli.main(["threads"]) == 0
    assert thread in capsys.readouterr().out
    assert cli.main(["thread", thread]) == 0
    assert "ok from claude" in capsys.readouterr().out
    assert cli.main(["stop", thread]) == 0
    assert default_store().is_stopped(thread)
    assert cli.main(["claude", "--thread", thread, "again"]) == 1
    assert cli.main(["resume", thread]) == 0
    assert "STOPPED" not in (cli.main(["threads"]) or capsys.readouterr().out)
    assert cli.main(["thread", "t-none"]) == 1
    assert cli.main(["log", "-n", "x"]) == 1
    assert cli.main(["stop"]) == 1


def test_cli_telegram_not_configured(fake, capsys):
    assert cli.main(["telegram", "test"]) == 1
    assert "not configured" in capsys.readouterr().err


def test_cli_telegram_test_and_bad_action(agents_file, monkeypatch, tmp_path, capsys):
    token = tmp_path / "tok"
    token.write_text("1:x", encoding="utf-8")
    agents_file(f'[telegram]\nchat_id = -1\ntoken_file = "{token.as_posix()}"\n')
    sent = []
    monkeypatch.setattr("hail.telegram.http_api", lambda tok, timeout=15: lambda m, p: sent.append((m, p)))
    assert cli.main(["telegram", "test"]) == 0
    assert sent[0][0] == "sendMessage"
    assert cli.main(["telegram", "nope"]) == 1


def test_telegram_misconfigured_is_disabled_not_fatal(agents_file, capsys, monkeypatch):
    agents_file('[agents.claude]\npreset = "claude"\n\n[telegram]\nchat_id = -1\n')
    monkeypatch.setattr("hail.hub.ask", lambda a, m, env=None, mode="action", drop_env=(): Reply(a.name, "fine", "", 0))
    assert cli.main(["claude", "x"]) == 0
    assert "telegram disabled" in capsys.readouterr().err


def test_message_from_stdin_for_council(fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("piped question"))
    assert cli.main(["council", "--agents", "claude,hermes"]) == 0


def test_mcp_new_tools(fake):
    listed = rpc("hail_list", {})["content"][0]["text"]
    assert "skills=code-review" in listed and "(you are: unknown-agent)" in listed
    asked = rpc("hail_ask", {"skill": "code-review", "message": "hi", "from": "hermes"})
    assert not asked["isError"] and "thread t-" in asked["content"][0]["text"]
    sent = rpc("hail_send", {"agent": "claude", "message": "later"})["content"][0]["text"]
    task_id = sent.split()[1]
    assert "submitted" in rpc("hail_status", {"task": task_id})["content"][0]["text"]
    assert rpc("hail_status", {"task": "k-x"})["isError"]
    council = rpc("hail_council", {"question": "q", "agents": ["claude", "hermes"], "synth": "claude"})
    text = council["content"][0]["text"]
    assert "synthesis by claude" in text
    thread = text.rsplit("(thread ", 1)[1].rstrip(")")
    assert "ask" in rpc("hail_thread", {"thread": thread})["content"][0]["text"]
    assert rpc("hail_thread", {"thread": "t-none"})["isError"]


@pytest.mark.parametrize(
    "name, args",
    [
        ("hail_ask", {"message": 1}),
        ("hail_ask", {"message": "x", "agent": 3}),
        ("hail_status", {"task": 1}),
        ("hail_council", {"question": "q", "agents": "claude"}),
        ("hail_thread", {"thread": 2}),
    ],
)
def test_mcp_new_tools_validate(fake, name, args):
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert mcp.handle_line(line)["error"]["code"] == -32602
