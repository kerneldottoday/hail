import time

import pytest

from hail import envelope
from hail.policy import Policy, PolicyError, parse_policy
from hail.store import Store

# --- envelope -------------------------------------------------------------


def test_new_thread_id_is_valid_and_unique():
    a, b = envelope.new_thread_id(), envelope.new_thread_id()
    assert a != b
    assert envelope.valid_thread(a)


@pytest.mark.parametrize("value", ["", "a b", "x" * 41, "t-1;rm", "../x"])
def test_invalid_thread_ids(value):
    assert not envelope.valid_thread(value)


def test_from_env_reads_identity():
    env = {"HAIL_THREAD": "t-abc", "HAIL_HOP": "2", "HAIL_SELF": "hermes"}
    assert envelope.from_env(env) == envelope.Caller(name="hermes", thread="t-abc", hop=2)


def test_from_env_ignores_garbage():
    assert envelope.from_env({}) == envelope.Caller()
    bad = {"HAIL_THREAD": "no spaces allowed", "HAIL_HOP": "x", "HAIL_SELF": "Bad Name"}
    assert envelope.from_env(bad) == envelope.Caller()


def test_from_env_identity_without_thread():
    # an MCP client config may declare HAIL_SELF while the other variables stay unset placeholders
    env = {"HAIL_SELF": "hermes", "HAIL_THREAD": "${HAIL_THREAD}", "HAIL_HOP": "${HAIL_HOP}"}
    assert envelope.from_env(env) == envelope.Caller(name="hermes")


def test_callee_env_round_trip():
    ctx = envelope.Context(thread="t-1", hop=2, sender="hermes", recipient="claude", mode="advice")
    env = envelope.callee_env(ctx)
    assert env == {"HAIL_THREAD": "t-1", "HAIL_HOP": "2", "HAIL_SELF": "claude"}
    assert envelope.from_env(env) == envelope.Caller(name="claude", thread="t-1", hop=2)


def test_header_mentions_thread_hop_and_mode():
    ctx = envelope.Context(thread="t-1", hop=2, sender="hermes", recipient="claude", mode="advice", max_hops=3)
    text = envelope.compose(ctx, "how do I fix X?", history=(), notes=())
    assert "from: hermes" in text and "thread: t-1" in text and "hop 2/3" in text
    assert "advice" in text and text.rstrip().endswith("how do I fix X?")


def test_compose_includes_history_and_notes():
    ctx = envelope.Context(thread="t-1", hop=1, sender="user", recipient="claude", mode="action")
    text = envelope.compose(ctx, "next", history=(("claude", "earlier answer"),), notes=("be brief",))
    assert "earlier answer" in text and "be brief" in text


def test_compose_trims_history_to_budget():
    ctx = envelope.Context(thread="t-1", hop=1, sender="user", recipient="claude", mode="action")
    history = tuple(("claude", "x" * 500) for _ in range(20))
    text = envelope.compose(ctx, "q", history=history, notes=(), budget=1500)
    assert len(text) <= 1500 + 200


# --- store ----------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "hail.db")


def test_store_messages_and_depth(store):
    store.add("t-1", 1, "user", "hermes", "ask", "advice", "do X")
    store.add("t-1", 2, "hermes", "claude", "ask", "advice", "help?")
    store.add("t-1", 2, "claude", "hermes", "reply", "advice", "use Y")
    assert store.depth_of("t-1", "hermes") == 1
    assert store.depth_of("t-1", "claude") == 2
    assert store.depth_of("t-1", "nobody") == 0
    assert store.count_asks("t-1") == 2
    assert [m.kind for m in store.thread("t-1")] == ["ask", "ask", "reply"]


def test_store_history_between_two_agents(store):
    store.add("t-1", 1, "hermes", "claude", "ask", "advice", "q1")
    store.add("t-1", 1, "claude", "hermes", "reply", "advice", "a1")
    store.add("t-1", 1, "hermes", "openclaw", "ask", "advice", "other")
    assert store.history("t-1", "claude") == (("hermes", "q1"), ("claude", "a1"))


def test_store_rate_and_search(store):
    store.add("t-1", 1, "user", "a", "ask", "advice", "alpha beta")
    store.add("t-2", 1, "user", "b", "ask", "advice", "gamma")
    assert store.asks_since(time.time() - 60) == 2
    assert [m.text for m in store.recent(10, grep="beta")] == ["alpha beta"]
    assert len(store.recent(1)) == 1
    assert {t.thread for t in store.threads(10)} == {"t-1", "t-2"}


def test_store_stop_notes_topics_meta(store):
    store.ensure_thread("t-1", "title")
    assert not store.is_stopped("t-1")
    store.set_stopped("t-1", True)
    assert store.is_stopped("t-1")
    store.add("t-1", 0, "human", "", "human", "", "careful")
    assert store.notes("t-1") == ("careful",)
    store.set_topic("t-1", 42)
    assert store.topic_of("t-1") == 42 and store.thread_of_topic(42) == "t-1"
    store.set_meta("offset", "7")
    assert store.get_meta("offset") == "7" and store.get_meta("nope") is None


def test_store_tasks(store):
    store.create_task("k-1", {"agent": "claude", "message": "m"})
    task = store.get_task("k-1")
    assert task.status == "submitted" and task.request["agent"] == "claude"
    store.update_task("k-1", "completed", result="done")
    assert store.get_task("k-1").result == "done"
    assert store.get_task("missing") is None


# --- policy ---------------------------------------------------------------


def test_default_policy_allows_advice_not_action():
    p = Policy()
    assert p.check("hermes", "claude", "advice") is None
    assert "action" in p.check("hermes", "claude", "action")
    assert p.check("user", "claude", "action") is None


def test_policy_talk_and_act_lists():
    p = parse_policy({"max_hops": 2, "talk": {"hermes": ["claude"]}, "act": {"hermes": ["openclaw"]}})
    assert p.max_hops == 2
    assert p.check("hermes", "claude", "advice") is None
    assert p.check("hermes", "openclaw", "advice") is None  # act implies talk
    assert p.check("hermes", "openclaw", "action") is None
    assert "action" in p.check("hermes", "claude", "action")
    assert p.check("hermes", "relay", "advice") is not None
    assert p.check("openclaw", "anyone", "advice") is None  # no talk entry = free to consult


def test_policy_self_call_refused():
    assert "itself" in Policy().check("claude", "claude", "advice")


@pytest.mark.parametrize(
    "raw",
    [{"max_hops": 0}, {"max_hops": "3"}, {"talk": []}, {"talk": {"a": "b"}}, {"unknown": 1}],
)
def test_policy_invalid(raw):
    with pytest.raises(PolicyError):
        parse_policy(raw)
