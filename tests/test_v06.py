"""hail 0.6: idempotent `hail send --ref`, and `[policy] cli_access` for agents that run hail."""

import json

import pytest

from hail import cli, mcp, tasks
from hail.runner import Reply
from hail.store import default_store

CONFIG = """
[agents.claude]
preset = "claude"

[agents.hermes]
preset = "hermes"
host = "vps"
"""


@pytest.fixture
def spawned(agents_file, monkeypatch):
    agents_file(CONFIG)
    monkeypatch.setattr("hail.hub.ask", lambda agent, *a, **k: Reply(agent.name, f"ok from {agent.name}", "", 0))
    started: list[str] = []
    monkeypatch.setattr(tasks, "spawn", started.append)
    return started


def policy(agents_file, access: str) -> None:
    agents_file(CONFIG + f'\n[policy]\ncli_access = "{access}"\n')


def call(name, arguments):
    line = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    return mcp.handle_line(line)["result"]


def send_json(capsys, *argv) -> dict:
    assert cli.main(["send", *argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


# send --ref --------------------------------------------------------------------------------


def test_same_ref_returns_the_same_task_and_starts_one_worker(spawned, capsys):
    first = send_json(capsys, "claude", "--ref", "build-42", "run the build")
    again = send_json(capsys, "claude", "--ref", "build-42", "run the build")
    assert again["id"] == first["id"] and first["reused"] is False and again["reused"] is True
    assert first["ref"] == "build-42" and spawned == [first["id"]]


def test_without_ref_every_send_is_a_new_task(spawned, capsys):
    a = send_json(capsys, "claude", "job")
    b = send_json(capsys, "claude", "job")
    assert a["id"] != b["id"] and "ref" not in a and len(spawned) == 2


def test_same_ref_for_another_request_is_a_usage_error(spawned, capsys):
    send_json(capsys, "claude", "--ref", "r1", "one thing")
    assert cli.main(["send", "claude", "--ref", "r1", "another thing"]) == 2
    assert cli.main(["send", "hermes", "--ref", "r1", "one thing"]) == 2
    assert "r1" in capsys.readouterr().err and len(spawned) == 1


def test_invalid_ref_is_a_usage_error(spawned):
    assert cli.main(["send", "claude", "--ref", "bad ref!", "x"]) == 2
    assert cli.main(["send", "claude", "--ref", "x" * 65, "x"]) == 2
    assert spawned == []


def test_a_failed_task_can_be_retried_with_the_same_ref(spawned, capsys):
    first = send_json(capsys, "claude", "--ref", "r2", "job")
    default_store().update_task(first["id"], "failed", error="boom")
    again = send_json(capsys, "claude", "--ref", "r2", "job")
    assert again["id"] != first["id"] and again["reused"] is False and len(spawned) == 2


def test_a_reused_ref_does_not_count_against_the_active_task_limit(spawned, capsys, monkeypatch):
    first = send_json(capsys, "claude", "--ref", "r3", "job")
    monkeypatch.setattr(tasks, "MAX_ACTIVE", 1)
    assert send_json(capsys, "claude", "--ref", "r3", "job")["id"] == first["id"]
    assert cli.main(["send", "claude", "other"]) == 75


def test_status_json_shows_the_ref(spawned, capsys):
    task_id = send_json(capsys, "claude", "--ref", "r4", "job")["id"]
    assert cli.main(["status", task_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ref"] == "r4"


def test_prune_frees_refs_of_deleted_tasks(spawned, capsys):
    store = default_store()
    first = send_json(capsys, "claude", "--ref", "r5", "job")
    store.update_task(first["id"], "completed", result="done")
    with store._db() as db:
        db.execute("UPDATE tasks SET updated = 0 WHERE id = ?", (first["id"],))
    store.prune(1)
    assert store.get_task(first["id"]) is None
    assert send_json(capsys, "claude", "--ref", "r5", "a new job")["reused"] is False


def test_mcp_send_with_ref(spawned):
    one = call("hail_send", {"agent": "claude", "message": "job", "ref": "m1"})
    two = call("hail_send", {"agent": "claude", "message": "job", "ref": "m1"})
    task_id = one["content"][0]["text"].split()[1]
    assert not two["isError"] and task_id in two["content"][0]["text"] and "already" in two["content"][0]["text"]
    assert len(spawned) == 1


# [policy] cli_access ---------------------------------------------------------------------------


@pytest.fixture
def as_agent(monkeypatch):
    monkeypatch.setenv("HAIL_SELF", "hermes")


@pytest.mark.parametrize(
    "argv",
    [
        ["claude", "hi"],
        ["ask", "claude", "hi"],
        ["send", "claude", "hi"],
        ["council", "--agents", "claude,hermes", "q"],
        ["stop", "t-1"],
        ["resume", "t-1"],
        ["prune", "--days", "3"],
        ["inbox", "clear"],
        ["inbox", "read", "hermes", "x"],
        ["add", "x", "--preset", "claude"],
        ["init"],
        ["telegram", "test"],
    ],
)
def test_read_access_refuses_what_writes_or_sends(spawned, agents_file, as_agent, capsys, argv):
    policy(agents_file, "read")
    assert cli.main(argv) == 7
    assert "cli_access" in capsys.readouterr().err
    assert spawned == []


@pytest.mark.parametrize("argv", [["list"], ["log"], ["threads"], ["status", "k-none"], ["help", "agents"]])
def test_read_access_lets_agents_look(spawned, agents_file, as_agent, argv):
    policy(agents_file, "read")
    assert cli.main(argv) in (0, 5)  # 5: the task does not exist, but the lookup was allowed


def test_none_access_refuses_everything_but_help(spawned, agents_file, as_agent):
    policy(agents_file, "none")
    assert cli.main(["list"]) == 7
    assert cli.main(["log"]) == 7
    assert cli.main(["help", "agents"]) == 0
    assert cli.main(["--version"]) == 0


def test_cli_access_does_not_apply_to_you(spawned, agents_file):
    policy(agents_file, "none")
    assert cli.main(["claude", "hi"]) == 0
    assert cli.main(["list"]) == 0


def test_cli_access_default_is_all(spawned, as_agent):
    assert cli.main(["claude", "hi"]) == 0


def test_invalid_cli_access_is_a_config_error(agents_file):
    policy(agents_file, "write")
    assert cli.main(["list"]) == 3


def test_mcp_read_access(spawned, agents_file):
    policy(agents_file, "read")
    refused = call("hail_ask", {"agent": "claude", "message": "hi"})
    assert refused["isError"] and "cli_access" in refused["content"][0]["text"]
    assert call("hail_send", {"agent": "claude", "message": "hi"})["isError"]
    assert call("hail_inbox_read", {"agent": "hermes", "id": "x"})["isError"]
    assert not call("hail_list", {})["isError"]
    assert spawned == []


def test_mcp_none_access(spawned, agents_file):
    policy(agents_file, "none")
    assert call("hail_list", {})["isError"]


def test_help_agents_tells_an_agent_its_access(spawned, agents_file, as_agent, capsys):
    policy(agents_file, "read")
    assert cli.main(["help", "agents"]) == 0
    assert "cli_access = read" in capsys.readouterr().out


# review fixes ---------------------------------------------------------------------------


def test_an_agent_can_start_the_mcp_server_under_read_and_none(spawned, agents_file, as_agent, monkeypatch):
    monkeypatch.setattr("hail.mcp.serve", lambda: 0)
    for access in ("read", "none"):
        policy(agents_file, access)
        assert cli.main(["mcp"]) == 0  # each tool is checked on its own


@pytest.mark.parametrize(
    "argv",
    [
        ["inbox", "--json", "read", "hermes", "x"],
        ["inbox", "--all", "clear"],
        ["inbox", "--tz", "Europe/Paris", "clear", "hermes"],
    ],
)
def test_inbox_writes_are_refused_whatever_the_option_order(spawned, agents_file, as_agent, argv):
    policy(agents_file, "read")
    assert cli.main(argv) == 7


def test_a_ref_with_a_trailing_newline_is_invalid(spawned):
    assert cli.main(["send", "claude", "--ref", "abc\n", "x"]) == 2


def test_a_ref_conflict_does_not_reveal_the_other_task(spawned, capsys):
    first = send_json(capsys, "claude", "--ref", "r6", "one")
    assert cli.main(["send", "claude", "--ref", "r6", "two"]) == 2
    assert first["id"] not in capsys.readouterr().err


def test_a_task_whose_worker_could_not_start_frees_its_ref(agents_file, monkeypatch, capsys):
    agents_file(CONFIG)

    def broken(task_id):
        raise OSError("no python")

    monkeypatch.setattr(tasks, "spawn", broken)
    assert cli.main(["send", "claude", "--ref", "r7", "job"]) == 6
    started: list[str] = []
    monkeypatch.setattr(tasks, "spawn", started.append)
    capsys.readouterr()
    again = send_json(capsys, "claude", "--ref", "r7", "job")
    assert again["reused"] is False and started == [again["id"]]


def test_a_task_runs_once_even_if_started_by_hand(spawned, capsys, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "hail.hub.ask", lambda agent, *a, **k: calls.append(agent.name) or Reply(agent.name, "ok", "", 0)
    )
    task_id = send_json(capsys, "claude", "job")["id"]
    assert cli.main(["_task", task_id]) == 0
    assert cli.main(["_task", task_id]) == 0  # already done: nothing runs again
    assert calls == ["claude"]


def test_the_worker_of_an_allowed_send_is_not_held_to_cli_access(spawned, agents_file, as_agent, capsys):
    task_id = send_json(capsys, "claude", "job")["id"]  # cli_access = all when sent
    policy(agents_file, "read")  # tightened before the worker starts
    assert cli.main(["_task", task_id]) == 0
    assert default_store().get_task(task_id).status == "completed"
