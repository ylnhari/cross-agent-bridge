# Validation snapshot — 2026-08-11

This is an immutable pre-release validation snapshot for schema 4. It records
synthetic test evidence only. No private prompts, repository payloads, task IDs,
database paths, or host identifiers are included.

## Verdict

**PASS for live Claude Code terminal ↔ Codex app-task communication on one
Windows host.** The release gate remains **OPEN** because Claude Code Desktop
Code-tab push delivery has not been demonstrated and a different agentic system
must still approve the exact release candidate.

No package, repository, or marketing material was published by this run.

## Validated hosts

- Windows, local SQLite WAL database on a local disk.
- Python 3.12.1 and Node.js 24.13.0.
- Codex CLI/app-server 0.146.0; adapter-created tasks were independently visible
  and readable in the Codex app.
- Claude Code 2.1.226 in a genuine interactive PTY session with the custom
  Channel development opt-in.

The Claude test was not `claude -p`, a mock model, or a queue poll performed by
the model. The Channel subprocess remained alive independently of Claude turns.
The Claude Code Desktop Code tab was not used for this proof.

## Automated evidence

- 57 Python tests passed, including process-boundary CLI, concurrent
  initialization, sender/leave serialization, atomic claims, wrong-database
  failure, delivery lifecycle, adapter takeover, Unicode JSONL, and Codex
  `turn/steer` isolation. A Codex-adapter subprocess test also rejects a
  duplicate endpoint owner, hard-kills the active owner, waits for lease expiry,
  takes over the endpoint, and completes the original unfinished conversation.
  Another injects a transient app-server dispatch failure and proves the same
  adapter retries successfully instead of exiting.
- 4 Node tests passed against real Python child processes and the Claude Channel
  process boundary, including UTF-8 stdin, cancellable long-poll shutdown, and
  EOF cleanup that releases both its endpoint lease and unfinished delivery. A
  real MCP client/server test proves that a repeated new-conversation key
  deduplicates, conflicting reuse fails, and a new key permits an intentional
  identical message.
- Python compilation, Node syntax checks, and `git diff --check` passed.

## Real-session scenarios

### Long Codex turn with mid-turn Claude steering

One genuine Codex task remained active for 41.364 seconds. It observed a root
message, sent nonterminal progress, and started a separate conversation with
Claude. Claude answered through its Channel while the Codex turn was still
active. The Codex adapter delivered that answer with `turn/steer`; Codex
observed it, answered Claude, and then completed the original request.

Evidence showed the root delivery transition:

```text
queued → claimed → injected → observed → acted → replied
```

The progress message and final reply shared the root conversation ID. The
Claude round trip used a different conversation ID.

After reliability remediation, this scenario was repeated from a fresh bridge
with a fresh genuine Claude Code PTY and a fresh app-visible Codex task. The
Codex turn remained active for 41.220 seconds. The app transcript contained the
Claude response as a second user item in that same turn, and the database again
recorded the full lifecycle above. The cross-agent text preserved an em dash,
accented Latin text, Devanagari, and emoji.

### Two simultaneous conversations to one busy Claude session

The same sender opened two root conversations with the same Claude endpoint.
Conversation A sent progress and remained open while Claude requested a Codex
clarification. Conversation B received its final reply while A was still open.
After the Codex clarification arrived, Claude completed A. Histories filtered by
conversation ID contained only their own root, progress, and reply messages.

### Multiple sessions from one provider

Two independent Codex app-server adapters, with unique endpoint IDs and unique
visible Codex tasks, held valid leases in the same room at the same time. Both
communicated with the same Claude endpoint without routing collision.

### Crash and takeover

A Claude Channel process had claimed a message when its host process was
terminated. After the endpoint lease expired, a new genuine Claude session took
the same endpoint lease. Lease takeover immediately requeued the unfinished
claim; the new session observed it, sent a new Codex conversation, and completed
the original request. No manual database edit or message replay was used.

### Unicode across real hosts

The first real pass exposed two Windows code-page bugs: redirected Python JSON
output and UTF-8 text piped into Python stdin. Both boundaries were changed to
an ASCII-only JSON wire plus explicit UTF-8 stdin decoding. The repeated
Claude-to-Codex exchange preserved an em dash, accented Latin text, Devanagari,
and emoji in SQLite and in the visible Codex task before Codex replied.

### Keyed new conversations in both directions

Fresh genuine Claude and Codex sessions exercised the final
`agent_chat_send(..., request_key)` contract in both directions. Codex initiated
a keyed Unicode conversation to Claude. Claude independently initiated a keyed
conversation to Codex, kept its probe request nonterminal with progress, then
completed it only after Codex replied. The two root conversations and their
agent-to-agent sub-conversations retained distinct conversation IDs.

One synthetic Codex instruction was linguistically ambiguous about the exact
reply token. The bridge correctly left that root in `acted` rather than falsely
marking it complete; a later clarification addressed the original message ID
and completed it. This was prompt rework, not message loss or route crossing.

## Remaining release gates

- Prove push delivery in the Claude Code Desktop Code tab if Anthropic exposes a
  supported per-session Channel opt-in there. Standard MCP connection is not
  sufficient because Channel delivery requires explicit opt-in.
- Obtain an explicit APPROVE verdict from a different agentic system over the
  exact candidate after all remediation.
- Run a longer unattended soak if the intended release claim includes
  overnight reliability rather than crash recovery and bounded stress.
- Keep the project pre-release until those claims are narrowed or proven.
