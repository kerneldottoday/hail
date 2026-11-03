"""hail 0.6.1: an agent's own status line (`footer`) stays out of the reply; only you see it."""

import json

import pytest
from conftest import FakeProc

from hail import cli, mcp, parsers
from hail.config import ConfigError, parse_agent
from hail.errors import AgentUnusable
from hail.parsers import split_footer
from hail.runner import ask
from hail.store import default_store

# The line Hermes' footer-agents plugin adds under every answer (thin spaces around the dots).
SEP = " · "
LINE = SEP.join(["v4-pro", "1 appel", "$0.025", "jour $0.02", "solde $4.49", "ctx 18k/1M"])
NO_TURN = SEP.join(["jour $0.12", "solde ?"])
ALERT = "⚠ " + SEP.join(["v4-pro", "12 appels", "$0.310", "jour $0.40", "solde $4.10"])
HERMES_FOOTER = r"^(⚠\s)?(\S+\s·\s\d+\sappels?\s·\s\$[\d.]+\s·\s)?jour\s\$[\d.]+\s·\ssolde\s"

CONFIG = f"""
[agents.hermes]
preset = "hermes"
host = "vps"
footer = '{HERMES_FOOTER}'

[agents.claude]
preset = "claude"
"""


# split_footer -------------------------------------------------------------------------------


@pytest.mark.parametrize("line", [LINE, NO_TURN, ALERT])
def test_the_hermes_status_line_is_moved_out_of_the_reply(line):
    assert split_footer(f"OK, done.\n\n{line}", HERMES_FOOTER) == ("OK, done.", line)


def test_without_a_pattern_nothing_changes():
    assert split_footer(f"OK\n\n{LINE}", None) == (f"OK\n\n{LINE}", "")


def test_only_trailing_lines_are_taken():
    text = f"{NO_TURN} is what the bar says\nbut this answer goes on"
    assert split_footer(text, HERMES_FOOTER) == (text, "")


def test_several_trailing_lines_and_blank_lines_between():
    body, footer = split_footer(f"answer\n\n{ALERT}\n\n{LINE}\n", HERMES_FOOTER)
    assert body == "answer" and footer == f"{ALERT}\n{LINE}"


def test_at_most_a_few_lines_are_taken():
    body, footer = split_footer("a\n" + "x\n" * 8, r"^x$")
    assert footer.count("x") == parsers.MAX_FOOTER_LINES and body == "a\n" + "\n".join("x" * 3)


# config -----------------------------------------------------------------------------------------


def test_footer_is_a_setting_not_a_template_variable():
    agent = parse_agent("h", {"preset": "hermes", "footer": HERMES_FOOTER})
    assert agent.footer == HERMES_FOOTER and "footer" not in agent.variables


@pytest.mark.parametrize(
    "value, why",
    [
        ("(unclosed", "not a valid regular expression"),
        (".*", "empty line"),
        ("", "regular expression"),
        (3, "regular expression"),
        ("x" * 301, "regular expression"),
    ],
)
def test_bad_footer_patterns_are_refused(value, why):
    with pytest.raises(ConfigError, match=why):
        parse_agent("h", {"preset": "hermes", "footer": value})


def test_hail_add_checks_the_pattern(agents_file):
    agents_file("")
    assert cli.main(["add", "x", "--preset", "claude", "--set", "footer=.*"]) == 3


# runner -----------------------------------------------------------------------------------------


def test_ask_returns_the_footer_apart(fake_run):
    _, state = fake_run
    state["proc"] = FakeProc(f"OK\n\n{LINE}\n".encode())
    reply = ask(parse_agent("h", {"preset": "hermes", "host": "vps", "footer": HERMES_FOOTER}), "x")
    assert reply.text == "OK" and reply.footer == LINE


