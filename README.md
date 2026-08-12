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

Only `init` may create a database; every other command fails closed on a
missing, foreign, or mismatched database rather than silently diverging (see
[docs/PROTOCOL.md](docs/PROTOCOL.md) § Failure behavior).

```powershell
$profile = "$PWD\.local\build.bridge.json"
agent-chat --db "$PWD\.local\agent-chat.sqlite3" `
  init --room build --write-profile $profile
```

The local profile pins the absolute database path, bridge ID, and room. Keep
both the database and profile in ignored, user-only local storage; never put
either in Git or a cloud-synced directory.

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

This creates a persisted Codex task visible in the Codex app and keeps a
bounded, lease-owning adapter running independently of model turns, routing
concurrent conversations by message ID. See
[docs/PROTOCOL.md](docs/PROTOCOL.md) for the delivery, continuation, and
retry-key contract, and [docs/LIVE-SESSION-SETUP.md](docs/LIVE-SESSION-SETUP.md)
for reattaching an existing task, attaching an older legacy task, and
Windows-specific app/adapter caveats.

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

This starts an isolated per-session Channel that stays attached to that live
session and renews its bridge lease independently of model turns. Every
concurrent native session needs its own unique endpoint ID. See
[docs/PROTOCOL.md](docs/PROTOCOL.md) for the delivery and reminder contract,
and [docs/LIVE-SESSION-SETUP.md](docs/LIVE-SESSION-SETUP.md) for reconnect
commands, authority-prompt guidance, and current Desktop-support status.

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

This version is for mutually trusted processes under one local OS account: the
SQLite file itself is the security boundary, and a bridge ID prevents
accidental database mix-ups but is not a credential. Never send secrets,
credentials, or private data through the bridge; see [SECURITY.md](SECURITY.md)
for the full threat model, data-handling rules, and deployment boundary.

## Development

```powershell
python -m pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the full contributor setup,
CI matrix, individual quality gates, and the intentional legacy exclusions.

The latest real-host evidence is recorded in
[docs/VALIDATION-2026-08-11-CROSS-TURN.md](docs/VALIDATION-2026-08-11-CROSS-TURN.md).
The corresponding pre-release decision record is
[docs/RELEASE-READINESS-2026-08-11.md](docs/RELEASE-READINESS-2026-08-11.md).
The legacy two-agent `chat.py` CLI remains frozen for its own pre-release
database only; see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the current
schema-4 compatibility rule.
