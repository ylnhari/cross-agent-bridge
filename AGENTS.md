# Cross-Agent Bridge instructions

## Purpose

This repository provides a dependency-free, local SQLite group chat for agent
sessions. The protocol is agent-, model-, role-, and project-neutral. Product
repositories must not depend on application-native conversation databases.

## Authority and safety

- Authority sourcing and untrusted-message handling are owned by
  [prompts/participant.md](prompts/participant.md).
- Secrets/data handling, local-only storage, and the trust boundary are owned by
  [SECURITY.md](SECURITY.md).

## Protocol invariants

- Each concurrently active session uses a unique endpoint ID, even when several
  sessions belong to one agent system.
- One renewable adapter lease owns each live endpoint. A second adapter may take
  over only after release or expiry, and every claimed but unfinished delivery
  is then requeued.
- Room broadcasts atomically snapshot all other active endpoints. Direct sends
  name one or more active endpoints.
- Every recipient has an independent delivery claim and acknowledgement.
- Progress messages are nonterminal; only a final reply completes the parent
  delivery. Every root message has a conversation ID inherited by its replies.
- Receives claim atomically with an expiring visibility lease. Consumers are
  idempotent because processing is at least once.
- Retry-key reuse succeeds only for an identical immutable envelope; conflicting
  reuse fails.
- Only `init` creates a database. All later commands verify the explicit path,
  schema, and optionally pinned bridge ID.
- Message content is free-form. Do not add mandatory task/result schemas.
- Durable delivery is not attention delivery. Core code never claims to wake or
  keep a model alive; persistent host adapters own invocation and must record
  injected, observed, acted, and replied separately.

## Development

- Protocol/schema changes require a version gate and explicit migration plan.
- Preserve `chat.py` and `tests/test_chat.py` as frozen schema 2 (`chat.py`)
  compatibility until the legacy deployment is explicitly retired.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the stdlib-only/no-daemon
principles, local checks, test-coverage expectations, and the pre-publication
review process.
