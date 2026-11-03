# hail

![hail in 45 seconds: one command to reach any agent, agents that consult each other, read-only advice mode, one inbox](docs/demo.gif)

**Talk to your AI agents from anywhere, and let them talk to each other.** One command to message Hermes, OpenClaw, Claude Code, Codex, Antigravity CLI (`agy`), a local Ollama model or any CLI agent, on this machine or on a server over SSH, from your terminal or from any MCP client. When an agent is stuck, it can consult another one itself, with loop guards, a journal and an optional Telegram channel to watch it happen.

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
uv tool install hail-agent
# or
pipx install hail-agent
# latest main: uv tool install git+https://github.com/kerneldottoday/hail
# with the keyboard screen (hail tui, adds Textual): uv tool install 'hail-agent[tui]'
```

Python 3.10+. The distribution name `hail-agent` is unrelated to the
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
| `agy` | `agy -p <msg> --output-format json` (Google Antigravity CLI; the message is an argument, visible in `ps` on that machine) | `response` |
| `gemini` | legacy Gemini CLI, message on stdin (API key only since 18 June 2026: use `agy`) | plain text |
| `ollama` | `ollama run <model>` (default `llama3.2`), message on stdin | plain text |
| `command` | your command, message on stdin | `text` or `json:<key>` |

In `advice` mode, `claude` runs with `--permission-mode plan`, `codex` with `--sandbox read-only` and `hermes` with read-only toolsets only (`-t web,session_search,vision,hail`: no terminal, files, code, browser or cron; narrow it with `advice_toolsets`, which only accepts read-only toolsets). OpenClaw switches to a read-only agent you define (`advice_agent_id = "advisor"`), and a `command` agent can name its read-only variant (`advice_command = "mybot --read-only"`). `hail list` marks the agents whose advice is only an instruction. `agy --mode plan` only adds a `/plan` prefix to the prompt and Gemini's plan mode switches to auto-approval once the plan is done in headless runs, so hail uses neither: for `agy` and `gemini`, advice is an instruction, not a sandbox. An `ollama` model has no tools at all, which makes it a cheap, safe second opinion.

### Paid agents and pinned SSH keys

`max_calls_per_day = 50` caps the asks an agent receives in any rolling 24 hours, all callers and threads together (you included). Set it on agents billed per call: a loop between two agents stops there instead of on your invoice.

The safest way to reach an agent on another machine is an SSH key that can only run one thing. On the server, pin the key in `~/.ssh/authorized_keys`:

```
restrict,from="100.64.0.7",command="/usr/local/bin/ask-advisor" ssh-ed25519 AAAA... hail-from-vps
```

`command=` makes the server ignore what the client asks to run, `restrict` turns off forwarding and terminals, `from=` limits where the key works. The script reads the message on stdin and calls a read-only agent, for example `exec openclaw agent --agent advisor --session-key hail --message "$(head -c 100000)" --json`. On the client, mark the agent:

```toml
[agents.pi]
preset = "openclaw"           # still picks the reply parser
host = "pi-advice"            # Host alias using that key
forced_command = true         # send the message only; the server decides what runs
```

hail then sends nothing but the message, and `hail doctor` tests only the SSH connection. A stolen key gives the read-only agent and nothing else; with Tailscale, an ACL that opens only port 22 of that machine narrows it further. hail cannot see what the script runs, so the read-only part is yours to check.

### Status lines (`footer`)

Some agents end every answer with their own status line: model, cost, remaining balance. Describe it and hail keeps it out of the reply. You still see it (on stderr, or as `footer` with `--json`), but other agents, MCP callers, the journal, Telegram and background tasks only get the answer, so your costs and balance stay with you.

```toml
[agents.hermes]
preset = "hermes"
host = "my-vps"
footer = '^cost: \$[\d.]+ · balance: '   # matches "cost: $0.025 · balance: $4.49" under each answer
```

`footer` is a regular expression (TOML single quotes keep backslashes as they are), tried with `re.search` on the last lines of the reply only; blank lines in between are skipped and at most 5 lines are taken. Lines over 500 characters are never tried. A pattern that matches an empty line is refused; anchor it anyway, since a loose one (`balance`) would also take the last lines of an answer that happen to match. An answer that is nothing but its footer counts as no answer (exit 6, whatever the agent's own exit code). `hail council` shows each agent's footer on stderr.

Every setting that is not a known key (`bin`, `agent_id`, `session`, `max_turns`...) fills the preset template, so presets stay easy to tweak.

## Commands

```
hail <agent> "message"     ask an agent (or pipe the message on stdin)
    [--json] [--thread T] [--mode advice|action] [--from NAME] [--hop N]
