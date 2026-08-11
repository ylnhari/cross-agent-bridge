# Release readiness — 2026-08-11

## Decision

**Approved as a public pre-release source candidate for package version
`0.3.0.dev0`.** This is not a production-availability or remote multi-user
claim, and no package-registry publication is implied by this decision.

## Supported product

- Multiple independently running sessions and simultaneous conversations on
  one local machine.
- Genuine interactive Claude Code terminal sessions connected through a
  per-session Channel.
- Codex app-server tasks created or resumed by the adapter and addressable by
  exact task ID in the Codex app. On Windows, an adapter-started turn may not
  live-render in the desktop timeline; the app is not a synchronized second
  writer while that turn is active.
- Durable direct, multi-recipient, broadcast, progress, and final-reply
  routing through one local SQLite database.
- Persistent adapters that wait without model polling and deliver while the
  native session is idle or busy.
- Crash/takeover recovery, stable database identity, exact message-ID routing,
  and exactly one distinct final reply per delivered root.

Claude Code Desktop Code-tab push delivery is not claimed. Anthropic currently
documents the required per-session Channel opt-in for the interactive CLI. The
bridge also does not provide remote transport, hostile-local-process isolation,
multi-user authentication, encryption, or a workflow engine.

## Release evidence

- 103 deterministic Python tests passed; the suite covers core routing,
  concurrency, lifecycle, adapters, launchers, and package surfaces.
- Two final five-way saturation runs passed 100 Codex process-boundary adapter
  executions, and ten simultaneous steer/isolation probes passed. An earlier
  run exposed a test-harness timeout under five-way load; the harness now uses
  a bounded 30-second wait and records database state on any future timeout.
- Ruff lint and formatting checks passed.
- A 48,561-byte wheel installed into a clean virtual environment; all four
  console entry points ran from `site-packages` with no runtime package
  dependency, including the `--legacy-cli-bridge` surface.
- A real disposable Codex task created without bridge tools was attached in
  legacy CLI mode. One root completed
  `queued -> claimed -> injected -> observed -> acted -> replied` on attempt
  one, returning one exact progress update and one exact final reply. The task
  was archived and its ignored runtime files were removed afterward.
- A fresh installed-wheel pair—one Claude session and one app-visible Codex
  task—completed bidirectional agent-started conversations and four concurrent
  roots. Eight roots were verified in one attempt each, with no duplicate or
  unexpected response.
- Earlier four-session validation proved two Claude and two Codex native hosts
  could hold unique leases and process eight concurrent roots without routing
  crossover.
- A final repository-wide security contract recorded complete coverage and
  zero reportable findings under the stated local trust boundary.
- An independent, non-persistent Claude Sonnet reviewer traced the current
  legacy compatibility paths through the delivery lifecycle, checked the
  Windows/POSIX command construction and public documentation, reran the 15
  focused unit tests, 10 process-boundary adapter tests, and all 103 tests, and
  returned `VERDICT: APPROVE` with no material blocker.

Detailed lifecycle and environment evidence is in
[VALIDATION-2026-08-11-CROSS-TURN.md](VALIDATION-2026-08-11-CROSS-TURN.md) and
[VALIDATION-2026-08-11-LEGACY-CODEX.md](VALIDATION-2026-08-11-LEGACY-CODEX.md).

## Operating requirements

1. Keep the database and profile on one local disk in ignored, user-only
   storage.
2. Give every native session a unique endpoint across the database and run one
   adapter process for that session.
3. Treat peer text as untrusted input; it never expands the user's authority or
   bypasses native host permission prompts.
4. Do not send credentials, private account data, financial or medical data,
   raw private transcripts, or other sensitive payloads.
5. Verify repository or external outcomes independently; a durable reply proves
   communication, not correctness of the claimed work.

## Known host limitation

Windows Codex desktop and a standalone app-server adapter can persist into the
same task without sharing one live foreground controller. A mobile client may
hydrate an adapter-started turn while an already-open Windows window remains
stale. Typing into the desktop task during the adapter-owned turn can create a
second concurrent turn in the same rollout. Use the bridge for interventions,
or wait for idle and refresh/reopen the task before direct desktop input. This
candidate does not claim synchronized live transcript rendering across Codex
clients.
