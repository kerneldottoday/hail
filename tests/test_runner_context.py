import pytest

from hail.config import ConfigError, load_config, parse_agent
from hail.runner import build

ENV = {"HAIL_THREAD": "t-1", "HAIL_HOP": "2", "HAIL_SELF": "claude"}


def test_agent_card_fields():
    agent = parse_agent("rev", {"preset": "claude", "skills": ["code-review", "python"], "cost": "high"})
    assert agent.skills == ("code-review", "python")
    assert agent.cost == "high"
    assert parse_agent("x", {"preset": "claude"}).cost == "medium"


@pytest.mark.parametrize("raw", [{"skills": "python"}, {"skills": [1]}, {"cost": "free"}])
def test_agent_card_invalid(raw):
    with pytest.raises(ConfigError):
        parse_agent("x", {"preset": "claude", **raw})


def test_load_config_reads_policy_and_telegram(agents_file):
    agents_file('[agents.a]\npreset = "claude"\n\n[policy]\nmax_hops = 2\n\n[telegram]\nchat_id = -100123\n')
    cfg = load_config()
    assert list(cfg.agents) == ["a"]
    assert cfg.policy.max_hops == 2
    assert cfg.telegram == {"chat_id": -100123}


def test_load_config_bad_policy(agents_file):
    agents_file("[policy]\nmax_hops = 0\n")
    with pytest.raises(ConfigError, match="max_hops"):
        load_config()


def test_local_build_exports_context(monkeypatch):
    agent = parse_agent("me", {"preset": "command", "command": "cat"})
    inv = build(agent, "hi", env=ENV)
    assert inv.env is not None and inv.env["HAIL_THREAD"] == "t-1" and "PATH" in inv.env


def test_remote_build_exports_context():
    agent = parse_agent("h", {"preset": "hermes", "host": "vps"})
    inv = build(agent, "hi", env=ENV)
    script = inv.argv[-1]
    assert "export HAIL_THREAD=t-1 HAIL_HOP=2 HAIL_SELF=claude;" in script


def test_advice_mode_adds_plan_flag_for_claude():
    agent = parse_agent("c", {"preset": "claude"})
    assert "plan" in build(agent, "q", mode="advice").argv
    assert "plan" not in build(agent, "q", mode="action").argv
    remote = parse_agent("c", {"preset": "claude", "host": "vps"})
    assert "--permission-mode plan" in build(remote, "q", mode="advice").argv[-1]
