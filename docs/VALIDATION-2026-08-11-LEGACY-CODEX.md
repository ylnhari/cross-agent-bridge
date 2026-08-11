# Legacy Codex and Windows UI validation — 2026-08-11

This addendum records the validation added after the immutable cross-turn
snapshot. It contains no bridge payloads, session identifiers, account data, or
machine-specific paths.

## Environment

- Windows 11
- Python 3.12.1
- Codex CLI 0.146.0
- Claude Code 2.1.227
- package candidate `0.3.0.dev0`

## Deterministic and package checks

- The complete suite passed: 103 tests in 80.997 seconds.
- The focused Codex unit and process-boundary suite passed: 25 tests.
- Pre-commit, Ruff lint, Ruff format, and Python compilation passed.
- A 48,561-byte wheel installed in a clean virtual environment. All four
  console entry points ran from the installed wheel, and
  `agent-chat-codex --help` exposed `--legacy-cli-bridge`.

## Repeated process-boundary stress

- Ten simultaneously launched steer/isolation tests passed.
- Two final five-way saturation runs each passed all ten Codex adapter
  process-boundary tests: 100 executions total.
- A prior five-way run exposed a fixed ten-second harness timeout, and a second
  run still exceeded a temporary thirty-second wait once under full-suite
  saturation. The final harness keeps a bounded thirty-second wait and now
  reports the authoritative bridge status and visible history on any timeout.
  The two final saturation runs completed without timeout or loss.

## Real legacy-task proof

A disposable real Codex task was created without dynamic bridge tools, then
attached through `--legacy-cli-bridge` from the clean installed wheel. A
diagnostic root requested one progress update and one final reply, with no file
or external side effects.

The delivery completed on attempt one through:

```text
queued -> claimed -> injected -> observed -> acted -> replied
```

The peer received exactly one progress response and exactly one final response,
both under the original conversation. No delivery remained claimed, visible, or
unread. The adapter process tree was stopped, the disposable Codex task was
archived, and all ignored probe database/profile/log files were removed.

## Windows desktop visibility observation

One real adapter-created Codex task had a bridge-started turn active when a user
submitted a message from the Windows desktop app. The persisted rollout showed
both distinct turn IDs in the same task. The bridge turn continued controlling
tools while the desktop foregrounded the manual turn. A mobile Codex client
hydrated the bridge conversation, while the already-open Windows window did not
live-render it.

This proves that the work did not move to a hidden task; it also disproves a
stronger claim that the Windows desktop is a synchronized second controller for
a standalone app-server turn. README, protocol, setup, and release-readiness
documentation now state the limitation and instruct users to send interventions
through the bridge or wait for idle.
