# Development guide

This guide gets a contributor from a fresh clone to the same checks enforced by
CI. The bridge core has no runtime dependencies; Ruff and pre-commit are
development-only tools.

## Prerequisites

- Python 3.11 or newer.
- Git.
- Node.js 20 or newer only when changing the Claude Channel adapter.

Use a virtual environment so development tools do not alter the system Python:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On macOS or Linux, activate the environment with
`source .venv/bin/activate` instead.

## Install the Git hooks

```powershell
pre-commit install
pre-commit run --all-files
```

The first command installs the hook for future commits. The second validates
the complete checkout once, rather than only files changed in the next commit.
Hooks check common repository mistakes and run Ruff linting and formatting.

If a hook rewrites a file, review the change, stage it again, and rerun the
command. CI runs the same hook configuration without modifying the submitted
commit.

## Run checks directly

Ruff is configured in `pyproject.toml`:

```powershell
ruff check .
ruff format --check .
```

To apply safe lint fixes and formatting locally:

```powershell
ruff check --fix .
ruff format .
```

Run the Python suite and packaging checks:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src tests
agent-chat --help
python -m pip wheel . --no-deps --wheel-dir dist
```

On macOS or Linux, use `export PYTHONPATH=src`.

Run the Claude Channel adapter suite when its JavaScript or protocol boundary
changes:

```powershell
npm ci --prefix adapters/claude-channel
npm test --prefix adapters/claude-channel
```

## What CI covers

The GitHub Actions workflow runs:

- pre-commit and Ruff on Python 3.12;
- the Python suite, compile check, console entry point, and wheel build on
  Python 3.11, 3.12, and 3.13 on both Ubuntu and Windows; and
- the Claude Channel adapter tests on Node.js 20 on both Ubuntu and Windows.

Use `pre-commit run --all-files` plus the Python and Node suites before opening
a pull request. A passing unit suite is not evidence of live-session delivery;
claims about Claude or Codex host behavior also require a real interactive-host
trial and recorded evidence.

## Intentional legacy exclusions

`chat.py` and `tests/test_chat.py` preserve the frozen schema-2 compatibility
surface. Ruff excludes them so adopting the formatter cannot create an
accidental compatibility diff. New code belongs under `src/agent_chat`, with
tests in the current schema-4 suite.

## Troubleshooting

- If `ruff`, `pre-commit`, or `agent-chat` is not found, activate the virtual
  environment before running the command.
- If pre-commit changes a file, review it, stage it again, and rerun
  `pre-commit run --all-files`.
- If the Node suite cannot resolve the MCP SDK, run
  `npm ci --prefix adapters/claude-channel` before `npm test`.
- If a local check differs from CI, confirm the Python and Node versions against
  the matrix above and reinstall the pinned development extra.

## Change expectations

- Keep message content free-form and protocol envelopes minimal.
- Add deterministic process-boundary tests for adapter changes.
- Add race, restart, lease, and failure-path coverage for protocol changes.
- Never commit bridge databases, prompts from real conversations, credentials,
  machine-specific paths, or private payloads.
- Follow [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md)
  before proposing publication or security-sensitive changes.