def test_a_reply_that_is_only_a_footer_is_no_reply(fake_run):
    _, state = fake_run
    state["proc"] = FakeProc(f"\n{LINE}\n".encode())
    with pytest.raises(AgentUnusable, match="footer") as info:
        ask(parse_agent("h", {"preset": "hermes", "host": "vps", "footer": HERMES_FOOTER}), "x")
    assert info.value.exit_code == 6


# who sees it --------------------------------------------------------------------------------------


@pytest.fixture
def hermes(agents_file, fake_run):
    agents_file(CONFIG)
    _, state = fake_run
    state["proc"] = FakeProc(f"OK\n\n{LINE}\n".encode())


def test_you_see_it_on_stderr_and_the_journal_keeps_the_reply_only(hermes, capsys):
    assert cli.main(["hermes", "hi"]) == 0
    out = capsys.readouterr()
    assert out.out == "OK\n" and "solde $4.49" in out.err
    replies = [m.text for m in default_store().recent(5) if m.kind == "reply"]
    assert replies == ["OK"]


def test_json_gives_it_as_a_field(hermes, capsys):
    assert cli.main(["hermes", "--json", "hi"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply"] == "OK" and payload["footer"] == LINE


def test_another_agent_never_gets_it(hermes, capsys, monkeypatch):
    monkeypatch.setenv("HAIL_SELF", "claude")
    assert cli.main(["hermes", "--json", "hi"]) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert "footer" not in payload and "solde" not in out.out + out.err


def test_mcp_callers_never_get_it(hermes):
    line = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "hail_ask", "arguments": {"agent": "hermes", "message": "hi"}},
        }
    )
    text = mcp.handle_line(line)["result"]["content"][0]["text"]
    assert "OK" in text and "solde" not in text


def test_council_json_keeps_each_footer_apart(hermes, capsys):
    assert cli.main(["council", "--json", "--agents", "hermes,claude", "q"]) == 0
    answers = {a["agent"]: a for a in json.loads(capsys.readouterr().out)["answers"]}
    assert answers["hermes"]["reply"] == "OK" and answers["hermes"]["footer"] == LINE
    assert "footer" not in answers["claude"] and "solde" in answers["claude"]["reply"]  # no pattern set for claude


def test_the_body_is_kept_byte_for_byte():
    text = "a b\rc\r\nd\n\n" + LINE + "\r\n"
    assert split_footer(text, HERMES_FOOTER) == ("a b\rc\r\nd", LINE)


def test_long_lines_are_never_tried(monkeypatch):
    long_line = "jour $1 · solde " + "x" * 600
    assert split_footer(f"ok\n{long_line}", r"jour") == (f"ok\n{long_line}", "")


def test_footer_only_on_a_failed_run_is_still_no_reply(fake_run):
    _, state = fake_run
    state["proc"] = FakeProc(f"{LINE}\n".encode(), b"", 1)
    with pytest.raises(AgentUnusable):
        ask(parse_agent("h", {"preset": "hermes", "host": "vps", "footer": HERMES_FOOTER}), "x")


def test_council_text_shows_footers_on_stderr(hermes, capsys):
    assert cli.main(["council", "--agents", "hermes,claude", "q"]) == 0
    out = capsys.readouterr()
    assert "solde $4.49" in out.err and "== hermes\nOK\n" in out.out


def test_the_readme_example_works():
    agent = parse_agent("h", {"preset": "hermes", "footer": r"^cost: \$[\d.]+ · balance: "})
    assert split_footer("All good.\ncost: $0.025 · balance: $4.49", agent.footer) == (
        "All good.",
        "cost: $0.025 · balance: $4.49",
    )


def test_the_footer_shown_to_you_is_stripped_of_terminal_escapes(agents_file, fake_run, capsys):
    agents_file('[agents.loc]\npreset = "command"\ncommand = "x"\nfooter = "^cost:"\n')
    _, state = fake_run
    state["proc"] = FakeProc(b"answer\ncost: 1\x1b]0;pwned\x07\n")
    assert cli.main(["loc", "hi"]) == 0
    err = capsys.readouterr().err
    assert "cost: 1" in err and "\x1b" not in err
