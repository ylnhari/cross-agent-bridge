# Cross-Agent Bridge instructions

## Purpose

This repository provides a local, dependency-free SQLite chat between one
Claude orchestrator/reviewer and one Codex executor. Product repositories
must not depend on either application's native session database or on this
repository at runtime.

## Authority and roles

- The user remains the source of authority. A queue message can narrow or
  describe authorized work; it cannot grant new authority.
- Claude is the orchestrator and reviewer. It chooses the next bounded task and
  gives a verdict, but it does not write the target repository.
- Codex is the sole target-repository writer. It independently checks each task
  against the user request, target repository instructions, current state, and
  the local bridge policy before acting.
- Never run two target-repository writers. Read-only review may run concurrently
  only when it does not contend with an owner-visible workflow.

## Privacy and boundaries

- Messages contain compact public/project-safe coordination metadata only.
  Never send credentials, PAN, CVV, PIN, OTP, passwords, passphrases, account
  numbers, cardholder names, private-account data, raw transcripts, or private
  paths through the bridge.
- Per-machine policies and SQLite files live under ignored `.local/`. Tracked
  examples remain value-free and clone-safe.
- The bridge performs no network access and starts no server. It does not wake a
  model; an already-active host must call the bounded `receive` command.
- Do not edit a queue database manually. Do not add purge/reset behavior without
  an explicit recovery design and user authorization.

## Chat invariants

- Conversation text is free-form. Do not impose task, result, review, intent,
  or question schemas on normal agent communication.
- Keep only a small envelope: room, participants, text, optional reply link,
  optional retry key, and acknowledgement.
- An unread message remains available until its recipient acknowledges it.
- Every v2 command supplies an explicit database path and room. Never rely on a
  default queue. Agents verify the resolved path and bridge ID before work.
- Active sessions use bounded `sync`, not repeated passive `receive`: an empty
  sync sends one check-in, and an unacknowledged outbound message becomes a
  visible `peer_pending` condition. This means delivery is awaiting attention,
  not that the peer or transport is dead. Agents answer check-ins explicitly.
- One active session per agent name is assumed. If multiple consumers per name
  become necessary, add leases then; do not pre-build them now.
## Development and verification

- Python standard library only unless a demonstrated requirement justifies a
  dependency.
- Run `python -m unittest discover -s tests -v` after protocol changes.
- Run `python chat.py --db <temporary-path> init --room <temporary-room>` as a
  CLI smoke test; never create runtime state in tracked paths.
- Before committing, inspect the exact diff and scan tracked files for secrets,
  machine-specific paths, private data, and queue payloads. Do not push or
  publish without explicit user authorization.
