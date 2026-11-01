"""0.4.1: empty stdin for inline presets, server errors reach the caller, forced_command agents, per-agent daily cap."""

import subprocess

import pytest
from conftest import FakeProc

from hail import cli
from hail.config import ConfigError, load_config, parse_agent
from hail.envelope import USER
from hail.hub import Hub, Refused, Request
from hail.runner import ask, build
from hail.store import Store


def test_inline_preset_gets_empty_stdin_not_hails_own(fake_run, monkeypatch):
    monkeypatch.setattr("hail.runner.shutil.which", lambda p, path=None: "/usr/bin/hermes")
    calls, state = fake_run
    state["proc"] = FakeProc(b"answer")
    ask(parse_agent("h", {"preset": "hermes"}), "hi")
    assert calls[0]["stdin"] is subprocess.DEVNULL and calls[0]["input"] is None


def test_stdin_preset_still_gets_the_message(fake_run):
    calls, _ = fake_run
    ask(parse_agent("c", {"preset": "claude", "host": "vps"}), "hi")
    assert calls[0]["input"] == b"hi" and calls[0]["stdin"] is None


def test_server_error_json_reaches_the_caller(fake_run):
    """A pinned script that refuses (e.g. a spending cap) answers with JSON on stdout and a non-zero exit."""
    _, state = fake_run
    state["proc"] = FakeProc(b'{"error":"paused: daily budget reached"}', b"", 3)
    reply = ask(parse_agent("o", {"preset": "openclaw", "host": "pi", "forced_command": True}), "hi")
    assert not reply.ok and "paused: daily budget reached" in reply.text


def test_forced_command_sends_only_the_message():
    agent = parse_agent("pi", {"preset": "openclaw", "host": "pi-advice", "forced_command": True})
    inv = build(agent, "hello")
    assert inv.argv == ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", "pi-advice", "true")
    assert inv.stdin == "hello"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"preset": "openclaw", "forced_command": True}, "needs a 'host'"),
        ({"preset": "openclaw", "host": "pi", "forced_command": "yes"}, "true or false"),
        ({"preset": "claude", "max_calls_per_day": 0}, "positive integer"),
    ],
)
def test_new_keys_are_validated(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_agent("x", raw)


def test_new_keys_do_not_leak_into_template_variables():
    agent = parse_agent("pi", {"preset": "openclaw", "host": "pi", "forced_command": True, "max_calls_per_day": 5})
    assert agent.max_calls_per_day == 5 and "forced_command" not in agent.variables
    assert parse_agent("c", {"preset": "claude"}).max_calls_per_day is None


def test_doctor_forced_command_probes_ssh_only(agents_file, fake_run, capsys):
    agents_file('[agents.pi]\npreset = "openclaw"\nhost = "pi-advice"\nforced_command = true\n')
    calls, state = fake_run
    state["proc"] = FakeProc(b'{"error":"empty message"}', b"", 2)  # the pinned script refused an empty message
    assert cli.main(["doctor"]) == 0
    assert "ssh ok (forced command" in capsys.readouterr().out
    assert calls[0]["argv"][-1] == "true" and calls[0]["stdin"] is subprocess.DEVNULL
    state["proc"] = FakeProc(b"", b"Connection timed out", 255)
    assert cli.main(["doctor"]) == 1
    assert "ssh failed Connection timed out" in capsys.readouterr().out
    state["proc"] = subprocess.TimeoutExpired("ssh", 30)
    assert cli.main(["doctor"]) == 1


def test_doctor_never_inherits_stdin(agents_file, fake_run):
    agents_file('[agents.h]\npreset = "hermes"\nhost = "vps"\n')
    calls, state = fake_run
    state["proc"] = FakeProc(b"/usr/bin/hermes\n")
    assert cli.main(["doctor"]) == 0
    assert calls[0]["stdin"] is subprocess.DEVNULL


def _hub(agents_file, tmp_path, limit_line):
    agents_file(f'[agents.paid]\npreset = "claude"\n{limit_line}\n[agents.free]\npreset = "claude"\n')
    return Hub(load_config(), Store(tmp_path / "j.db"), {})


def test_daily_cap_refuses_after_n_asks_all_threads(agents_file, fake_run, tmp_path):
    hub = _hub(agents_file, tmp_path, "max_calls_per_day = 2")
    hub.consult(Request(message="a", agent="paid", thread="t1"))
    hub.consult(Request(message="b", agent="paid", thread="t2"))
    with pytest.raises(Refused, match=r"daily limit \(2 calls in 24 h"):
        hub.consult(Request(message="c", agent="paid", thread="t3"))
    assert hub.consult(Request(message="d", agent="free", thread="t4")).ok  # other agents unaffected


def test_daily_cap_applies_to_the_user_too_and_forgets_after_24h(agents_file, fake_run, tmp_path, monkeypatch):
    hub = _hub(agents_file, tmp_path, "max_calls_per_day = 1")
    assert hub.sender_for(None) == USER
    hub.consult(Request(message="a", agent="paid"))
    with pytest.raises(Refused):
        hub.consult(Request(message="b", agent="paid"))
    real = __import__("time").time
    monkeypatch.setattr("hail.store.time.time", lambda: real() + 86401)
    assert hub.consult(Request(message="c", agent="paid")).ok


def test_no_cap_by_default(agents_file, fake_run, tmp_path):
    hub = _hub(agents_file, tmp_path, "")
    for i in range(5):
        assert hub.consult(Request(message=str(i), agent="paid", thread=f"t{i}")).ok
