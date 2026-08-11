# Live Claude and Codex setup

This setup produces two independently running interactive sessions. Their
adapters, not their model turns, wait for messages. Once connected, both
sessions can continue ordinary long-running work and exchange questions,
progress, clarifications, and final answers without manual copy/paste.

This guide currently starts Claude Code in an interactive terminal. The Codex
task is visible in the Codex app. Anthropic's current Channel preview requires a
per-session CLI opt-in and does not document an equivalent control in the
Claude Code Desktop UI, so Desktop Code-tab push delivery is not yet claimed.

## 1. Prepare one local bridge

From the Cross-Agent Bridge checkout:

```powershell
python -m pip install -e .
npm ci --prefix adapters/claude-channel

$db = "$PWD\.local\project-chat.sqlite3"
$init = agent-chat --db $db init --room project | ConvertFrom-Json

$env:AGENT_CHAT_DB = $init.bridge.database
$env:AGENT_CHAT_BRIDGE = $init.bridge.id
$env:AGENT_CHAT_ROOM = "project"
```

Save the resolved database path and bridge ID. Every adapter for this room must
use those exact values.

## 2. Start the Codex participant

In one terminal:

```powershell
$env:AGENT_CHAT_ENDPOINT = "codex.app.1"

agent-chat-codex `
  --create-thread `
  --cwd C:\path\to\target-project `
  --title "Target project executor"
```

Keep this process running. Its first JSON line contains the Codex task ID. Open
that task in the Codex app whenever you want to watch its transcript. The
adapter itself remains outside model turns:

- idle task + inbound message: `turn/start`;
- active task + inbound message: `turn/steer`;
- Codex reply/progress tool call: durable message back to the sender.

The adapter does not approve shell commands, file changes, or user-input
requests on Codex's behalf. Those remain in the interactive Codex client.

If console entry points are not on `PATH`, replace `agent-chat-codex` with
`python -m agent_chat.codex_adapter` and `agent-chat` with
`python -m agent_chat` throughout this guide.

## 3. Start the Claude participant

Create a session-specific `.mcp.json` using the template in
`adapters/claude-channel/.mcp.json.example`. Set:

- the same database path, bridge ID, and room;
- a unique endpoint such as `claude.cli.1`;
- the absolute path to `adapters/claude-channel/index.mjs`.

Then start Claude Code interactively:

```powershell
claude `
  --mcp-config C:\absolute\path\to\.mcp.json `
  --strict-mcp-config `
  --dangerously-load-development-channels server:agent-chat
```

Claude Code currently labels custom Channels as a research preview. Confirm the
local-development warning only when the configured adapter path is this trusted
checkout. Keep the Claude session open. Its Channel subprocess waits and renews
leases even when the model is idle or busy.

## 4. Verify the live pair

Use a third diagnostic endpoint so the test does not impersonate either live
session:

```powershell
agent-chat join --endpoint probe.1 --system human-test
agent-chat members
```

`members` should report `adapter_online: true` for both live endpoints. Send a
small message to each:

```powershell
agent-chat send --from probe.1 --to codex.app.1 --text "Reply with CODEX_OK."
agent-chat send --from probe.1 --to claude.cli.1 --text "Reply with CLAUDE_OK."
agent-chat receive --as probe.1 --wait 60
```

For a direct agent-to-agent proof, ask either live agent to call
`agent_chat_send` to the other endpoint. Replies retain the same
`conversation_id` automatically. A new-conversation send also takes a stable
`request_key`: reuse it only for a retry of that intended send, and choose a new
key for an intentional new conversation.

## Many sessions and conversations

Run one adapter process per live session and assign each a unique endpoint:

```text
claude.cli.1
claude.cli.2
codex.app.1
codex.app.2
codex.cli.review.1
```

All may join the same room. A new message without `reply_to` starts a new
conversation; replies and progress inherit the parent's conversation ID. This
allows several independent conversations between the same pair and several
senders addressing one busy session at once.

The endpoint lease rejects two live adapters that accidentally reuse one ID.
After an adapter crash, its lease expires. The next instance takes ownership and
requeues every claimed request that never received a final reply.

## Long-running work

Agents should:

1. observe the inbound message;
2. send useful progress with `agent_chat_progress` during long work;
3. keep doing the authorized task;
4. answer questions arriving under other message IDs;
5. finish the original request with `agent_chat_reply`.

Progress is deliberately nonterminal. Only a final reply moves the original
delivery to `replied`.

## Troubleshooting

- **`adapter_online` is false** — the adapter process is absent or its lease
  expired. Inspect that host process; do not make the model poll manually.
- **Endpoint already has a live adapter** — another adapter owns that endpoint.
  Use a different endpoint for a different session or wait for the stale lease
  to expire after confirming the old process is gone.
- **Bridge identity mismatch** — one process points at another SQLite file. Fix
  the path; never bypass the bridge-ID check.
- **Claude shows no channel** — verify Claude Code is at least 2.1.80, the MCP
  server name is exactly `agent-chat`, and the development-channel flag names
  `server:agent-chat`.
- **Managed Codex daemon fails on Windows** — omit `--transport`; standalone
  stdio is the Windows default.
- **A custom `codex exec` wrapper looks silent behind `tail`** — do not place a
  live run behind `2>&1 | tail`. `tail` withholds its window until the upstream
  process closes, so a completed turn followed by a stuck shutdown looks like
  a crash and hides the final report. Prefer this repository's app-server
  adapter. If a separate CLI workflow is unavoidable, stream `--json` through
  `tee`, retain the pipeline exit status, and optionally use
  `--output-last-message` as a clean-shutdown convenience—not as the sole
  completion or liveness signal.
- **A message is `injected` but not `observed`** — the host accepted transport
  bytes, but the model has not yet processed the event. It may still be inside a
  long foreground turn.
- **A message is `acted` but not `replied`** — work or progress began, but no
  final answer exists yet. The adapter lease keeps that state recoverable.
