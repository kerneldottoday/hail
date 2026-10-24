import json

import pytest

from hail import tasks, telegram
from hail.config import load_config
from hail.envelope import Event
from hail.hub import Hub, Outcome, Request
from hail.runner import HailError
from hail.store import Store

CONFIG = '[agents.claude]\npreset = "claude"\n\n[agents.hermes]\npreset = "hermes"\nhost = "vps"\n'


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "hail.db")


# --- tasks ----------------------------------------------------------------


def test_submit_records_and_spawns(store, monkeypatch):
    spawned = []
    monkeypatch.setattr(tasks, "spawn", spawned.append)
    task_id, thread = tasks.submit(store, Request(message="long job", agent="claude"), thread="t-1", sender="user")
    assert spawned == [task_id] and thread == "t-1"
    task = store.get_task(task_id)
    assert task.status == "submitted" and task.request["message"] == "long job"


class FakeHub:
    def __init__(self, result):
        self.result = result
        self.seen = []

    def consult(self, req):
        self.seen.append(req)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_run_task_completes(store):
    store.create_task("k-1", {"message": "m", "agent": "claude", "thread": "t-1", "sender": "hermes"})
    hub = FakeHub(Outcome("t-1", 2, "claude", "done", True))
    tasks.run(hub, store, "k-1")
    task = store.get_task("k-1")
    assert task.status == "completed" and task.result == "done"
    assert hub.seen[0].sender == "hermes" and hub.seen[0].thread == "t-1"


def test_run_task_failure(store):
    store.create_task("k-2", {"message": "m", "agent": "claude"})
    tasks.run(FakeHub(HailError("down")), store, "k-2")
    assert store.get_task("k-2").status == "failed" and "down" in store.get_task("k-2").error


def test_run_task_unknown_and_bad_request(store):
    with pytest.raises(HailError):
        tasks.run(FakeHub(None), store, "nope")
    store.create_task("k-3", {"bogus": 1})
    tasks.run(FakeHub(None), store, "k-3")
    assert store.get_task("k-3").status == "failed"


def test_describe_task(store):
    store.create_task("k-4", {"message": "m", "agent": "claude", "thread": "t-9"})
    text = tasks.describe(store.get_task("k-4"))
    assert "k-4" in text and "submitted" in text and "t-9" in text


def test_spawn_uses_detached_worker(monkeypatch):
    seen = {}

    class P:
        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw

    monkeypatch.setattr(tasks.subprocess, "Popen", P)
    tasks.spawn("k-9")
    assert seen["argv"][-2:] == ["_task", "k-9"]
    assert seen["kw"]["stdin"] is tasks.subprocess.DEVNULL


# --- telegram -------------------------------------------------------------


class FakeApi:
    def __init__(self, updates=()):
        self.calls = []
        self.updates = list(updates)

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "createForumTopic":
            return {"message_thread_id": 77}
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return batch
        return {}


def make_tg(store, api, **raw):
    cfg = telegram.parse_config({"chat_id": -100, "allowed_users": [5], **raw}, {"HAIL_TELEGRAM_TOKEN": "123:abc"})
    return telegram.Telegram(cfg, store, api=api)


def test_parse_config_reads_token_from_env_or_file(tmp_path):
    assert telegram.parse_config(None, {}) is None
    with pytest.raises(telegram.TelegramError, match="token"):
        telegram.parse_config({"chat_id": 1}, {})
    token_file = tmp_path / "tok"
    token_file.write_text("9:xyz\n", encoding="utf-8")
    cfg = telegram.parse_config({"chat_id": 1, "token_file": str(token_file)}, {})
    assert cfg.token == "9:xyz" and cfg.topics


@pytest.mark.parametrize("raw", [{"chat_id": "x"}, {"chat_id": 1, "allowed_users": ["a"]}, {"chat_id": 1, "x": 1}])
def test_parse_config_invalid(raw):
    with pytest.raises(telegram.TelegramError):
        telegram.parse_config(raw, {"HAIL_TELEGRAM_TOKEN": "t"})


def test_redact_hides_secrets():
    text = "key sk-ant-abcdefghijklmnop1234 and ghp_ABCDEFGHIJKLMNOPQRSTUV123 password=hunter2 ok"
    out = telegram.redact(text)
    assert "sk-ant-abc" not in out and "ghp_" not in out and "hunter2" not in out and "ok" in out


