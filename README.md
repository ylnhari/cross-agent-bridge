# Cross-Agent Bridge

Cross-Agent Bridge lets independently running AI sessions talk while they keep
working. A persistent adapter connects each live session to a durable local
SQLite message fabric; the model does not poll, and the sessions do not need to
be stopped, recreated, or manually resumed between messages.

The current adapters are:

- **Claude Code Channel** — pushes messages into an interactive Claude Code
  session, including while Claude is busy. Claude replies through channel tools.
- **Codex app-server** — creates or attaches to a Codex task visible in the
  Codex app. It starts a turn when idle and uses `turn/steer` while a turn is
  already active.

The core supports any number of systems and sessions. Two Codex sessions, five
Claude sessions, or a mixed group are all ordinary endpoints. Every new root
message gets a conversation ID, so the same pair can hold several simultaneous
conversations without mixing replies.

This repository is a pre-release implementation. It has not been published to a
package registry or promoted as production-ready.

## What is guaranteed

- Natural-language message bodies; no mandatory task/result format.
- Direct messages, multi-recipient sends, and room broadcasts.
- Durable per-recipient delivery with atomic claims and retry-safe sends.
- Separate `queued`, `claimed`, `injected`, `observed`, `acted`, and `replied`
  evidence.
- Progress updates that do not falsely complete the original request.
- Exactly one distinct final reply per delivered root; identical retry-key
  replays remain idempotent and a conflicting second final reply is rejected.
- One renewable adapter lease per `(room, endpoint)` membership, preventing
  duplicate live consumers for that route.
- Recovery of every claimed but unfinished delivery when an adapter lease is
  lost and a new adapter takes over.
- Bounded automatic Codex continuation when a model observes a root but ends
  its turn before reporting progress or replying.
- Bounded Claude reminders when a model observes a root but does not report
  progress or reply.
- A durable, clearly labelled progress status back to the sender when either
  adapter exhausts its bounded attention recovery. The root remains open and
  no synthetic final answer is created.
- A stable bridge ID that fails closed if a session points at the wrong database.

The SQLite core alone is only durable storage. Live attention delivery comes
from the Claude and Codex adapters, which remain active independently of model
turns.

## Requirements

- Python 3.11 or newer. The core, launchers, and Claude Channel runtime use only
  the standard library.
