"""Advice mode enforced by each preset (read-only toolsets, advisor agent, advice command)."""

import pytest

from hail.config import ConfigError, load_config, parse_agent
from hail.hub import Hub, Refused, Request
from hail.runner import Reply, advice_enforced, build
from hail.store import Store


def test_hermes_advice_restricts_toolsets():
    agent = parse_agent("h", {"preset": "hermes"})
    argv = build(agent, "q", mode="advice").argv
    assert argv[-2:] == ("-t", "web,session_search,vision,hail")
    assert "-t" not in build(agent, "q", mode="action").argv
    custom = parse_agent("h", {"preset": "hermes", "advice_toolsets": "web"})
    assert build(custom, "q", mode="advice").argv[-1] == "web"


def test_openclaw_advice_switches_to_advisor_agent():
    plain = parse_agent("o", {"preset": "openclaw", "host": "vps"})
    assert not advice_enforced(plain)
    agent = parse_agent("o", {"preset": "openclaw", "host": "vps", "advice_agent_id": "advisor"})
    assert advice_enforced(agent)
    assert "advisor" in build(agent, "q", mode="advice").argv[-1]
    script = build(agent, "q", mode="action").argv[-1]
    assert "advisor" not in script and "main" in script


def test_command_advice_command():
    agent = parse_agent("c", {"preset": "command", "command": "full-agent", "advice_command": "readonly-agent"})
    assert advice_enforced(agent)
    assert build(agent, "q", mode="advice").argv == "readonly-agent"
    assert build(agent, "q", mode="action").argv == "full-agent"
    assert not advice_enforced(parse_agent("c", {"preset": "command", "command": "x"}))


def test_claude_and_hermes_are_enforced():
    assert advice_enforced(parse_agent("c", {"preset": "claude"}))
    assert advice_enforced(parse_agent("h", {"preset": "hermes"}))


def test_advice_command_must_be_a_string():
    with pytest.raises(ConfigError):
        parse_agent("c", {"preset": "command", "command": "x", "advice_command": 3})


def test_strict_advice_refuses_unenforceable_agents(agents_file, tmp_path, monkeypatch):
    agents_file(
        '[agents.loose]\npreset = "command"\ncommand = "cat"\n\n'
        '[agents.claude]\npreset = "claude"\n\n[policy]\nstrict_advice = true\n'
    )
    monkeypatch.setattr("hail.hub.ask", lambda a, m, **kw: Reply(a.name, "ok", "", 0))
    hub = Hub(load_config(), Store(tmp_path / "h.db"), {"HAIL_SELF": "claude"})
    with pytest.raises(Refused, match="advice"):
        hub.consult(Request(message="x", agent="loose"))
    user_hub = Hub(load_config(), Store(tmp_path / "h.db"), {})
    assert user_hub.consult(Request(message="x", agent="loose")).ok  # the user asks for action: fine


def test_hermes_advice_flags_reach_the_remote_script():
    agent = parse_agent("h", {"preset": "hermes", "host": "vps"})
    script = build(agent, "q", mode="advice").argv[-1]
    assert "-t web,session_search,vision,hail" in script
    assert "-t " not in build(agent, "q", mode="action").argv[-1]


def test_openclaw_without_advisor_keeps_its_agent_in_advice():
    agent = parse_agent("o", {"preset": "openclaw", "host": "vps"})
    script = build(agent, "q", mode="advice").argv[-1]
    assert "main" in script and "advisor" not in script


def test_command_advice_command_over_ssh():
    agent = parse_agent("c", {"preset": "command", "host": "vps", "command": "full", "advice_command": "readonly"})
    assert "sh -c readonly" in build(agent, "q", mode="advice").argv[-1]
    assert "sh -c full" in build(agent, "q", mode="action").argv[-1]


@pytest.mark.parametrize(
    ("preset", "enforced"), [("codex", True), ("ollama", True), ("gemini", False), ("openclaw", False)]
)
def test_which_presets_enforce_advice(preset, enforced):
    assert advice_enforced(parse_agent("a", {"preset": preset})) is enforced


@pytest.mark.parametrize("key", ["advice_agent_id", "advice_toolsets"])
def test_advice_settings_must_be_non_empty_strings(key):
    with pytest.raises(ConfigError, match=key):
        parse_agent("o", {"preset": "openclaw", key: "  "})


