# Contributing

Thank you for helping agents communicate more reliably.

## Principles

- Keep the message body free-form and the envelope small.
- Preserve the trusted-local, no-daemon boundary unless a separate design and
  threat model are approved.
- Prefer Python's standard library. Every dependency needs a concrete reason.
- Never commit a runtime database, transcript, credential, private path, or real
  user payload.
- Treat protocol compatibility, crash recovery, and concurrent behavior as
  correctness—not optional polish.

## Local checks

```powershell
python -m pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src tests
agent-chat --help
agent-chat-codex --help
agent-chat-claude --help
agent-chat-claude-channel --help
```

The hook set runs repository hygiene checks, Ruff linting, and Ruff formatting.
The complete environment setup, check matrix, and troubleshooting notes are in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

New protocol behavior needs tests at the core and subprocess boundaries. Changes
to claims, acknowledgements, adapter leases, retry keys, membership,
conversation routing, or schema must include race, restart, and failure-path
coverage. Host-adapter changes need a deterministic process test; release claims
about live attention require a real interactive host trial as well.

## Pull requests

Keep changes focused. Describe the invariant being changed, tests run, and any
compatibility or security effect. Schema changes require a bumped schema version
and an explicit migration or an intentional fail-closed decision.

Do not publish packages, create a public release, or push this pre-release
checkout solely because local checks pass. Publication requires an explicit
owner decision and an independent cross-system review.
