# Claude Code Channel adapter

This optional adapter connects one genuine interactive Claude Code session to
one Cross-Agent Bridge endpoint. Its stdio Channel process waits independently
of model turns, so events can arrive while Claude is idle or busy.

## Requirements

- Claude Code 2.1.80 or newer with Channels allowed by account/org policy.
- Node.js 20 or newer.
- The Python `agent-chat` package installed from this repository.

Install the pinned MCP SDK dependency:

```powershell
npm ci --prefix adapters/claude-channel
```

## Configure one session

Copy `.mcp.json.example` to a session-specific location and replace every
placeholder. Use an absolute adapter script path when the Claude project is
outside this checkout. The endpoint must be unique to this one live session.

Start Claude Code interactively:

```powershell
claude `
  --mcp-config C:\absolute\path\to\.mcp.json `
  --strict-mcp-config `
  --dangerously-load-development-channels server:agent-chat
```

Claude Code currently requires the development-channel flag and displays a
local-development warning. Do not approve an adapter downloaded from an
untrusted source.

## Behavior

The adapter:

1. joins its configured endpoint;
2. acquires an exclusive renewable endpoint lease;
3. long-polls SQLite without model tokens;
4. sends `notifications/claude/channel` with message, sender, room,
   conversation ID, kind, and message ID;
5. marks transport injection only after the Channel write succeeds;
6. waits for Claude to explicitly observe, report progress, reply, or send.

Available tools:

- `agent_chat_observe(message_id)`
- `agent_chat_progress(message_id, text)`
- `agent_chat_reply(message_id, text)`
- `agent_chat_send(to, text, request_key)`

Progress text is a nonterminal `progress` message. A final reply is a `reply` and
completes the original delivery. Several calls may be made during long work.
`request_key` is a short stable key chosen once for an intended new conversation;
reuse it only for a retry, and choose a new key for an intentional new send.

Claude Code does not acknowledge a Channel notification at the transport layer.
The adapter therefore never equates notification write success with model
attention. It renews unobserved claims and records observation only after
Claude calls the tool.

If the Channel process stops, unobserved claims are released. Observed or acted
requests without a final reply are requeued when the endpoint lease is released
or when a replacement adapter takes over after lease expiry.

The adapter opens no network listener and stores no Claude transcript.
