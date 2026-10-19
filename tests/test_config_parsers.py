import pytest

from hail.config import ConfigError, load_agents, parse_agent
from hail.parsers import find_key, parse
from hail.presets import get_preset


def test_load_agents_with_defaults(agents_file):
    agents_file('[agents.hermes]\npreset = "hermes"\nhost = "vps"\nbin = "~/.local/bin/hermes"\n')
    agent = load_agents()["hermes"]
    assert agent.host == "vps"
    assert agent.is_remote
    assert agent.parser == "hermes"
    assert agent.variables["bin"] == "~/.local/bin/hermes"
    assert agent.variables["max_turns"] == "8"
    assert agent.timeout == 180


@pytest.mark.parametrize("host", ["local", "localhost", ""])
def test_local_host_aliases(host):
    assert parse_agent("x", {"preset": "claude", "host": host}).host is None


def test_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HAIL_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match="hail init"):
        load_agents()


def test_bad_toml(agents_file):
    agents_file("[agents.x\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_agents()


def test_agents_must_be_table(agents_file):
    agents_file('agents = "nope"\n')
    with pytest.raises(ConfigError, match="must be a table"):
        load_agents()


@pytest.mark.parametrize("name", ["list", "mcp", "Bad", "a b", "-x"])
def test_invalid_names(name):
    with pytest.raises(ConfigError, match="invalid agent name"):
        parse_agent(name, {"preset": "claude"})


@pytest.mark.parametrize(
    "raw, msg",
    [
        ("nope", "must be a table"),
        ({"preset": "zzz"}, "unknown preset"),
        ({"preset": "command"}, "needs a 'command'"),
        ({"preset": "claude", "timeout": 0}, "positive integer"),
        ({"preset": "claude", "timeout": True}, "positive integer"),
        ({"preset": "claude", "path": "x"}, "list of strings"),
        ({"preset": "claude", "parser": "xml"}, "unknown parser"),
        ({"preset": "claude", "parser": "json:"}, "unknown parser"),
        ({"preset": "command", "command": 3}, "must be a string"),
        ({"preset": "claude", "host": "-oProxyCommand=calc"}, "invalid host"),
        ({"preset": "claude", "host": "my vps"}, "invalid host"),
    ],
)
def test_invalid_agents(raw, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_agent("a", raw)


def test_parser_override_and_description():
    agent = parse_agent("a", {"preset": "command", "command": "x", "parser": "json:k", "description": "d"})
    assert agent.parser == "json:k"
    assert agent.description == "d"


def test_unknown_preset():
    with pytest.raises(ValueError, match="known"):
        get_preset("nope")


def test_parse_text():
    assert parse("text", "  hi \n") == "hi"


def test_parse_hermes_strips_noise():
    raw = "OK done\n\nDEEPSEEK [▰▰▱▱] $9 / $19   49%\n\nsession_id: 2026\nWarning: Unknown toolsets x\n"
    assert parse("hermes", raw) == "OK done"


def test_parse_json_key_nested():
    raw = 'log line\n{"a": [{"b": 1}, {"finalAssistantVisibleText": " OK "}]}'
    assert parse("json:finalAssistantVisibleText", raw) == "OK"


@pytest.mark.parametrize("raw", ["no json here", "{broken", '{"other": 1}'])
def test_parse_json_falls_back_to_raw(raw):
    assert parse("json:k", raw) == raw


def test_unknown_parser():
    with pytest.raises(ValueError):
        parse("xml", "x")


def test_find_key_none():
    assert find_key(3, "k") is None


def test_bom_is_accepted(agents_file):
    path = agents_file("")
    path.write_bytes(b"\xef\xbb\xbf" + b'[agents.a]\npreset = "claude"\n')
    assert "a" in load_agents()


def test_host_forms():
    assert parse_agent("a", {"preset": "claude", "host": "user@10.0.0.1"}).host == "user@10.0.0.1"


def test_parse_json_skips_noise_and_trailing_text():
    raw = 'warn {not json}\n{"x": {"answer": "yes"}}\ntrailing log {'
    assert parse("json:answer", raw) == "yes"
