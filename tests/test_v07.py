"""hail 0.7: presets for OpenCode and Goose (message on stdin, plain text out, advice is only an instruction)."""

from hail.config import parse_agent
from hail.parsers import parse
from hail.presets import PRESETS
from hail.runner import advice_enforced, build


def test_opencode_reads_the_message_on_stdin():
    agent = parse_agent("oc", {"preset": "opencode", "bin": "nope-opencode"})
    inv = build(agent, "-x hi", mode="action")
    assert inv.argv == ("nope-opencode", "run") and inv.stdin == "-x hi"  # never an argument: not an option
    assert parse(agent.parser, "pong\n") == "pong"


def test_opencode_advice_is_an_instruction_not_a_sandbox():
    agent = parse_agent("oc", {"preset": "opencode", "bin": "nope-opencode"})
    assert build(agent, "hi", mode="advice").argv == ("nope-opencode", "run")
    assert not advice_enforced(agent)  # the plan agent obeys a global "*": "allow"


def test_goose_runs_once_quietly_without_a_session():
    agent = parse_agent("g", {"preset": "goose", "bin": "nope-goose"})
    inv = build(agent, "hi", mode="action")
    assert inv.argv == ("nope-goose", "run", "--no-session", "--quiet", "--instructions", "-")
    assert inv.stdin == "hi" and not advice_enforced(agent)


def test_new_presets_say_what_they_run():
    assert "opencode run" in PRESETS["opencode"].description
    assert "goose run" in PRESETS["goose"].description
