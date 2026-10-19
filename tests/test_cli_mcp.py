import io
import json
import subprocess

import pytest
from conftest import FakeProc

from hail import cli, mcp
from hail.config import load_agents
from hail.runner import Reply

CONFIG = '[agents.hermes]\npreset = "hermes"\nhost = "vps"\n\n[agents.me]\npreset = "command"\ncommand = "cat"\n'


@pytest.fixture
def fake_ask(monkeypatch):
    seen = []

    def ask(agent, message):
        seen.append((agent.name, message))
        return Reply(agent=agent.name, text=f"echo:{message}", raw="", exit_code=0)

    monkeypatch.setattr(cli, "ask", ask)
    monkeypatch.setattr(mcp, "ask", ask)
    return seen


def test_help_and_version(capsys):
    assert cli.main([]) == 0
    assert "hail <agent>" in capsys.readouterr().out
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("hail ")


def test_ask_text_and_json(agents_file, fake_ask, capsys):
    agents_file(CONFIG)
    assert cli.main(["hermes", "hi", "there"]) == 0
    assert capsys.readouterr().out.strip() == "echo:hi there"
    assert cli.main(["hermes", "--json", "yo"]) == 0
    assert json.loads(capsys.readouterr().out) == {"agent": "hermes", "reply": "echo:yo", "exit_code": 0}


def test_ask_from_stdin(agents_file, fake_ask, monkeypatch):
    agents_file(CONFIG)
    monkeypatch.setattr("sys.stdin", io.StringIO("piped"))
    assert cli.main(["hermes"]) == 0
    assert fake_ask == [("hermes", "piped")]


def test_ask_unknown_agent(agents_file, capsys):
    agents_file(CONFIG)
    assert cli.main(["ghost", "x"]) == 1
    assert "unknown agent 'ghost'" in capsys.readouterr().err


def test_list(agents_file, capsys):
    agents_file(CONFIG)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "hermes" in out and "vps" in out and "local" in out


def test_list_empty(agents_file, capsys):
    agents_file("")
    assert cli.main(["list"]) == 0
    assert "no agents yet" in capsys.readouterr().out


def test_init_then_add(agents_file, capsys):
    path = agents_file("")
    path.unlink()
    assert cli.main(["init"]) == 0
    assert cli.main(["init"]) == 0
    assert "already exists" in capsys.readouterr().out
    assert (
        cli.main(
            ["add", "kernel", "--preset", "openclaw", "--host", "vps", "--set", "agent_id=main", "--set", "timeout=90"]
        )
        == 0
    )
    agent = load_agents()["kernel"]
    assert agent.host == "vps" and agent.timeout == 90 and agent.variables["agent_id"] == "main"
    assert cli.main(["add", "kernel", "--preset", "claude"]) == 1


def test_add_creates_file_and_command(agents_file):
    path = agents_file("")
    path.unlink()
    assert cli.main(["add", "x", "--preset", "command", "--command", 'say "hi"']) == 0
    assert load_agents()["x"].command == 'say "hi"'


def test_add_rejects_bad_pair(agents_file, capsys):
    agents_file("")
    assert cli.main(["add", "x", "--preset", "claude", "--set", "novalue"]) == 1
    assert "KEY=VALUE" in capsys.readouterr().err


def test_doctor(agents_file, fake_run, capsys, monkeypatch):
    agents_file(CONFIG + 'path = ["~/bin"]\n')
    calls, state = fake_run
    state["proc"] = FakeProc(b"/home/u/.local/bin/hermes\n")
    monkeypatch.setattr(cli.shutil, "which", lambda p, path=None: None)
    assert cli.main(["doctor"]) == 1  # local 'cat' reported missing
    out = capsys.readouterr().out
    assert "ok   hermes" in out and "FAIL me" in out
    assert calls[0]["argv"][-1].startswith("sh -c ")


def test_doctor_remote_failures(agents_file, fake_run):
    agents_file('[agents.h]\npreset = "hermes"\nhost = "vps"\n')
    _, state = fake_run
    state["proc"] = FakeProc(b"", b"denied", 255)
    assert cli.main(["doctor"]) == 1
    state["proc"] = subprocess.TimeoutExpired("ssh", 30)
    assert cli.main(["doctor"]) == 1


def rpc(method, params=None, msg_id=1):
    return mcp.handle_line(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}))


