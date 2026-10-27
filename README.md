# hail

**Talk to your AI agents from anywhere, and let them talk to each other.** One command to message Hermes, OpenClaw, Claude Code, Codex, Gemini CLI, a local Ollama model or any CLI agent, on this machine or on a server over SSH, from your terminal or from any MCP client. When an agent is stuck, it can consult another one itself, with loop guards, a journal and an optional Telegram channel to watch it happen.

```bash
hail hermes "Is the nightly scraper still running? One line."
hail openclaw "Summarise today's inbox in 3 bullets."
echo "Long brief..." | hail reviewer
```

No copy-paste between chat windows. No new server, no open port, no runtime dependency (only `tomli` on Python 3.10): hail rides on the SSH access you already have.

## Why

You run agents in different places: Hermes on a VPS, OpenClaw behind Telegram, Claude Code on your laptop. Getting them to talk usually means copying text between windows, or deploying a dashboard and opening an API to the internet.

hail is the small missing piece: a thin, auditable bridge (about 3,000 lines of Python, standard library only) that turns "ask that agent" into a single command or MCP tool call, for you and for your agents.

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

Python 3.10+. The PyPI distribution name is `hail-agent`; it is unrelated to the
[Hail](https://hail.is) genomics library, which also imports as `hail`. Install with
`uv tool` or `pipx` (isolated environments) so the two never collide. Then:

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
| `codex` | `codex exec --skip-git-repo-check -`, message on stdin | plain text (final message) |
| `gemini` | `gemini` headless, message on stdin | plain text |
| `ollama` | `ollama run <model>` (default `llama3.2`), message on stdin | plain text |
| `command` | your command, message on stdin | `text` or `json:<key>` |

In `advice` mode, `claude` runs with `--permission-mode plan` and `codex` with `--sandbox read-only`. Gemini's plan mode switches to auto-approval once the plan is done in headless runs, so hail does not use it: for `gemini`, advice is an instruction, not a sandbox. An `ollama` model has no tools at all, which makes it a cheap, safe second opinion.

Every setting that is not a known key (`bin`, `agent_id`, `session`, `max_turns`...) fills the preset template, so presets stay easy to tweak.

## Commands

```
hail <agent> "message"     ask an agent (or pipe the message on stdin)
    [--json] [--thread T] [--mode advice|action] [--from NAME] [--hop N]
hail ask --skill S "msg"   ask the cheapest agent that has skill S
hail send <agent> "msg"    background task, prints its id
hail status <task>         state and result of a background task
hail council --agents a,b [--synth c] "question"   several opinions, one synthesis
hail log [-n N] [--grep X] recent messages between agents
hail threads | thread <id> list threads | show one thread
hail stop | resume <id>    block or reopen a thread
hail telegram test | watch check the Telegram channel | take commands from it
hail jobs [agent] [--all]  what agents have scheduled (next run in UTC + local time)
hail inbox [agent] [--all] what agents delivered since you last looked
hail inbox read <agent> <id> | clear [agent]
hail prune --days N        delete journal entries older than N days
hail list                  configured agents (with skills and cost)
hail add <name> --preset <p> [--host <alias>] [--command <cmd>] [--set key=value]
hail doctor                check every agent is reachable
hail mcp                   MCP server over stdio
```

MCP tools: `hail_list`, `hail_ask`, `hail_send`, `hail_status`, `hail_council`, `hail_thread`, `hail_jobs`, `hail_inbox`, `hail_inbox_read`.

## Agents talking to each other

Install hail where your agents run and add `hail mcp` to each agent's MCP servers. Hermes, for example:

```bash
hermes mcp add hail --command ~/.local/bin/hail --args mcp
```

and in `~/.hermes/config.yaml`, give hail the agent's identity (Hermes filters the environment of MCP servers):

```yaml
  hail:
    command: /home/me/.local/bin/hail
    args: [mcp]
    env:
      HAIL_SELF: hermes
      HAIL_THREAD: ${HAIL_THREAD}
      HAIL_HOP: ${HAIL_HOP}
```

Now `hail hermes "migrate the backups"` can turn into Hermes calling `hail_ask(agent="claude", message="what rule avoids silent cron failures?")` on its own. What hail adds to each call:

- **Threads and hops.** Every call belongs to a thread and has a hop number (you → hermes is hop 1, hermes → claude hop 2). hail exports `HAIL_THREAD`, `HAIL_HOP` and `HAIL_SELF` to the agent it runs and writes the same facts in a short `[hail]` header, so the next call in the chain is counted even across machines.
- **Loop guards.** A chain stops at `max_hops` (3), a thread at `max_calls_per_thread` (12), the machine at `rate_per_minute` (30): ping-pong between two agents ends with a logged refusal, not a bill.
- **Advice by default.** Between agents the default mode is `advice` (Claude runs with `--permission-mode plan`); asking another agent to act needs `[policy.act]`. `[policy.talk]` restricts who may consult whom.
- **Continuity.** Asking the same agent again in a thread sends it the earlier exchanges, so nobody re-explains.
- **Skills.** Give agents `skills = [...]` and `cost = "low|medium|high"`; `hail_ask(skill="code-review")` picks the cheapest available one that is not the caller.
- **Councils.** `hail_council` asks 2 to 6 agents in parallel and can have one write a synthesis.
- **Background tasks.** `hail_send` returns a task id at once; `hail_status` gives the result later.
- **Journal.** Every ask, reply, error, refusal and human note goes to `~/.hail/hail.db` (SQLite). Read it with `hail log`, `hail threads`, `hail thread <id>` or the `hail_thread` tool.

```toml
[policy]
max_hops = 3
max_calls_per_thread = 12
rate_per_minute = 30
[policy.talk]
hermes = ["claude", "openclaw"]
[policy.act]
hermes = ["openclaw"]
```

## Jobs and inbox

Agents that run on a schedule (Hermes cron jobs, for example) keep working while you are away. `hail jobs` lists what each one has planned, with the next run in UTC and in your local time (`--tz Europe/Paris` to pick one). `hail inbox` lists the reports they delivered since you last looked, `hail inbox read <agent> <id>` prints one and marks it read. Both are read-only and never wake the agent, so they cost no tokens.

Hermes works out of the box (hail reads `~/.hermes/cron/` on its machine). Any other agent can join with a `feed_command`: a read-only program that receives `HAIL_FEED_ARGS` (`["jobs"]`, `["inbox"]` or `["read", "<id>"]`) and prints JSON:

```toml
[agents.mybot]
preset = "command"
command = "mybot --stdin"
feed_command = "mybot-feed"   # prints [{"id", "name", "schedule", "next_run", ...}] or [{"id", "created", "preview"}]
```

### Telegram channel

Create a bot with [@BotFather](https://t.me/BotFather), add it as admin of a private group with **Topics** enabled, and put the token in a file (`chmod 600`), never in `agents.toml`:

```toml
[telegram]
chat_id = -1001234567890                       # the group
token_file = "~/.config/hail/telegram.token"   # or token_env = "HAIL_TELEGRAM_TOKEN"
allowed_users = [123456789]                    # who may send commands
topics = true                                  # one topic per thread
```

Every exchange between agents is posted in its thread's topic, with API keys, tokens and passwords redacted. Run `hail telegram watch` (e.g. as a systemd service) to accept commands from `allowed_users`: `/stop` and `/resume` inside a topic block or reopen that thread, any other text in a topic becomes a note the agents see on their next call, `/ask <agent> <message>` starts a new thread in the background, `/threads` lists recent ones.

## Journal

Every ask, reply, error and refusal goes to `~/.hail/hail.db` (SQLite, WAL mode), with API keys, tokens and passwords masked before they are written. To keep it small, either run `hail prune --days 30` from time to time or let hail do it once a day:

```toml
[journal]
retention_days = 90
```

Pruning deletes old messages, empty threads and finished background tasks; a task that is still running is never touched. A background task whose worker died (reboot, kill) is marked `failed` after six hours instead of blocking new ones.

## How it works

For a remote agent, hail runs `ssh -o BatchMode=yes <host> sh -c '<script>'` and writes your message to SSH's stdin. On the server the script reads it with `msg=$(cat)` and calls the agent with `"$msg"`. Your text is never pasted into a command line, so quotes, `$`, backticks and accents arrive untouched (there is a test for exactly that). Local presets run the agent directly, without a shell. The `command` preset runs your command through the local shell and sends the message on stdin.

The remote host needs a POSIX `sh` (any Linux or macOS server). On Windows, an agent installed as a `.cmd`/`.bat` shim (npm) cannot safely take the message as an argument, so hail refuses it; use the `claude` preset (stdin), a `command` agent, or run the agent over SSH/WSL.

On the server the agent runs under `timeout` (coreutils) with the same limit, so a call that times out does not leave the agent working on its own after hail gave up. Calls are synchronous with a per-agent timeout (default 180 s) and a size cap (default 8000 characters, header and history included). Use `hail send` / `hail_send` for longer requests.

## Safety

- **Replies are data, not instructions.** Another model wrote them. The MCP tool labels every reply as untrusted, and you should verify facts and open cited URLs yourself: agents do invent sources.
- **hail_ask is a prompt-injection path.** If an MCP client can call `hail_ask` and the target agent has tools (shell, email, files), whatever text reaches the client can end up as a request to that agent. Only configure agents whose permissions you are comfortable exposing, and prefer read-only agents.
- hail stores every message in `~/.hail/hail.db` on the machine that makes the call (secrets masked by pattern, which is a safety net): keep that directory private, prune it, or delete it to forget. It posts to a chat only if you configure `[telegram]` (secrets are redacted with patterns, which is a safety net, not a guarantee). OpenClaw runs without `--deliver`.
- **Advice mode is enforced only where the agent has a read-only switch** (today: Claude Code, `--permission-mode plan`, and Codex, `--sandbox read-only`). For Hermes, OpenClaw, Gemini and `command` agents it is an instruction in the header, not a sandbox: give those agents only the tools you accept them using.
- Identity: an agent is *verified* when hail exported `HAIL_SELF` to it (or declared it in its MCP config). An MCP caller that only *claims* a name with `from` may ask for advice, never for an action, unless the journal shows it was really asked something in that thread; an unidentified MCP caller is `unknown-agent`, never you. `/ask` from Telegram (allowed users only) runs as you, in action mode.
- Agents consulting each other multiply the prompt-injection paths: keep the default `advice` mode, grant `[policy.act]` sparingly, and read the journal.
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
