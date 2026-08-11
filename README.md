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
- One renewable adapter lease per endpoint, preventing duplicate live consumers.
- Recovery of every claimed but unfinished delivery when an adapter lease is
  lost and a new adapter takes over.
- A stable bridge ID that fails closed if a session points at the wrong database.

The SQLite core alone is only durable storage. Live attention delivery comes
from the Claude and Codex adapters, which remain active independently of model
turns.

## Requirements

- Python 3.11 or newer for the core and Codex adapter.
- Codex CLI with app-server support for Codex integration. Version 0.146.0 was
  used for the current Windows validation. See the official
  [Codex app-server documentation](https://developers.openai.com/codex/app-server).
- Node.js 20 or newer and Claude Code 2.1.80 or newer for the Claude Channel
  adapter. Version 2.1.226 was used for the current validation. Channels remain
  an Anthropic research preview; see the official
  [Channels guide](https://code.claude.com/docs/en/channels) and
  [protocol reference](https://code.claude.com/docs/en/channels-reference).

Install the Python package from this checkout:

```powershell
python -m pip install -e .
agent-chat --help
```

If the Python environment's scripts directory is not on `PATH`, use
`python -m agent_chat` in place of `agent-chat`, and
`python -m agent_chat.codex_adapter` in place of `agent-chat-codex`.

Install the pinned Claude adapter dependency only when Claude integration is
needed:

```powershell
npm ci --prefix adapters/claude-channel
```

## Create a bridge

Only `init` may create a database. That prevents a misspelled path from silently
forming a second message fabric.

```powershell
$db = "$PWD\.local\agent-chat.sqlite3"
$init = agent-chat --db $db init --room build | ConvertFrom-Json

$env:AGENT_CHAT_DB = $init.bridge.database
$env:AGENT_CHAT_BRIDGE = $init.bridge.id
$env:AGENT_CHAT_ROOM = "build"
```

Keep the database in ignored, user-only local storage. Never put a real bridge
database in Git or a cloud-synced directory.

## Connect a live Codex task

Give every live session a unique endpoint ID:

```powershell
$env:AGENT_CHAT_ENDPOINT = "codex.app.1"

agent-chat-codex `
  --create-thread `
  --cwd C:\path\to\project `
  --title "Project executor"
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

On Windows, the adapter uses a standalone stdio app-server because the managed
app-server daemon is Unix-only. Unix defaults to the managed daemon transport.

An existing adapter-created task can be reattached after a restart:

```powershell
agent-chat-codex --thread-id TASK_ID
```

Attaching an arbitrary older task without persisted bridge tools is a limited
compatibility path. `--compat-output-routing` can forward ordinary model output,
but it cannot safely infer several simultaneous senders. Use an adapter-created
task for full multi-conversation routing.

## Connect a live Claude Code session

Create an MCP config for that one Claude session. Paths must be absolute when the
Claude project is outside this repository:

```json
{
  "mcpServers": {
    "agent-chat": {
      "command": "node",
      "args": ["C:\\path\\to\\cross-agent-bridge\\adapters\\claude-channel\\index.mjs"],
      "env": {
        "AGENT_CHAT_DB": "C:\\path\\to\\agent-chat.sqlite3",
        "AGENT_CHAT_BRIDGE": "BRIDGE_ID_FROM_INIT",
        "AGENT_CHAT_ROOM": "build",
        "AGENT_CHAT_ENDPOINT": "claude.cli.1",
        "AGENT_CHAT_SYSTEM": "claude"
      }
    }
  }
}
```

Start a genuine interactive Claude Code session:

```powershell
claude `
  --mcp-config C:\path\to\.mcp.json `
  --strict-mcp-config `
  --dangerously-load-development-channels server:agent-chat
```

The `dangerously-load-development-channels` flag is required by Claude Code's
current Channel research preview for a local custom channel. Review and accept
Claude's local-development warning only for this trusted checkout. The channel
process stays attached to that live session, receives while Claude is busy, and
renews its bridge lease without spending model tokens.

The validated Claude host is a genuine interactive Claude Code terminal
session. As of the validation date, Anthropic documents per-session Channel
opt-in through the CLI `--channels`/development flag and does not document a
Claude Code Desktop equivalent. Desktop shares MCP configuration, but an MCP
connection alone does not enable push delivery. Do not claim Desktop Code-tab
support until that exact surface passes the same live test.

For another Claude session, use another endpoint ID and another config/process.
Never reuse one endpoint ID for two concurrent sessions.

## Talk and inspect

Adapters normally call these operations for the models, but the CLI is useful
for diagnostics and custom hosts:

```powershell
agent-chat join --endpoint observer.1 --system custom
agent-chat send --from observer.1 --to codex.app.1 --text "Can you verify this?"
agent-chat receive --as observer.1 --wait 30
agent-chat history --as observer.1 --limit 50
agent-chat members
agent-chat status
agent-chat doctor
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
npm test --prefix adapters/claude-channel
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the contributor setup, CI
matrix, individual quality gates, and the intentional legacy exclusions.

The latest real-host evidence is recorded in
[docs/VALIDATION-2026-08-11.md](docs/VALIDATION-2026-08-11.md). The original
two-agent `chat.py` CLI remains frozen only for its pre-release legacy database;
new integrations use `agent-chat` and schema 4.
