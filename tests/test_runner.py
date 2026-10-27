import shlex
import shutil
import subprocess

import pytest
from conftest import FakeProc

from hail.config import parse_agent
from hail.runner import HailError, ask, build, remote_quote

TRICKY = 'Salut "toi" $HOME `id` éè \' ; rm -rf /'


def test_remote_hermes_script():
    agent = parse_agent("h", {"preset": "hermes", "host": "vps", "bin": "~/.local/bin/hermes"})
    inv = build(agent, "hi")
    assert inv.argv[:7] == ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", "vps")
    assert 'msg=$(cat); exec "$@" "$HOME/.local/bin/hermes" chat -q "$msg" -Q --max-turns 8' in inv.argv[-1]
    assert inv.stdin == "hi"


def test_remote_path_prefix_and_command():
    agent = parse_agent("c", {"preset": "command", "host": "vps", "command": "ask-x", "path": ["~/bin", "/opt/b"]})
    script = build(agent, "m").argv[-1]
    assert 'export PATH="$HOME/bin":/opt/b:"$PATH"; ' in script and 'exec "$@" sh -c ask-x' in script


def test_local_command_uses_shell_and_stdin():
    agent = parse_agent("c", {"preset": "command", "command": "cat"})
    inv = build(agent, "m")
    assert inv.shell and inv.argv == "cat" and inv.stdin == "m"


def test_local_argv_preset_passes_message_inline():
    agent = parse_agent("h", {"preset": "hermes", "bin": "definitely-not-a-binary-xyz"})
    inv = build(agent, TRICKY)
    assert inv.argv == ("definitely-not-a-binary-xyz", "chat", "-q", TRICKY, "-Q", "--max-turns", "8")
    assert inv.stdin is None


def test_stdin_preset_local_and_remote():
    local = build(parse_agent("c", {"preset": "claude", "bin": "nope-xyz"}), "hi")
    assert local.argv == ("nope-xyz", "-p") and local.stdin == "hi"
    remote = build(parse_agent("c", {"preset": "claude", "host": "vps"}), "hi")
    assert remote.argv[-1].endswith("exec \"$@\" claude -p'") and remote.stdin == "hi"


def test_windows_batch_shim_is_refused(monkeypatch):
    monkeypatch.setattr("hail.runner.shutil.which", lambda p, path=None: r"C:\npm\openclaw.CMD")
    with pytest.raises(HailError, match="batch shim"):
        build(parse_agent("o", {"preset": "openclaw"}), 'x" & calc & "')
    inv = build(parse_agent("c", {"preset": "claude"}), "x")  # stdin delivery is safe
    assert inv.argv[0].endswith(".CMD") and inv.stdin == "x"


def test_local_path_is_prepended(monkeypatch):
    monkeypatch.setenv("PATH", "orig")
    inv = build(parse_agent("c", {"preset": "command", "command": "x", "path": ["/opt/a"]}), "m")
    assert inv.env["PATH"].startswith("/opt/a") and inv.env["PATH"].endswith("orig")


def test_missing_template_variable():
    agent = parse_agent("o", {"preset": "openclaw"})
    broken = agent.__class__(**{**agent.__dict__, "variables": {}})
    with pytest.raises(HailError, match="missing setting"):
        build(broken, "m")


def test_remote_quote():
    assert remote_quote("~/a b") == '"$HOME/a b"'
    assert remote_quote('~/x"$`') == '"$HOME/x\\"\\$\\`"'
    assert remote_quote("plain") == "plain"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_remote_script_delivers_message_intact():
    """Run the generated remote script in a real shell with printf standing in for the agent."""
    agent = parse_agent("t", {"preset": "hermes", "host": "vps", "bin": "printf"})
    shell, flag, script = shlex.split(build(agent, TRICKY).argv[-1])
    script = script.replace("printf chat -q", "printf %s").replace(" -Q --max-turns 8", "")
    proc = subprocess.run([shell, flag, script], input=TRICKY.encode(), capture_output=True, check=False)
    assert proc.stdout.decode() == TRICKY


def test_ask_success(fake_run):
    calls, state = fake_run
    state["proc"] = FakeProc(b"OK\nsession_id: 1\n")
    agent = parse_agent("h", {"preset": "hermes", "host": "vps"})
    reply = ask(agent, "  ping  ")
    assert reply.text == "OK"
    assert calls[0]["input"] == b"ping"
    assert calls[0]["timeout"] == 180


def test_ask_validation():
    agent = parse_agent("h", {"preset": "hermes", "host": "vps", "max_chars": 3})
    with pytest.raises(HailError, match="empty"):
        ask(agent, "   ")
    with pytest.raises(HailError, match="too long"):
        ask(agent, "abcd")
    with pytest.raises(HailError, match="NUL"):
        ask(agent, "a\x00")


@pytest.mark.parametrize(
    "error, msg",
    [
        (subprocess.TimeoutExpired("ssh", 1), "did not answer"),
        (FileNotFoundError(2, "nf", "ssh"), "command not found"),
        (PermissionError(13, "denied"), "could not start"),
    ],
)
def test_ask_process_errors(fake_run, error, msg):
    _, state = fake_run
    state["proc"] = error
    with pytest.raises(HailError, match=msg):
        ask(parse_agent("h", {"preset": "hermes", "host": "vps"}), "x")


def test_ask_empty_output(fake_run):
    _, state = fake_run
    state["proc"] = FakeProc(b"", b"boom", 255)
    with pytest.raises(HailError, match="exit 255.*boom"):
        ask(parse_agent("h", {"preset": "hermes", "host": "vps"}), "x")


def test_ask_local_command_end_to_end():
    agent = parse_agent(
        "echo", {"preset": "command", "command": 'python -c "import sys; print(sys.stdin.read().upper())"'}
    )
    assert ask(agent, "hello").text == "HELLO"


def test_ask_nonzero_exit_is_reported(fake_run):
    _, state = fake_run
    state["proc"] = FakeProc(b"Traceback: boom", b"", 1)
    reply = ask(parse_agent("h", {"preset": "command", "command": "x", "host": "vps"}), "x")
    assert not reply.ok and reply.exit_code == 1
