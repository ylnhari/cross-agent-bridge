# Validation snapshot — 2026-08-11 cross-turn release candidate

This is an immutable pre-release snapshot for schema 4 and package version
`0.3.0.dev0`. It contains only synthetic counts and host-level facts. It omits
native task IDs, session IDs, machine paths, prompts, private payloads, and
conversation transcripts.

## Supported claim

**PASS for durable, multi-turn communication among independently running
Claude Code interactive-terminal sessions and Codex app-visible tasks on one
local machine.**

The supported Claude surface is the genuine interactive CLI launched with a
per-session Channel opt-in. Claude Code Desktop Code-tab push delivery is not
claimed because Anthropic does not currently document an equivalent Channel
control there. Codex tasks created by the adapter were opened and visible in the
Codex app.

The bridge is still a pre-release, mutually trusted, single-OS-account local
tool. This snapshot is not a production-availability, remote-host, security-
isolation, or overnight-uptime claim.

## Candidate environment

- Windows with SQLite WAL on a local disk.
- Python 3.11 test runtime and Python 3.12 host runtime.
- Claude Code 2.1.227, interactive PTY, Sonnet with low effort.
- Codex CLI/app-server 0.146.0, Luna with low effort.
- No runtime Python package dependencies and no Node.js Channel dependency.

## Deterministic and package evidence

- 95 Python tests passed in 54.904 seconds. Coverage includes concurrent
  initialization, sender/membership serialization, atomic claims, database-ID
  pinning, Unicode JSONL, retry-key conflicts, response isolation, adapter lease
  takeover, Channel EOF cleanup, Channel reconnect, Codex dispatch retry,
  Codex turn steering, bounded protocol frames and queues, conflicting final-
  reply retries and conflicts, hard-kill lease recovery, nonblocking adapter
  database calls, bounded attention escalation, and observed-but-unacted
  continuation.
- The critical crash/takeover, nonblocking database, continuation exhaustion,
  duplicate/conflicting reply, concurrent-claim, send/leave, final-reply race,
  broadcast-retry, and oversized-frame set was repeated five times: 50
  executions passed in 71.711 seconds.
- Ruff lint and formatting checks passed.
- A 46,399-byte wheel was built, installed into a clean virtual environment,
  and imported from `site-packages`. All four console entry points ran from the
  installed wheel. The packaged Claude Channel required neither the repository
  checkout nor Node.js.

## Real-session evidence

### Root survives a natural Codex turn boundary

A genuine Codex task observed and acted on a root request, then naturally ended
its model turn without a final answer. The delivery remained `acted`; the
adapter did not synthesize a timeout reply. Codex opened a keyed question to a
genuine Claude session; Claude's answer later started a different Codex turn,
after which Codex completed the original root. The root had one delivery attempt
and this lifecycle:

```text
queued → claimed → injected → observed → acted → replied
```

### Installed-wheel Claude and Codex pair

One actual Claude Code session and one app-visible Codex task were launched from
the clean wheel environment. Both answered independent roots. Claude then
started a new keyed conversation with Codex, observed Codex's answer, and
completed its original request. The three conversations completed with exact
synthetic responses and zero retries.

### Four simultaneous native sessions

Two Claude Code sessions and two app-visible Codex tasks held four distinct
endpoint leases and four distinct native host references. Eight roots—two per
session—were sent concurrently. All eight received exactly one expected final
reply. Every root completed in one attempt with `queued`, `claimed`, `injected`,
`observed`, and `replied` evidence; no unexpected probe event remained.

### Final installed-wheel remediation proof

After lifecycle and recovery remediation, a newly built wheel launched one
fresh genuine Claude terminal session and one fresh app-visible Codex task. The
two sessions completed agent-started conversations in both directions and four
additional direct roots sent concurrently. Eight roots were verified in total;
all completed in one delivery attempt, every final response occurred exactly
once, no unexpected response was delivered, both adapters retained distinct
native host references, and SQLite integrity/WAL checks passed. The message
span was 62.724 seconds. Both synthetic process trees were then stopped, and
the exact synthetic Codex task was archived after its ID and name were checked.

### Release-candidate continuation defect and remediation

The first genuine multi-root run exposed a lifecycle gap: Codex could observe a
root, end the turn before progress or reply, and then receive no later peer event
to wake it. The delivery correctly remained durable, but no turn resumed it.

The candidate now schedules at most three continuation turns only for an
observed-but-unacted root. A fresh app-visible Codex task proved the exact case:
the first turn observed and ended; the adapter automatically opened a second
turn; Codex sent the final reply. The delivery completed in one attempt with
`queued`, `claimed`, `injected`, `observed`, `acted`, and `replied` evidence.
An exhausted budget leaves the root durable and reports a diagnostic instead of
inventing an answer. The final candidate also sends the original peer a clearly
labelled nonterminal status, so exhaustion is visible without watching adapter
stderr.

### Crash and reconnect

An unfinished Claude delivery was injected and its Channel process lost. EOF
cleanup released the endpoint lease and requeued the unfinished root. A new
Channel instance could acquire the endpoint and continue it. A separate hard-
kill process test proved an observed root requeued after the short test lease
expired and was claimed by a replacement as attempt two. Separately, an already
observed inbound reply remained terminal across reconnect and was not replayed
as new work.

## Operational conclusions

- Adapter processes, not model polling loops, wait for new messages.
- A model turn ending is not a bridge reply and does not end an unfinished root.
- An observed-but-unacted Codex root receives bounded automatic continuation;
  acted work waits efficiently for a new bridge event.
- An observed-but-unacted Claude root receives bounded Channel reminders. Both
  adapters emit durable nonterminal peer status after bounded recovery is
  exhausted, while keeping the root open.
- Root requests recover after adapter ownership loss; observed response events
  do not create acknowledgement loops after reconnect.
- A local profile pins database path, bridge ID, and room and rejects conflicting
  environment or command-line values.
- Each concurrent session still requires a unique endpoint and its own launcher
  process. User authority and host permission prompts remain outside the bridge.

## Release review

An independent, nonpersistent Claude Code 2.1.227 reviewer inspected the full
candidate against its base revision after remediation. The reviewer directly
verified all six earlier release-blocking findings, checked the medium concerns,
ran compilation and all 95 tests independently, and returned `VERDICT:
APPROVE`. It found no blocker or high-severity issue.

The only nonblocking follow-up was test robustness: the two hard-kill tests use
a 5-second lease and a 5.2-second assertion delay. Those paths passed in the
full suite and in all five repeated critical-suite runs, but polling to a wider
deadline may be friendlier to unusually loaded CI hosts.

A repository-wide final security contract was also sealed with complete
coverage and zero reportable findings under the documented mutually trusted,
single-account local threat model. The review and scan artifacts contain no
private message payloads or native session identifiers.