def test_mcp_initialize_and_list():
    init = rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert rpc("initialize", {"protocolVersion": "1999"})["result"]["protocolVersion"] == "2025-06-18"
    names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
    assert names == ["hail_list", "hail_ask"]
    assert rpc("ping")["result"] == {}


def test_mcp_calls(agents_file, fake_ask):
    agents_file(CONFIG)
    listed = rpc("tools/call", {"name": "hail_list"})["result"]
    assert "hermes: preset=hermes host=vps" in listed["content"][0]["text"]
    asked = rpc("tools/call", {"name": "hail_ask", "arguments": {"agent": "hermes", "message": "hi"}})["result"]
    assert "untrusted data" in asked["content"][0]["text"] and "echo:hi" in asked["content"][0]["text"]
    ghost = rpc("tools/call", {"name": "hail_ask", "arguments": {"agent": "ghost", "message": "hi"}})["result"]
    assert ghost["isError"]


def test_mcp_list_empty(agents_file):
    agents_file("")
    assert "No agents configured" in rpc("tools/call", {"name": "hail_list"})["result"]["content"][0]["text"]


def test_mcp_errors():
    assert mcp.handle_line("{nope")["error"]["code"] == -32700
    assert mcp.handle_line("[1]")["error"]["code"] == -32600
    assert rpc("nope")["error"]["code"] == -32601
    assert rpc("tools/call", {"name": "zzz"})["error"]["code"] == -32602
    bad = rpc("tools/call", {"name": "hail_ask", "arguments": {"agent": 1}})
    assert bad["error"]["code"] == -32602
    assert mcp.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None


def test_mcp_serve_loop():
    stdin = io.StringIO("\n" + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n")
    stdout = io.StringIO()
    assert mcp.serve(stdin, stdout) == 0
    assert json.loads(stdout.getvalue())["id"] == 7


def test_cli_nonzero_exit(agents_file, monkeypatch, capsys):
    agents_file(CONFIG)
    monkeypatch.setattr(cli, "ask", lambda a, m: Reply(agent=a.name, text="crash", raw="", exit_code=2))
    assert cli.main(["hermes", "x"]) == 1
    assert "exited with code 2" in capsys.readouterr().err


def test_mcp_nonzero_exit_is_error(agents_file, monkeypatch):
    agents_file(CONFIG)
    monkeypatch.setattr(mcp, "ask", lambda a, m: Reply(agent=a.name, text="crash", raw="", exit_code=2))
    res = rpc("tools/call", {"name": "hail_ask", "arguments": {"agent": "hermes", "message": "x"}})["result"]
    assert res["isError"] and "exit code 2" in res["content"][0]["text"]


@pytest.mark.parametrize(
    "args, msg",
    [
        (["--command", "c", "--set", "bad key=1"], "simple key"),
        (["--command", "c", "--set", "a\nb=1"], "simple key"),
        (["--command", "c", "--set", "host=evil"], "use --host"),
        (["--command", "c", "--set", "bin=a\x7fb"], "control characters"),
        (["--command", "a\nb"], "control characters"),
    ],
)
def test_add_rejects_unsafe_values(agents_file, capsys, args, msg):
    agents_file("")
    assert cli.main(["add", "x", "--preset", "command", *args]) == 1
    assert msg in capsys.readouterr().err


def test_add_keeps_string_values(agents_file):
    agents_file("")
    assert cli.main(["add", "o", "--preset", "openclaw", "--set", "session=007"]) == 0
    assert load_agents()["o"].variables["session"] == "007"


def test_doctor_unparsable_command_and_missing_ssh(agents_file, fake_run, capsys):
    agents_file(
        '[agents.a]\npreset = "command"\ncommand = "\\"unbalanced"\nhost = "vps"\n'
        '[agents.b]\npreset = "claude"\nhost = "vps"\n'
    )
    _, state = fake_run
    state["proc"] = FileNotFoundError(2, "nf", "ssh")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "cannot parse command" in out and "cannot run ssh" in out


def test_mcp_robustness(monkeypatch):
    bad_params = mcp.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}))
    assert bad_params["error"]["code"] == -32602
    bad_args = rpc("tools/call", {"name": "hail_ask", "arguments": "x"})
    assert bad_args["error"]["code"] == -32602
    assert mcp.handle_line(json.dumps({"jsonrpc": "2.0", "id": 5, "result": {}})) is None

    def boom(_params):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(mcp.HANDLERS, "tools/list", boom)
    assert rpc("tools/list")["error"]["code"] == -32603