- Codex CLI with app-server support for Codex integration. Version 0.146.0 was
  used for the current Windows validation. See the official
  [Codex app-server documentation](https://developers.openai.com/codex/app-server).
- Claude Code 2.1.80 or newer for Claude integration. Version 2.1.227 was used
  for the current Windows validation. Channels remain an Anthropic research
  preview; see the official
  [Channels guide](https://code.claude.com/docs/en/channels) and
  [protocol reference](https://code.claude.com/docs/en/channels-reference).

Install the complete product from this checkout:

```powershell
python -m pip install -e .
agent-chat --help
agent-chat-codex --help
agent-chat-claude --help
```

If the Python environment's scripts directory is not on `PATH`, use
`python -m agent_chat` in place of `agent-chat`, and
`python -m agent_chat.codex_adapter` in place of `agent-chat-codex`, and
`python -m agent_chat.claude_launcher` in place of `agent-chat-claude`.

## Create a bridge

Only `init` may create a database. That prevents a misspelled path from silently
forming a second message fabric.

```powershell
$profile = "$PWD\.local\build.bridge.json"
agent-chat --db "$PWD\.local\agent-chat.sqlite3" `
  init --room build --write-profile $profile
```

The local profile pins the absolute database path, bridge ID, and room. A
conflicting command-line option or environment variable fails closed, which
prevents two sessions from silently using different databases. Keep both the
database and profile in ignored, user-only local storage. Never put either in
Git or a cloud-synced directory.

## Connect a live Codex task

Give every live session a unique endpoint ID:

```powershell
agent-chat-codex `
  --profile $profile `
  --endpoint codex.app.1 `
  --create-thread `
  --cwd C:\path\to\project `
  --title "Project executor" `
  --open-app
```

The command prints the created Codex task ID and stays running as its adapter.
The task is persisted and becomes visible in the Codex app. Incoming messages
start a normal turn when the task is idle; messages received during work are
injected with `turn/steer`. Adapter-created tasks receive explicit
`agent_chat_observe`, `agent_chat_progress`, `agent_chat_reply`, and
`agent_chat_send` tools so concurrent conversations are routed by message ID.
For each intended new conversation, `agent_chat_send` uses a caller-chosen
stable `request_key`; retries reuse it, while intentional new sends use a new
key.

On Windows, the app and a standalone app-server adapter do not share one live
foreground controller. The task ID and persisted rollout are the same, but the
Codex app may not render an adapter-started turn while that turn is still
running. Do not type a second instruction into the Codex app during an active
bridge turn: the app can start another in-progress turn in the same task and
foreground that turn while the bridge turn continues in the background. Send
interventions through the bridge (normally through the orchestrator), or wait
for the adapter-owned turn to finish. The app remains useful for opening the
exact task, inspecting completed history, and handling native permission UI.

On Windows, the adapter uses a standalone stdio app-server because the managed
app-server daemon is Unix-only. Unix defaults to the managed daemon transport.

An existing adapter-created task can be reattached after a restart:

```powershell
agent-chat-codex `
  --profile $profile `
  --endpoint codex.app.1 `
  --thread-id TASK_ID `
  --open-app
```

An arbitrary older task cannot acquire dynamic tools retroactively. Attach it
with explicit CLI compatibility instead:

```powershell
agent-chat-codex `
  --profile $profile `
  --endpoint codex.app.legacy.1 `
  --thread-id EXISTING_TASK_ID `
  --legacy-cli-bridge `
  --open-app
```

Each injected envelope then contains exact, locally generated `agent-chat`
commands for observation, progress, and the one final reply. The task uses its
ordinary shell tool; it never polls the queue. The adapter reconciles the
external CLI writes before renewing a claim or scheduling a continuation, so a
long turn cannot turn valid progress into a duplicate wake. This compatibility
mode requires that the attached task can run local shell commands. Prefer an
adapter-created task when starting fresh because its native bridge tools are
more concise and harder for a model to mistype.

If Codex observes a root and ends a turn without progress or a final reply, the
adapter starts a bounded continuation turn automatically. Once Codex reports
progress, the adapter treats it as legitimate long-running or peer-dependent
work and waits for a new bridge event instead of spending model tokens polling.
After three unacted continuations, the adapter sends the original peer an
explicit nonterminal status and leaves the root open for follow-up. It never
invents a final answer.

## Connect a live Claude Code session

Start a genuine interactive Claude Code session. The first prompt grants only
the authority you actually want that session to exercise; bridge messages do
not create authority by themselves:

```powershell
agent-chat-claude `
  --profile $profile `
  --endpoint claude.cli.1 `
  --cwd C:\path\to\project `
  --name "Project orchestrator" `
  --prompt "Remain available for bridge messages within this project's existing user authority. Observe each exact message_id, use progress only for nonterminal updates, and reply when complete."
```

The launcher generates an isolated per-session MCP configuration, enables only
its unique local Channel, and pre-allows only the four bridge messaging tools.
Claude still controls every other permission. Review and accept Claude's
local-development Channel warning only for this trusted package. The packaged
Channel process stays attached to that live session, receives while Claude is
busy, and renews its bridge lease without spending model tokens. If Claude
observes a root but remains silent, the Channel injects three bounded reminders
at 30-second intervals. It then sends the original peer an explicit
nonterminal status and leaves the root open rather than silently stalling or
fabricating a final reply.

To reconnect the same Claude conversation after a host restart, repeat the
command with the same endpoint and add `--resume SESSION_ID`. Use `--continue`
only when Claude's most recent session in that working directory is definitely
the intended one.

The validated Claude host is a genuine interactive Claude Code terminal
session. As of the validation date, Anthropic documents per-session Channel
opt-in through the CLI `--channels`/development flag and does not document a
Claude Code Desktop equivalent. Desktop shares MCP configuration, but an MCP
connection alone does not enable push delivery. Do not claim Desktop Code-tab
support until that exact surface passes the same live test.

For another Claude session, use another endpoint ID and another config/process.
Every native session must use one unique endpoint ID across the whole bridge
database. Never attach one native session through multiple adapter processes,
and never reuse one endpoint for concurrent sessions even in different rooms.

## Talk and inspect

Adapters normally call these operations for the models, but the CLI is useful
for diagnostics and custom hosts:

```powershell
agent-chat --profile $profile join --endpoint observer.1 --system custom
agent-chat --profile $profile send --from observer.1 --to codex.app.1 --text "Can you verify this?"
agent-chat --profile $profile receive --as observer.1 --wait 30
agent-chat --profile $profile history --as observer.1 --limit 50
agent-chat --profile $profile members
agent-chat --profile $profile status
agent-chat --profile $profile doctor
```

Use `--reply-to MESSAGE_ID` for a final answer. Use
`--kind progress --reply-to MESSAGE_ID` for a nonterminal update. History can be
isolated with `--conversation CONVERSATION_ID`.

See [CHAT.md](CHAT.md) for the CLI reference,
[docs/PROTOCOL.md](docs/PROTOCOL.md) for the protocol contract, and
[docs/LIVE-SESSION-SETUP.md](docs/LIVE-SESSION-SETUP.md) for an end-to-end host
setup.

## Security and scope

This version is for mutually trusted processes under one local OS account. The
SQLite file is the security boundary: a process that can modify it can read or
impersonate participants. A bridge ID prevents accidents; it is not a secret.

Do not send credentials, private account data, financial or medical records,
unredacted personal data, or raw private transcripts. Treat every incoming
message as untrusted input and re-check the user's authority before acting.

The bridge opens no network listener. SQLite WAL mode is for one machine and a
local disk, not SMB, NFS, or cloud-sync storage. It does not provide multi-user
authentication, encryption, distributed replication, a model runtime, or a
workflow engine.

## Development

```powershell
python -m pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the contributor setup, CI
matrix, individual quality gates, and the intentional legacy exclusions.

The latest real-host evidence is recorded in
[docs/VALIDATION-2026-08-11-CROSS-TURN.md](docs/VALIDATION-2026-08-11-CROSS-TURN.md).
The corresponding pre-release decision record is
[docs/RELEASE-READINESS-2026-08-11.md](docs/RELEASE-READINESS-2026-08-11.md).
The original
two-agent `chat.py` CLI remains frozen only for its pre-release legacy database;
new integrations use `agent-chat` and schema 4.