def test_notify_creates_topic_once_and_posts(store):
    api = FakeApi()
    tg = make_tg(store, api)
    ev = Event("t-1", 2, "hermes", "claude", "ask", "advice", "help me sk-ant-secretsecretsecret123")
    tg.notify(ev)
    tg.notify(Event("t-1", 2, "claude", "hermes", "reply", "advice", "do Y"))
    methods = [c[0] for c in api.calls]
    assert methods == ["createForumTopic", "sendMessage", "sendMessage"]
    sent = api.calls[1][1]
    assert sent["message_thread_id"] == 77 and sent["chat_id"] == -100
    assert "hermes → claude" in sent["text"] and "secretsecret" not in sent["text"]
    assert store.topic_of("t-1") == 77


def test_notify_without_topics_or_failed_topic(store):
    api = FakeApi()
    tg = make_tg(store, api, topics=False)
    tg.notify(Event("t-1", 1, "user", "claude", "ask", "action", "x"))
    assert [c[0] for c in api.calls] == ["sendMessage"] and "t-1" in api.calls[0][1]["text"]

    def failing(method, payload):
        if method == "createForumTopic":
            raise telegram.TelegramError("not a forum")
        api.calls.append((method, payload))
        return {}

    tg2 = make_tg(store, failing)
    tg2.notify(Event("t-2", 1, "user", "claude", "ask", "action", "x"))
    assert "message_thread_id" not in api.calls[-1][1]


def test_long_messages_are_truncated(store):
    api = FakeApi()
    make_tg(store, api, topics=False).notify(Event("t", 1, "a", "b", "reply", "advice", "x" * 9000))
    assert len(api.calls[0][1]["text"]) <= 4096


def update(uid, text, user=5, topic=None):
    msg = {"chat": {"id": -100}, "from": {"id": user}, "text": text}
    if topic:
        msg["message_thread_id"] = topic
    return {"update_id": uid, "message": msg}


def test_poll_handles_stop_note_and_ask(store, monkeypatch):
    store.set_topic("t-1", 77)
    submitted = []
    monkeypatch.setattr(telegram, "submit_task", lambda st, req, **kw: submitted.append(req) or ("k-1", "t-new"))
    api = FakeApi(
        [
            update(1, "/stop", topic=77),
            update(2, "use staging please", topic=77),
            update(3, "/ask claude review this"),
            update(4, "/stop", user=666, topic=77),  # stranger: ignored
            update(5, "/resume", topic=77),
        ]
    )
    tg = make_tg(store, api)
    assert tg.poll_once(timeout=0) == 4
    assert store.notes("t-1") == ("use staging please",)
    assert not store.is_stopped("t-1")  # stopped then resumed
    assert submitted[0].agent == "claude" and submitted[0].message == "review this"
    assert store.get_meta("telegram_offset") == "6"
    replies = [c[1]["text"] for c in api.calls if c[0] == "sendMessage"]
    assert any("stopped" in r for r in replies) and any("k-1" in r for r in replies)


def test_poll_requires_allowed_users(store):
    cfg = telegram.parse_config({"chat_id": -100}, {"HAIL_TELEGRAM_TOKEN": "t"})
    with pytest.raises(telegram.TelegramError, match="allowed_users"):
        telegram.Telegram(cfg, store, api=FakeApi()).poll_once(timeout=0)


def test_threads_command_and_bad_ask(store):
    store.add("t-1", 1, "user", "claude", "ask", "action", "x")
    api = FakeApi([update(1, "/threads"), update(2, "/ask"), update(3, "hello outside topics")])
    make_tg(store, api).poll_once(timeout=0)
    replies = [c[1]["text"] for c in api.calls if c[0] == "sendMessage"]
    assert "t-1" in replies[0] and "usage" in replies[1].lower()


def test_real_api_call_shape(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": {"x": 1}}).encode()

    def urlopen(req, timeout):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return Resp()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", urlopen)
    api = telegram.http_api("123:abc", timeout=5)
    assert api("sendMessage", {"chat_id": 1, "text": "hi"}) == {"x": 1}
    assert seen["url"].endswith("/bot123:abc/sendMessage") and seen["body"]["text"] == "hi"


def test_hub_notifies_telegram_end_to_end(agents_file, store, monkeypatch):
    agents_file(CONFIG)
    from hail.runner import Reply

    monkeypatch.setattr("hail.hub.ask", lambda a, m, env=None, mode="action", drop_env=(): Reply(a.name, "fine", "", 0))
    api = FakeApi()
    tg = make_tg(store, api)
    Hub(load_config(), store, {}, notify=tg.notify).consult(Request(message="q", agent="claude"))
    assert [c[0] for c in api.calls] == ["createForumTopic", "sendMessage", "sendMessage"]
