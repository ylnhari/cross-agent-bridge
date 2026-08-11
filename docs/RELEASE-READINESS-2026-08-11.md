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
- Codex app-server tasks created or resumed by the adapter and visible in the
  Codex app.
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

- 95 deterministic Python tests passed; the suite covers core routing,
  concurrency, lifecycle, adapters, launchers, and package surfaces.
- 50 critical race, crash, retry, resource-bound, and recovery executions
  passed across five repetitions.
- Ruff lint and formatting checks passed.
- A 46,399-byte wheel installed into a clean virtual environment; all four
  console entry points ran from `site-packages` with no runtime package
  dependency.
- A fresh installed-wheel pair—one Claude session and one app-visible Codex
  task—completed bidirectional agent-started conversations and four concurrent
  roots. Eight roots were verified in one attempt each, with no duplicate or
  unexpected response.
- Earlier four-session validation proved two Claude and two Codex native hosts
  could hold unique leases and process eight concurrent roots without routing
  crossover.
- A final repository-wide security contract recorded complete coverage and
  zero reportable findings under the stated local trust boundary.
- An independent Claude reviewer rechecked every prior rejection and the full
  candidate, independently passed compilation and all 95 tests, and returned
  `VERDICT: APPROVE` with no blocker or high-severity issue.

Detailed lifecycle and environment evidence is in
[VALIDATION-2026-08-11-CROSS-TURN.md](VALIDATION-2026-08-11-CROSS-TURN.md).

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

## Nonblocking follow-up

The hard-kill tests currently wait 5.2 seconds for a 5-second test lease. They
passed the full and repeated suites, but a future test-only change may replace
the fixed delay with bounded polling for more margin on unusually loaded CI
runners.
