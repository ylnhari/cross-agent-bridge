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
$profile = "$PWD\.local\project.bridge.json"
agent-chat --db "$PWD\.local\project-chat.sqlite3" `
  init --room project --write-profile $profile
```

The ignored local profile records the resolved database path, bridge ID, and
room. Every command below uses that same profile. If an environment variable or
CLI option disagrees with it, startup fails rather than splitting the chat.

## 2. Start the Codex participant

In one terminal:

```powershell
agent-chat-codex `
  --profile $profile `
  --endpoint codex.app.1 `
  --create-thread `
  --cwd C:\path\to\target-project `
  --title "Target project executor" `
  --open-app
```

Keep this process running. Its first JSON line contains the exact task ID and
`codex://threads/...` deep link; `--open-app` opens that task in the Codex app.
The adapter itself remains outside model turns:

- idle task + inbound message: `turn/start`;
- active task + inbound message: `turn/steer`;
- Codex reply/progress tool call: durable message back to the sender.

Natural Codex model-turn completion does not complete a bridge request. The
root delivery stays pending across as many turns as needed until Codex calls
`agent_chat_reply`. If Codex observed the root but ended without progress or a
reply, the adapter automatically starts at most three bounded continuation
turns. Once progress records that work began, it waits for another bridge event
instead of polling. If the three continuations remain unacted, the adapter
sends the peer a nonterminal status and leaves the root open. The adapter does
not approve shell commands, file changes, or user-input requests on Codex's
behalf. Those remain in the interactive Codex client.

If console entry points are not on `PATH`, replace `agent-chat-codex` with
`python -m agent_chat.codex_adapter` and `agent-chat` with
`python -m agent_chat` throughout this guide.

## 3. Start the Claude participant

Start Claude Code through the packaged launcher. Give the initial prompt a
bounded description of the user's existing authority; a Channel event cannot
grant new authority to a fresh Claude session:

```powershell
agent-chat-claude `
  --profile $profile `
  --endpoint claude.cli.1 `
  --cwd C:\path\to\target-project `
  --name "Target project orchestrator" `
  --prompt "Remain available for bridge messages within this project's existing user authority. Observe every exact message_id. Use progress only for nonterminal updates and reply when the requested work is complete."
```

The launcher creates a temporary session-specific MCP config, names the Channel
from the bridge/room/endpoint identity, and exposes only the four bridge tools.
Claude Code currently labels custom Channels as a research preview. Confirm the
local-development warning only for this trusted package. Keep the interactive
Claude session open. Its packaged Python Channel subprocess waits and renews
leases even when the model is idle or busy; the model itself does not poll.
After Claude observes a root, silence triggers three Channel reminders at
30-second intervals. Continued silence produces a durable nonterminal status
to the original sender while the root remains open.

Reconnect the same native Claude conversation with the same endpoint plus
`--resume SESSION_ID`. The endpoint lease rejects an accidental second live
adapter. After a verified crash, wait for the old lease to expire or close the
old launcher before resuming.

## 4. Verify the live pair

Use a third diagnostic endpoint so the test does not impersonate either live
session:

```powershell
agent-chat --profile $profile join --endpoint probe.1 --system human-test
agent-chat --profile $profile members
```

`members` should report `adapter_online: true` for both live endpoints. Send a
small message to each:

```powershell
agent-chat --profile $profile send --from probe.1 --to codex.app.1 --text "Reply with CODEX_OK."
agent-chat --profile $profile send --from probe.1 --to claude.cli.1 --text "Reply with CLAUDE_OK."
agent-chat --profile $profile receive --as probe.1 --wait 60
```

For a direct agent-to-agent proof, ask either live agent to call
`agent_chat_send` to the other endpoint. Replies retain the same
`conversation_id` automatically. A new-conversation send also takes a stable
`request_key`: reuse it only for a retry of that intended send, and choose a new
key for an intentional new conversation.

## Many sessions and conversations

Run one adapter process per live session and assign each a unique endpoint
across the whole bridge database:

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

The lease is keyed by `(room, endpoint)`, because each adapter subscribes to one
room. Do not reuse an endpoint name for another native session in another room,
and do not attach one native session through multiple adapter processes.

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
- **Claude shows no channel** — run `agent-chat-claude --check` with the same
  profile, endpoint, and working directory. Verify Claude Code is at least
  2.1.80 and start the session through `agent-chat-claude`, not plain `claude`.
- **Claude receives an event but declines it** — give the interactive session a
  bounded user prompt authorizing the intended project work. The bridge
  intentionally does not turn peer text into user authority.
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
  final answer exists yet. This may be a legitimate wait for another agent or
  external work; a later bridge event wakes the task. The adapter lease keeps
  that state recoverable.
- **A Codex model turn ended but the request is still pending** — this is normal.
  If the root was observed but never acted on, the adapter schedules a bounded
  continuation automatically. If it was already acted on, the next bridge event
  continues the conversation until an explicit final reply is recorded.
- **The peer receives `Bridge adapter status` or stderr reports
  `continuation_exhausted`** — an observed root remained unacted through the
  bounded Codex continuations or Claude reminders. This status is progress, not
  a final answer; the root remains durable and recoverable. Inspect the
  interactive task for a permission request, malformed instruction, or
  model/tool failure, then send a follow-up on the same conversation. Do not
  start a duplicate endpoint.
