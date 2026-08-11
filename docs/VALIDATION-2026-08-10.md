# Validation snapshot — 2026-08-10

This is a dated release-candidate snapshot, not a claim about every future
commit.

## Deterministic checks

- 34 unit and subprocess tests passed.
- The complete suite passed five consecutive rounds: 170 test executions.
- Covered direct and room routing, independent recipient acknowledgements,
  multiple sessions from one system, membership snapshots, conflicting retry
  keys, wrong bridge identity, foreign database refusal, claim expiry, release,
  crash/restart redelivery, concurrent claims, concurrent SQLite writers,
  environment-pinned commands, and a long-poll unblocked by another process.
- A dependency-free wheel built and installed in a clean virtual environment.
- The installed `agent-chat` entry point initialized and diagnosed a new bridge.

## Live session trial

Four independent Codex-hosted test sessions joined one disposable room. Two
declared the same agent system and two declared separate systems. They used only
the public CLI and synthetic text.

- One observer broadcast fanned out to all four endpoints.
- Every endpoint broadcast one natural-language question.
- Peers answered addressed questions with direct, reply-linked messages.
- The first turns ended while one direct answer remained queued for each
  endpoint.
- The same four sessions were resumed, retrieved and acknowledged those durable
  answers, and sent confirmation messages.
- Final result: 17 messages, zero unread deliveries, no duplicate active claim,
  and SQLite integrity `ok`.

One participant initially labeled receive exit code 3 as an anomaly. The
transport behaved correctly: code 3 is the documented `status=empty` result for
a bounded poll. The continuation prompt stated that contract explicitly, and
all resumed turns completed without anomaly.

## Limits of this evidence

The live sessions exercised real independent model turns but shared the Codex
host family. Cross-vendor model behavior was not claimed. Host neutrality is
instead covered at the process boundary: the protocol accepts any caller that
can invoke the CLI and parse JSON. A Claude, Codex, Gemini, or custom runtime
adapter still needs its own invocation/wake integration.

The trial used one Windows machine and local storage. CI covers multiple Python
versions on Windows and Linux after publication. Network filesystems,
multi-user authentication, remote hosts, and untrusted participants are outside
the 0.2 security model.
