import pytest

from hail.config import load_config
from hail.hub import Hub, Refused, Request
from hail.runner import HailError, Reply
from hail.store import Store

CONFIG = """
[agents.hermes]
preset = "hermes"
host = "vps"
skills = ["ops"]

[agents.claude]
preset = "claude"
skills = ["code-review", "python"]
cost = "high"

[agents.cheap]
preset = "command"
command = "cat"
skills = ["code-review"]
cost = "low"

[agents.openclaw]
preset = "openclaw"

[policy]
max_hops = 3
max_calls_per_thread = 5
"""


@pytest.fixture
def setup(agents_file, tmp_path, monkeypatch):
    agents_file(CONFIG)
    calls = []

    def fake_ask(agent, message, env=None, mode="action", drop_env=()):
        calls.append({"agent": agent.name, "message": message, "env": dict(env or {}), "mode": mode})
        return Reply(agent=agent.name, text=f"answer from {agent.name}", raw="", exit_code=0)

    monkeypatch.setattr("hail.hub.ask", fake_ask)
    store = Store(tmp_path / "hail.db")
    events = []

    def make(environ=None):
        return Hub(load_config(), store, environ or {}, notify=events.append)

    return make, store, calls, events


def test_user_ask_creates_thread_and_logs(setup):
    make, store, calls, events = setup
    out = make().consult(Request(message="deploy X", agent="hermes"))
    assert out.ok and out.hop == 1 and out.agent == "hermes" and out.text == "answer from hermes"
    assert calls[0]["mode"] == "action"
    assert calls[0]["env"] == {"HAIL_THREAD": out.thread, "HAIL_HOP": "1", "HAIL_SELF": "hermes"}
    assert "thread: " + out.thread in calls[0]["message"] and calls[0]["message"].endswith("deploy X")
    assert [m.kind for m in store.thread(out.thread)] == ["ask", "reply"]
    assert [e.kind for e in events] == ["ask", "reply"]


def test_agent_to_agent_uses_env_and_defaults_to_advice(setup):
    make, store, calls, _ = setup
    first = make().consult(Request(message="task", agent="hermes"))
    env = calls[0]["env"]
    out = make(env).consult(Request(message="stuck, advice?", agent="claude"))
    assert out.thread == first.thread and out.hop == 2
    assert calls[1]["mode"] == "advice"
    assert "from: hermes" in calls[1]["message"]
    assert store.thread(out.thread)[2].sender == "hermes"


def test_explicit_thread_and_sender_without_env(setup):
    make, store, calls, _ = setup
    first = make().consult(Request(message="task", agent="hermes"))
    out = make().consult(Request(message="help", agent="claude", thread=first.thread, sender="hermes"))
    assert out.hop == 2  # depth found in the journal


def test_declared_hop_can_only_raise_depth(setup):
    make, _, _, _ = setup
    out = make().consult(Request(message="help", agent="claude", thread="t-h", sender="hermes", hop=2))
    assert out.hop == 3
    out = make().consult(Request(message="help", agent="claude", thread="t-h", sender="hermes", hop=0))
    assert out.hop == 1  # declaring 0 adds nothing
    with pytest.raises(Refused, match="hop"):
        make().consult(Request(message="x", agent="claude", thread="t-h2", sender="hermes", hop=3))


def test_hop_limit_refused_and_logged(setup):
    make, store, _, events = setup
    env = {"HAIL_THREAD": "t-deep", "HAIL_HOP": "3", "HAIL_SELF": "claude"}
    with pytest.raises(Refused, match="hop"):
        make(env).consult(Request(message="again", agent="openclaw"))
    assert store.thread("t-deep")[-1].kind == "refused"
    assert events[-1].kind == "refused"


def test_thread_budget_and_stop(setup):
    make, store, _, _ = setup
    hub = make()
    out = hub.consult(Request(message="1", agent="claude"))
    for i in range(4):
        hub.consult(Request(message=str(i), agent="claude", thread=out.thread))
    with pytest.raises(Refused, match="budget"):
        hub.consult(Request(message="6", agent="claude", thread=out.thread))
    store.set_stopped("t-x", True)
    with pytest.raises(Refused, match="stopped"):
        hub.consult(Request(message="x", agent="claude", thread="t-x"))


