"""Regression tests for the v0.2 security review (identity, hops, atomic guards, secrets)."""

import os

import pytest

from hail import tasks, telegram
from hail.config import load_config, parse_agent
from hail.hub import ANONYMOUS, Hub, Refused, Request
from hail.runner import HailError, Reply, build
from hail.store import Store

CONFIG = """
[agents.hermes]
preset = "hermes"
host = "vps"

[agents.claude]
preset = "claude"

[agents.openclaw]
preset = "openclaw"

[policy]
max_calls_per_thread = 3
[policy.act]
hermes = ["openclaw"]
"""


@pytest.fixture
def env(agents_file, tmp_path, monkeypatch):
    agents_file(CONFIG)
    calls = []

    def fake_ask(agent, message, env=None, mode="action", drop_env=()):
        calls.append({"agent": agent.name, "message": message, "mode": mode, "drop": tuple(drop_env)})
        return Reply(agent.name, f"answer from {agent.name}", "", 0)

    monkeypatch.setattr("hail.hub.ask", fake_ask)
    store = Store(tmp_path / "hail.db")

    def make(environ=None, trust_user=True):
        return Hub(load_config(), store, environ or {}, trust_user=trust_user)

    return make, store, calls


def test_mcp_caller_without_identity_is_not_the_user(env):
    make, store, calls = env
    out = make(trust_user=False).consult(Request(message="x", agent="claude"))
    assert calls[0]["mode"] == "advice"
    assert store.thread(out.thread)[0].sender == ANONYMOUS
    with pytest.raises(Refused, match="action"):
        make(trust_user=False).consult(Request(message="x", agent="claude", mode="action"))
    out = make(trust_user=False).consult(Request(message="x", agent="claude", sender="user"))
    assert store.thread(out.thread)[0].sender == ANONYMOUS


def test_claimed_identity_cannot_ask_for_action(env):
    make, _, _ = env
    # hermes may act on openclaw, but a caller merely *claiming* to be hermes may not
    with pytest.raises(Refused, match="verified"):
        make(trust_user=False).consult(Request(message="x", agent="openclaw", sender="hermes", mode="action"))


def test_claimed_identity_verified_by_journal(env):
    make, _, _ = env
    first = make().consult(Request(message="task", agent="hermes"))
    out = make(trust_user=False).consult(
        Request(message="do it", agent="openclaw", sender="hermes", thread=first.thread, mode="action")
    )
    assert out.ok and out.hop == 2


def test_env_identity_may_act_when_policy_allows(env):
    make, _, _ = env
    environ = {"HAIL_SELF": "hermes", "HAIL_THREAD": "t-1", "HAIL_HOP": "1"}
    assert make(environ, trust_user=False).consult(Request(message="x", agent="openclaw", mode="action")).ok


def test_switching_thread_does_not_reset_hops(env):
    make, _, _ = env
    environ = {"HAIL_SELF": "claude", "HAIL_THREAD": "t-a", "HAIL_HOP": "3"}
    with pytest.raises(Refused, match="hop"):
        make(environ).consult(Request(message="x", agent="openclaw", thread="t-fresh"))


def test_budget_is_atomic_under_parallel_calls(env):
    make, store, _ = env
    hub = make()
    from concurrent.futures import ThreadPoolExecutor

    def one(_i):
        try:
            hub.consult(Request(message="x", agent="claude", thread="t-par"))
            return True
        except Refused:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(8)))
    assert sum(results) == 3
    assert store.count_asks("t-par") == 3


def test_telegram_token_never_reaches_agents(env, monkeypatch):
    make, _, calls = env
    make().consult(Request(message="x", agent="claude"))
    assert "HAIL_TELEGRAM_TOKEN" in calls[0]["drop"]
    monkeypatch.setenv("HAIL_TELEGRAM_TOKEN", "123:secret")
    agent = parse_agent("me", {"preset": "command", "command": "cat"})
    inv = build(agent, "hi", drop_env=("HAIL_TELEGRAM_TOKEN",))
    assert inv.env is not None and "HAIL_TELEGRAM_TOKEN" not in inv.env
    assert os.environ["HAIL_TELEGRAM_TOKEN"] == "123:secret"


def test_notes_are_truncated(env):
    make, store, calls = env
    store.add("t-n", 0, "human", "", "human", "", "y" * 5000)
    make().consult(Request(message="go", agent="claude", thread="t-n"))
    assert "y" * 600 not in calls[0]["message"]


def test_council_synthesis_prompt_is_bounded(env, monkeypatch):
    make, _, calls = env

    def big(agent, message, env=None, mode="action", drop_env=()):
        calls.append({"agent": agent.name, "message": message})
        return Reply(agent.name, "z" * 20000, "", 0)

    monkeypatch.setattr("hail.hub.ask", big)
    make().council("q", ("claude", "hermes"), synth="openclaw")
    assert len(calls[-1]["message"]) < 9000


def test_task_worker_never_stays_working(tmp_path):
    store = Store(tmp_path / "hail.db")
    store.create_task("k-1", {"message": "m", "agent": "claude"})

    class Boom:
        def consult(self, req):
            raise RuntimeError("database is locked")

    tasks.run(Boom(), store, "k-1")
    task = store.get_task("k-1")
    assert task.status == "failed" and "locked" in task.error


def test_too_many_active_tasks(tmp_path, monkeypatch):
    store = Store(tmp_path / "hail.db")
    monkeypatch.setattr(tasks, "spawn", lambda task_id: None)
    for _ in range(tasks.MAX_ACTIVE):
        tasks.submit(store, Request(message="m", agent="claude"), thread="t-1", sender="user")
    with pytest.raises(HailError, match="background"):
        tasks.submit(store, Request(message="m", agent="claude"), thread="t-1", sender="user")


def test_redact_more_secret_shapes():
    text = (
        '{"password": "p4ss word", "api_key": "abc"}\n'
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\n-----END OPENSSH PRIVATE KEY-----\n"
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl done"
    )
    out = telegram.redact(text)
    assert "p4ss" not in out and '"abc"' not in out and "AAAAB3Nza" not in out
    assert "eyJzdWIi" not in out and "done" in out


def test_poll_survives_a_failing_update(tmp_path, capsys):
    store = Store(tmp_path / "hail.db")
    cfg = telegram.parse_config({"chat_id": -100, "allowed_users": [5]}, {"HAIL_TELEGRAM_TOKEN": "t"})
    sent = []

    def api(method, payload):
        if method == "getUpdates":
            return [
                {"update_id": 1, "message": {"chat": {"id": -100}, "from": {"id": 5}, "text": "/threads"}},
                {"update_id": 2, "message": {"chat": {"id": -100}, "from": {"id": 5}, "text": "/threads"}},
            ]
        if not sent:
            sent.append("boom")
            raise RuntimeError("send failed")
        sent.append(payload["text"])
        return {}

    assert telegram.Telegram(cfg, store, api=api).poll_once(timeout=0) == 2
    assert len(sent) == 2 and "send failed" in capsys.readouterr().err