hail ask --skill S "msg"   ask the cheapest agent that has skill S
hail send <agent> "msg"    background task, prints its id
    [--ref R]              same R again = the same task, never started twice
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
hail tui                   threads, replies and agents on one keyboard screen (extra: tui)
hail list                  configured agents (with skills and cost)
hail help agents           manual for an AI agent: --json, exit codes, the agents configured here
hail add <name> --preset <p> [--host <alias>] [--command <cmd>] [--set key=value]
hail doctor                check every agent is reachable (never sends it a message, costs nothing)
hail mcp                   MCP server over stdio
```

### For scripts and agents: `--json` and exit codes

`--json` works on `list`, `doctor`, ask, `send`, `status`, `council`, `log`, `threads`, `thread`, `jobs` and `inbox`. One object per line (lists oldest first), snake_case fields, times in UTC (`2026-11-02T14:05:00Z`), fields that do not apply are left out. An ask returns `{"agent", "reply", "ok", "thread", "hop"}`. When a command fails, stdout stays empty and stderr says why.

| Code | Meaning | What to do |
|---|---|---|
| 0 | ok | |
| 1 | other error | read stderr |
| 2 | usage | fix the command line |
| 3 | config | no agents file or invalid (`hail init`) |
| 4 | the agent answered with an error | its reply is still printed |
| 5 | no such agent, skill, task or thread | `hail list` |
| 6 | the agent could not run or returned nothing | retrying will not help |
| 7 | refused by a guard rail (policy, hops, thread budget, stopped thread) | do not retry |
| 75 | temporary: timeout, SSH unreachable, rate limit, daily cap | retry later, not in a loop |

`hail send --ref R` (MCP: `ref`) makes retries safe: sending the same request with the same ref returns the task it already started (`"reused": true`) instead of starting a second one. A ref used for a different request exits 2. Once that task has failed, the same ref starts a new attempt of the same request. Refs are shared by all callers and freed when `prune` deletes their task. A background task runs at most once, even if its worker is started twice.

When SSH fails before a session opens (exit 255 in under 15 s, nothing printed, and ssh reports a refused, unreachable or unresolved host), hail retries once after 2 s. A connection dropped after login is not retried, since the agent may already be running. A timeout (75) does not mean nothing happened. `hail status` exits 4 for any failed task (its `error` field says why); `doctor`, `jobs` and `inbox` exit 1 when some agents failed and still print the others.

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

Hermes works out of the box (hail reads `~/.hermes/cron/` on its machine). Work it finishes outside a cron job (a chat, a task resumed by hand) can reach you too: give it a mailbox folder, tell it to drop a note there when it is done, and the note shows up in `hail inbox` as `mailbox/<file>` (top-level files only, dotfiles ignored):

```toml
[agents.hermes]
preset = "hermes"
host = "my-vps"
mailbox = "~/shared/from-agents/hermes"   # on the agent's machine
```

`hail inbox clear` remembers the newest report it cleared, so older reports that an agent's cleanup lets back into the list never come back as new.

Any other agent can join with a `feed_command`: a read-only program that receives `HAIL_FEED_ARGS` (`["jobs"]`, `["inbox"]` or `["read", "<id>"]`) and prints JSON:

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

## Keyboard screen: `hail tui`

`uv tool install 'hail-agent[tui]'` adds [Textual](https://textual.textualize.io) (hail itself stays dependency-free), then `hail tui`:

- left, the threads between your agents, newest first, with who took part; right, the open thread message by message (agent text is shown as plain, untrusted text, never as markup);
- `j`/`k` move, `r` replies to the agent asked last in the thread (sent as a background task, so the screen never waits), `s` stops or resumes the thread, `a` shows the agents with their read-only mode and today's calls against `max_calls_per_day`, `g` refreshes (it also refreshes every 3 s), `q` quits.

It reads the journal and the agents file (once, at start: restart it after `hail add`) and nothing else: no account, no server, works over SSH on the VPS. An agent cannot run it under `cli_access = read`.

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
- hail stores every message in `~/.hail/hail.db` on the machine that makes the call (secrets masked by pattern, which is a safety net). hail creates it owner-only (`700`, file `600`) and tightens it on every start; prune it, or delete it to forget. It posts to a chat only if you configure `[telegram]` (secrets are redacted with patterns, which is a safety net, not a guarantee). OpenClaw runs without `--deliver`.
- **`[policy] cli_access`** decides what an agent that runs hail may do: `all` (default), `read` (list, doctor, log, threads, thread, status, jobs, inbox; no ask, send, council, stop, resume, prune, add, init, telegram, and no `inbox read` or `clear`, which mark items as read) or `none` (only `hail help`). A refusal exits 7. It applies to calls from an agent (`HAIL_SELF` set, or any MCP caller), never to you, and guards hail's own commands only: a program running as you can still read `~/.hail` and the agents file, unset `HAIL_SELF`, or point `HAIL_CONFIG` at a file of its own. `hail mcp` itself always starts (each tool is checked), and a background task allowed when it was sent still runs. For an agent, an invalid agents file now stops every command but `help` (exit 3).
- **Advice mode is enforced only where the agent has a read-only switch**: Claude Code (`--permission-mode plan`), Codex (`--sandbox read-only`), Hermes (read-only toolsets), OpenClaw with `advice_agent_id`, `command` agents with `advice_command`, and Ollama (no tools). Elsewhere (`agy`, Gemini, OpenClaw or `command` without a read-only variant) it is an instruction in the header, not a sandbox. Set `strict_advice = true` under `[policy]` so agents can only consult agents that enforce it; you can still ask any of them yourself. hail checks that `advice_agent_id` and `advice_command` differ from the full agent, not that they are really read-only: that part is yours. Strict mode applies to calls hail sees as coming from an agent (`HAIL_SELF` set, or an MCP caller); an agent that runs `hail` in a shell where `HAIL_SELF` was removed counts as you.
- Identity: an agent is *verified* when hail exported `HAIL_SELF` to it (or declared it in its MCP config). An MCP caller that only *claims* a name with `from` may ask for advice, never for an action, unless the journal shows it was really asked something in that thread; an unidentified MCP caller is `unknown-agent`, never you. `/ask` from Telegram (allowed users only) runs as you, in action mode.
- Agents consulting each other multiply the prompt-injection paths: keep the default `advice` mode, grant `[policy.act]` sparingly, and read the journal.
- For local presets that take the message as an argument (`hermes`, `openclaw`), the message is visible in the local process list while the call runs. Remote messages travel only on the SSH channel.
- A remote agent reached with an ordinary key runs whatever hail sends, and so would anyone holding that key. Prefer a pinned key (`forced_command`, see *Paid agents and pinned SSH keys*).
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
