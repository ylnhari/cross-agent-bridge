# Agent Chat command reference

Every command prints one compact JSON object. Successful objects include the
resolved database path, stable bridge ID, and schema version under `bridge`.
Errors are one JSON object on stderr and normally exit with code 2.

## Shared configuration

| Environment variable | CLI option | Meaning |
|---|---|---|
| `AGENT_CHAT_PROFILE` | `--profile` | Local JSON profile pinning database, bridge ID, and room |
| `AGENT_CHAT_DB` | `--db` | Exact SQLite database path |
| `AGENT_CHAT_BRIDGE` | `--expect-bridge` | Expected stable bridge ID |
| `AGENT_CHAT_ROOM` | `--room` | Current room |
| `AGENT_CHAT_ENDPOINT` | `--as` / `--from` | Unique live-session endpoint |
| `AGENT_CHAT_SYSTEM` | `--system` | Agent product or runtime family |

There is no implicit database or room discovery. Prefer one ignored local
profile. If a database, bridge ID, or room from another source conflicts with
the profile, the command fails before mutation.

## Database and membership

```powershell
$db = "$PWD\.local\agent-chat.sqlite3"
$profile = "$PWD\.local\room.bridge.json"
agent-chat --db $db init --room ROOM --write-profile $profile

agent-chat --profile $profile join --endpoint ENDPOINT --system SYSTEM --label "optional"
agent-chat --profile $profile leave --as ENDPOINT
agent-chat --profile $profile members
agent-chat --profile $profile doctor
```

An endpoint identifies one live session and is unique across the bridge
database. Concurrent sessions never reuse an endpoint, even in different rooms
or when they belong to the same system. Leaving stops future sends to that
member but does not delete deliveries already queued for it.

`members` reports adapter type, host reference, lease expiry, and
`adapter_online`. That flag proves a host adapter is renewing its lease; it does
not prove the model is currently generating.

## Live-session launchers

```powershell
agent-chat-codex --profile $profile --endpoint codex.app.1 `
  --create-thread --cwd C:\path\to\project --open-app

agent-chat-claude --profile $profile --endpoint claude.cli.1 `
  --cwd C:\path\to\project --prompt "Wait for authorized bridge messages."
```

`agent-chat-codex` creates or resumes one app-server task and keeps its adapter
alive across model-turn boundaries. `agent-chat-claude` starts a genuine
interactive Claude Code terminal with an isolated packaged Channel. Give every
concurrent native session a unique endpoint. See
[docs/LIVE-SESSION-SETUP.md](docs/LIVE-SESSION-SETUP.md) for authority prompts,
resume commands, and verification.

## Send

```powershell
agent-chat send --from ENDPOINT --to PEER --text "question"
agent-chat send --from ENDPOINT --to PEER_A --to PEER_B --stdin
agent-chat send --from ENDPOINT --broadcast --file message.txt

agent-chat send --from ENDPOINT --to PEER `
  --reply-to MESSAGE_ID --kind progress --text "Still working"

agent-chat send --from ENDPOINT --to PEER `
  --reply-to MESSAGE_ID --kind reply --text "Final answer"
```

Options:

- `--reply-to MESSAGE_ID` links to a message visible to the sender.
- `--kind message|progress|reply` records conversational intent. Root sends are
  `message`; linked sends default to final `reply`.
- `--key CLIENT_MESSAGE_ID` makes a retry idempotent for that sender and room.

The same retry key may be reused only with identical text, kind, reply target,
audience, and recipients.

Each recipient may commit one distinct final reply to a delivered root. An
identical retry returns the original reply; a conflicting second final reply is
rejected without inserting another message.

Every root message starts a random `conversation_id`. Progress and replies
inherit the parent's conversation ID automatically.

## Receive and delivery evidence

```powershell
agent-chat receive --as ENDPOINT --wait 300 --visibility 900
agent-chat mark --as ENDPOINT --id MESSAGE_ID --receipt RECEIPT `
  --state injected --adapter host.adapter --host-ref SESSION_REFERENCE
agent-chat ack --as ENDPOINT --id MESSAGE_ID --receipt RECEIPT
agent-chat mark --as ENDPOINT --id MESSAGE_ID --receipt RECEIPT --state acted
agent-chat release --as ENDPOINT --id MESSAGE_ID --receipt RECEIPT
agent-chat renew --as ENDPOINT --id MESSAGE_ID --receipt RECEIPT --visibility 900
agent-chat delivery --as ENDPOINT --id MESSAGE_ID
```

`receive` claims the oldest visible delivery atomically and returns a random
receipt, attempt number, and visibility expiry. A stale receipt cannot mutate a
newer attempt. Exit code 3 with `status=empty` means a bounded wait ended without
a message; it is not a transport failure.

Host adapters mark `injected` only after their host accepts the event. The model
calls `ack` to record `observed`, then marks or sends progress when acting. A
final linked reply records `replied` automatically.

## Adapter ownership

These commands are primarily for host-adapter implementations:

```powershell
agent-chat adapter-acquire --as ENDPOINT --owner PROCESS_TOKEN `
  --adapter host.adapter --host-ref SESSION_REFERENCE --ttl 30

agent-chat adapter-renew --as ENDPOINT --owner PROCESS_TOKEN --ttl 30
agent-chat adapter-release --as ENDPOINT --owner PROCESS_TOKEN
```

Only one unexpired owner may hold the `(room, endpoint)` lease. Release requeues
every claimed request that never got a final reply. An expired owner cannot
renew; it must stop rather than continue as a duplicate consumer. The lease is
membership-local, so adapter operators must also preserve the database-wide
rule that one unique endpoint identifies one native session and must not attach
that session through multiple adapters.

## Inspect

```powershell
agent-chat status --room ROOM
agent-chat history --room ROOM --as ENDPOINT --limit 50
agent-chat history --room ROOM --as ENDPOINT `
  --conversation CONVERSATION_ID --limit 50
agent-chat members --room ROOM
agent-chat delivery --room ROOM --as ENDPOINT --id MESSAGE_ID
agent-chat doctor
```

Endpoint-scoped history includes only messages sent by or delivered to that
endpoint. Admin history without `--as` returns the room transcript to any
process that can open the database, which is why filesystem access is the trust
boundary.

`status` distinguishes unread, actively claimed, immediately visible, and
delivery-state counts. Delivery state is evidence, not a substitute for
independent verification of repository or external work.

Adapter-generated `Bridge adapter status` messages are nonterminal `progress`
events. They tell the peer that bounded attention recovery was exhausted; they
do not close the original root or claim that the target completed its work.

## Legacy format

`chat.py` is the frozen two-agent compatibility CLI for the original local
prototype. The packaged `agent-chat` CLI refuses that database and unsupported
pre-release schemas instead of silently migrating live state. New integrations
use schema 4.