def test_rate_limit_circuit_breaker(agents_file, setup):
    make, store, _, _ = setup
    agents_file(CONFIG.replace("max_calls_per_thread = 5", "rate_per_minute = 2"))
    hub = make()
    hub.consult(Request(message="a", agent="claude"))
    hub.consult(Request(message="b", agent="claude"))
    with pytest.raises(Refused, match="circuit"):
        hub.consult(Request(message="c", agent="claude"))


def test_policy_refuses_action_between_agents(setup):
    make, _, _, _ = setup
    env = {"HAIL_THREAD": "t-1", "HAIL_HOP": "1", "HAIL_SELF": "hermes"}
    with pytest.raises(Refused, match="action"):
        make(env).consult(Request(message="delete it", agent="claude", mode="action"))
    with pytest.raises(Refused, match="itself"):
        make(env).consult(Request(message="me?", agent="hermes"))


def test_route_by_skill_prefers_cheapest_and_skips_sender(setup):
    make, _, calls, _ = setup
    assert make().consult(Request(message="review", skill="code-review")).agent == "cheap"
    env = {"HAIL_THREAD": "t-2", "HAIL_HOP": "1", "HAIL_SELF": "cheap"}
    assert make(env).consult(Request(message="review", skill="code-review")).agent == "claude"
    with pytest.raises(HailError, match="no agent"):
        make().consult(Request(message="x", skill="painting"))


def test_continuation_sends_history(setup):
    make, _, calls, _ = setup
    hub = make()
    out = hub.consult(Request(message="first question", agent="claude"))
    hub.consult(Request(message="follow up", agent="claude", thread=out.thread))
    assert "first question" in calls[1]["message"] and "answer from claude" in calls[1]["message"]


def test_human_notes_are_forwarded(setup):
    make, store, calls, _ = setup
    store.add("t-n", 0, "human", "", "human", "", "use the staging server")
    make().consult(Request(message="go", agent="claude", thread="t-n"))
    assert "use the staging server" in calls[0]["message"]


def test_errors_are_logged(setup, monkeypatch):
    make, store, _, events = setup

    def boom(agent, message, env=None, mode="action", drop_env=()):
        raise HailError("down")

    monkeypatch.setattr("hail.hub.ask", boom)
    with pytest.raises(HailError):
        make().consult(Request(message="x", agent="claude", thread="t-e"))
    assert store.thread("t-e")[-1].kind == "error"
    assert events[-1].kind == "error"


def test_invalid_requests(setup):
    make, _, _, _ = setup
    hub = make()
    with pytest.raises(HailError, match="unknown agent"):
        hub.consult(Request(message="x", agent="ghost"))
    with pytest.raises(HailError, match="thread"):
        hub.consult(Request(message="x", agent="claude", thread="bad id!"))
    with pytest.raises(HailError, match="empty"):
        hub.consult(Request(message="  ", agent="claude"))
    with pytest.raises(HailError, match="agent or a skill"):
        hub.consult(Request(message="x"))


def test_notify_failure_never_breaks_a_call(setup, agents_file, tmp_path):
    _, store, _, _ = setup

    def broken(_event):
        raise RuntimeError("telegram down")

    hub = Hub(load_config(), store, {}, notify=broken)
    assert hub.consult(Request(message="x", agent="claude")).ok


def test_council_asks_all_and_synthesizes(setup):
    make, store, calls, events = setup
    result = make().council("which db?", ("claude", "hermes"), synth="openclaw")
    assert {a.agent for a in result.answers} == {"claude", "hermes"}
    assert result.synthesis is not None and result.synthesis.agent == "openclaw"
    assert "answer from claude" in calls[-1]["message"] and "answer from hermes" in calls[-1]["message"]
    assert events[0].kind == "council"


def test_council_reports_failures(setup, monkeypatch):
    make, _, _, _ = setup

    def flaky(agent, message, env=None, mode="action", drop_env=()):
        if agent.name == "hermes":
            raise HailError("timeout")
        return Reply(agent=agent.name, text="ok", raw="", exit_code=0)

    monkeypatch.setattr("hail.hub.ask", flaky)
    result = make().council("q", ("claude", "hermes"))
    failed = [a for a in result.answers if not a.ok]
    assert failed[0].agent == "hermes" and "timeout" in failed[0].text
    assert result.synthesis is None


def test_council_needs_two_agents(setup):
    make, _, _, _ = setup
    with pytest.raises(HailError, match="two"):
        make().council("q", ("claude",))