def test_strict_advice_must_be_a_boolean(agents_file):
    agents_file('[agents.a]\npreset = "claude"\n\n[policy]\nstrict_advice = "yes"\n')
    with pytest.raises(Exception, match="strict_advice"):
        load_config()


def test_strict_advice_leaves_allowed_actions_alone(agents_file, tmp_path, monkeypatch):
    agents_file(
        '[agents.loose]\npreset = "command"\ncommand = "cat"\n\n[agents.claude]\npreset = "claude"\n\n'
        '[policy]\nstrict_advice = true\n[policy.act]\nclaude = ["loose"]\n'
    )
    monkeypatch.setattr("hail.hub.ask", lambda a, m, **kw: Reply(a.name, "ok", "", 0))
    hub = Hub(load_config(), Store(tmp_path / "h.db"), {"HAIL_SELF": "claude"})
    assert hub.consult(Request(message="x", agent="loose", mode="action")).ok


def test_strict_advice_lets_agents_consult_enforced_ones(agents_file, tmp_path, monkeypatch):
    agents_file(
        '[agents.loose]\npreset = "command"\ncommand = "cat"\nadvice_command = "cat -n"\n\n'
        '[agents.claude]\npreset = "claude"\n\n[policy]\nstrict_advice = true\n'
    )
    monkeypatch.setattr("hail.hub.ask", lambda a, m, **kw: Reply(a.name, "ok", "", 0))
    hub = Hub(load_config(), Store(tmp_path / "h.db"), {"HAIL_SELF": "claude"})
    assert hub.consult(Request(message="x", agent="loose")).ok


def test_list_flags_agents_without_enforced_advice(agents_file, capsys):
    from hail.cli import cmd_list

    agents_file('[agents.loose]\npreset = "command"\ncommand = "cat"\n\n[agents.claude]\npreset = "claude"\n')
    assert cmd_list([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "prompt only" in next(line for line in lines if line.startswith("loose"))
    assert "prompt only" not in next(line for line in lines if line.startswith("claude"))


# --- review fixes: advice settings must really be narrower ---------------------------------


@pytest.mark.parametrize("toolsets", ["web,terminal", "all", "*", "web;rm", "file", "web,,vision"])
def test_hermes_advice_toolsets_must_be_read_only(toolsets):
    with pytest.raises(ConfigError, match="advice_toolsets"):
        parse_agent("h", {"preset": "hermes", "advice_toolsets": toolsets})


def test_hermes_advice_toolsets_accepts_a_narrower_read_only_list():
    agent = parse_agent("h", {"preset": "hermes", "advice_toolsets": "web, vision"})
    assert build(agent, "q", mode="advice").argv[-1] == "web,vision"


def test_advice_variant_must_differ_from_the_full_one():
    with pytest.raises(ConfigError, match="advice_agent_id"):
        parse_agent("o", {"preset": "openclaw", "advice_agent_id": "main"})
    with pytest.raises(ConfigError, match="advice_agent_id"):
        parse_agent("o", {"preset": "openclaw", "agent_id": "ops", "advice_agent_id": "ops"})
    with pytest.raises(ConfigError, match="advice_command"):
        parse_agent("c", {"preset": "command", "command": "bot", "advice_command": " bot "})


def test_routing_by_skill_skips_agents_strict_advice_would_refuse(agents_file, tmp_path, monkeypatch):
    agents_file(
        '[agents.cheap]\npreset = "command"\ncommand = "cat"\nskills = ["gtfs"]\ncost = "low"\n\n'
        '[agents.safe]\npreset = "command"\ncommand = "cat"\nadvice_command = "cat -n"\nskills = ["gtfs"]\n'
        'cost = "high"\n\n[agents.claude]\npreset = "claude"\n\n[policy]\nstrict_advice = true\n'
    )
    monkeypatch.setattr("hail.hub.ask", lambda a, m, **kw: Reply(a.name, "ok", "", 0))
    hub = Hub(load_config(), Store(tmp_path / "h.db"), {"HAIL_SELF": "claude"})
    assert hub.consult(Request(message="x", skill="gtfs")).agent == "safe"
