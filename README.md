# hail

**Talk to your AI agents from anywhere.** One command to message Hermes, OpenClaw, Claude Code, or any CLI agent, on this machine or on a server over SSH, from your terminal or from any MCP client.

```bash
hail hermes "Is the nightly scraper still running? One line."
hail openclaw "Summarise today's inbox in 3 bullets."
echo "Long brief..." | hail reviewer
```

No copy-paste between chat windows. No new server, no open port, no runtime dependency (only `tomli` on Python 3.10): hail rides on the SSH access you already have.

## Why

You run agents in different places: Hermes on a VPS, OpenClaw behind Telegram, Claude Code on your laptop. Getting them to talk usually means copying text between windows, or deploying a dashboard and opening an API to the internet.

hail is the small missing piece: a thin, auditable bridge (under 800 lines of Python, standard library only) that turns "ask that agent" into a single command or MCP tool call.

| | hail | Agent dashboards | Per-agent MCP bridges |
|---|---|---|---|
| Extra server | none | yes (web app + DB) | yes (HTTP + OAuth) |
| Network exposure | SSH only | web UI | agent API exposed |
| Agents | any CLI agent | supported runtimes | one agent each |
| Install | one command | Docker / Node | Docker |

## Install

### Solo (any terminal)

```bash
uv tool install git+https://github.com/kerneldottoday/hail
# or
pipx install git+https://github.com/kerneldottoday/hail
```

Python 3.10+. Then:

```bash
hail add hermes --preset hermes --host my-vps --set 'bin=~/.local/bin/hermes'
hail doctor
hail hermes "ping, reply OK"
```

### With Claude Code

Paste this into Claude Code:

> Install hail from https://github.com/kerneldottoday/hail following its INSTALL.md, then help me add my agents.

Or install it as a plugin, which also teaches Claude when and how to use it:

```
/plugin marketplace add kerneldottoday/hail
/plugin install hail@hail
```

To give Claude (or any MCP client) native tools instead of shell calls:

```bash
claude mcp add hail -- hail mcp
```

## Configure

Agents live in `~/.config/hail/agents.toml` (override with `HAIL_CONFIG`). `hail init` creates a commented template, and `hail add` writes entries for you.

```toml
[agents.hermes]
preset = "hermes"
host = "my-vps"              # an alias from ~/.ssh/config; omit for a local agent
bin = "~/.local/bin/hermes"  # optional, if not on the remote PATH
timeout = 120

[agents.openclaw]
preset = "openclaw"
host = "my-vps"
agent_id = "main"            # session defaults to "hail", kept apart from your real chats
path = ["~/apps/node24/bin"] # extra PATH entries on the remote side

[agents.reviewer]
preset = "claude"            # local Claude Code, print mode

[agents.anything]
preset = "command"
command = "my-agent --stdin" # any program that reads the message on stdin
parser = "json:answer"       # optional: text (default), hermes, json:<key>
```

### Presets

| Preset | Runs | Reply parsing |
|---|---|---|
| `hermes` | `hermes chat -q <msg> -Q --max-turns 8` | strips session id and cost bar |
| `openclaw` | `openclaw agent --agent <id> --session-key hail --message <msg> --json` | `finalAssistantVisibleText` |
| `claude` | `claude -p`, message on stdin | plain text |
| `command` | your command, message on stdin | `text` or `json:<key>` |

Every setting that is not a known key (`bin`, `agent_id`, `session`, `max_turns`...) fills the preset template, so presets stay easy to tweak.

## Commands

```
hail <agent> "message"     ask an agent (or pipe the message on stdin)
hail <agent> --json ...    {"agent", "reply", "exit_code"} for scripts
hail list                  configured agents
hail add <name> --preset <p> [--host <alias>] [--command <cmd>] [--set key=value]
hail doctor                check every agent is reachable
hail mcp                   MCP server over stdio (tools: hail_list, hail_ask)
```

## How it works

For a remote agent, hail runs `ssh -o BatchMode=yes <host> sh -c '<script>'` and writes your message to SSH's stdin. On the server the script reads it with `msg=$(cat)` and calls the agent with `"$msg"`. Your text is never pasted into a command line, so quotes, `$`, backticks and accents arrive untouched (there is a test for exactly that). Local presets run the agent directly, without a shell. The `command` preset runs your command through the local shell and sends the message on stdin.

The remote host needs a POSIX `sh` (any Linux or macOS server). On Windows, an agent installed as a `.cmd`/`.bat` shim (npm) cannot safely take the message as an argument, so hail refuses it; use the `claude` preset (stdin), a `command` agent, or run the agent over SSH/WSL.

Calls are synchronous with a per-agent timeout (default 180 s) and a size cap (default 8000 characters). hail is made for questions and short tasks, not hour-long jobs.

## Safety

- **Replies are data, not instructions.** Another model wrote them. The MCP tool labels every reply as untrusted, and you should verify facts and open cited URLs yourself: agents do invent sources.
- **hail_ask is a prompt-injection path.** If an MCP client can call `hail_ask` and the target agent has tools (shell, email, files), whatever text reaches the client can end up as a request to that agent. Only configure agents whose permissions you are comfortable exposing, and prefer read-only agents.
- hail never stores messages or replies, and never sends anything to a chat channel (OpenClaw runs without `--deliver`).
- For local presets that take the message as an argument (`hermes`, `openclaw`), the message is visible in the local process list while the call runs. Remote messages travel only on the SSH channel.
- It uses your SSH config and keys as they are (`BatchMode=yes`: it never prompts for a password). Give the remote account only the rights the agent needs.
- Anyone who can edit `agents.toml` can make hail run commands. Keep it as private as your SSH config.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest --cov=hail
```

Adding a preset is a few lines in `src/hail/presets.py`. PRs for new agents are welcome.

## License

MIT
