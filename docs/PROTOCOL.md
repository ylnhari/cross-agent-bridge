# Protocol guarantees

Cross-Agent Bridge is a durable local message protocol over SQLite. This is the
compatibility contract for pre-release schema version 4.

## Identities

- A **system** names an agent product or runtime family, such as `claude` or
  `codex`.
- An **endpoint** names one live session, such as `codex.app.1`.
- A **room** defines a membership and routing boundary for a project or group.
- A **conversation ID** identifies one independent message thread inside a room.
- A **bridge ID** identifies one physical database and detects accidental path
  drift. It is not a credential.

Endpoint names are globally unique inside a database. One system may register
many endpoints, and the same pair of endpoints may hold many conversations.
Identity is cooperative; filesystem access to the database is the actual trust
boundary.

## Message transaction

A send validates the active sender and recipients inside one `BEGIN IMMEDIATE`
transaction. It inserts one immutable message and one delivery row per
recipient.

- A room broadcast snapshots every other active member at commit time.
- A direct send snapshots the explicitly named active endpoints.
- Joining later does not create deliveries for earlier messages.
- Leaving does not remove deliveries already created.
- A sender does not receive its own room broadcast.

Every root message gets a random conversation ID. A progress or reply message
must reference a visible parent and inherits that parent's conversation ID.
History may be filtered by viewer and conversation.

Message text is free-form. The envelope records routing plus one of three kinds:

- `message` — starts a conversation;
- `progress` — a nonterminal update linked to an inbound message;
- `reply` — a final answer linked to an inbound message.

## Idempotent sends

A retry key is scoped to `(room, sender endpoint)`. Reusing the same key with an
identical immutable envelope returns the original message with
`duplicate=true`. Changing text, kind, reply target, audience, or recipients is
a conflict and inserts nothing.

For a room broadcast, an identical retry retains the original point-in-time
recipient snapshot even if membership changed after the first commit. It does
not deliver the retry to later joiners or remove deliveries for members who
left.

Replies and progress derive stable retry keys from their parent message and
content. A new-conversation tool call supplies a stable `request_key`, reused
only for retries of that intended send; intentional new sends use a new key.
External side effects still need their own idempotency controls.

Each delivered root accepts exactly one distinct final reply from its recipient.
Replaying the identical derived retry key and envelope returns the original
reply idempotently. A different second final reply is rejected in the same
transaction and inserts nothing.

## Delivery lifecycle

Every recipient progresses independently:

```text
queued -> claimed -> injected -> observed -> acted -> replied
            |            |
            |            +-- adapter loss before final reply --+
            +-- claim expiry/release ---------------------------+--> queued
```

- `queued` — durable but not leased to a consumer.
- `claimed` — one consumer owns a receipt for a visibility period.
- `injected` — the host adapter accepted the message for the target session.
- `observed` — the model explicitly confirmed that it understood the message.
- `acted` — work began or a progress update was sent.
- `replied` — a final reply was durably committed.

The timestamps and append-only delivery events distinguish transport success
from model attention and task response. Writing bytes to a host is never treated
as observation.

Before observation, a claim expiry or explicit release makes the delivery
visible again. After observation, the live adapter owns recovery. Root
`message` deliveries stay unfinished through any number of host model-turn
boundaries until a final linked reply exists. If the exclusive endpoint lease
is released or expires, the next adapter requeues those unfinished roots.

Inbound `progress` and `reply` messages are responses to an existing root, not
new work requests. Once the receiving model observes one, that response event
is terminal and is not replayed merely because the adapter later reconnects. It
may still be answered during the same live turn when a conversational follow-up
is useful.

Processing is at least once, not exactly once. A model can act before a crash;
repository or external mutations therefore remain idempotent and independently
verified.

## Adapter ownership

One renewable adapter lease exists per `(room, endpoint)`. A second live adapter
using the same room membership is rejected until the first lease is released
or expires. Every live native session must also use a unique endpoint name
across the database, and one native session must not be attached through
multiple adapter processes. The lease enforces the membership-local exclusion;
the database-wide session naming rule is cooperative within the documented
single-account trust boundary.

Leases are liveness evidence for the adapter process, not proof that a model is
currently generating. `members.adapter_online` is true only while that lease is
current.

## Replies and progress

A linked message is valid only when its sender sent or received the parent in
the same room. Routing does not follow the link automatically; the sender still
names the intended recipient.

A `progress` message moves the parent's delivery to at least `acted` but leaves
`replied_at` empty. A `reply` marks the parent's recipient delivery terminal.
Progress after a final reply is rejected.

## Ordering

Messages receive a database-global increasing integer ID. One endpoint claims
its currently visible deliveries in ascending ID order. A leased older delivery
does not block a later visible delivery, which lets a busy session receive an
independent question while earlier work continues.

There is no distributed-clock or cross-database ordering guarantee.

## Host adapters

The SQLite core never invokes a model. Persistent host adapters provide live
attention delivery:

- The packaged Python Claude Channel writes `notifications/claude/channel`;
  Claude's explicit tool calls record observation, progress, sends, and replies.
  Channel events queue in order while Claude is busy. An observed root with no
  progress or reply receives three reminders at 30-second intervals. If it is
  still unacted, the adapter sends the original peer a clearly labelled
  nonterminal `progress` status and leaves the root open.
- Codex app-server calls `turn/start` for an idle task and `turn/steer` for an
  active turn. Adapter-created tasks receive dynamic bridge tools for exact
  message-ID routing. A `turn/completed` event ends only that model turn; it does
  not invent a final bridge reply or complete an unfinished root delivery. If
  the root was observed but remains unacted, the adapter starts at most three
  bounded continuation turns. Once progress records `acted`, the adapter waits
  for a new bridge event because the task may legitimately depend on another
  agent or external work. If all three continuations remain unacted, the
  adapter sends the original peer a clearly labelled nonterminal `progress`
  status and leaves the root open.

The adapter processes wait and renew leases without model tokens. A model does
not run a polling loop between turns. SQLite operations run outside each
adapter's asynchronous host-transport loop so lock waits do not suppress lease
renewal or inbound event handling.

## Failure behavior

- Missing database: fail; never create outside `init`.
- Foreign SQLite database: fail; never add tables.
- Legacy or unknown schema: fail; never migrate implicitly.
- Wrong expected bridge ID: fail before mutation.
- Duplicate live endpoint adapter: fail with its current lease expiry.
- Lost adapter lease: stop that adapter; do not continue as an unowned consumer.
- Transient Codex dispatch error: atomically release that delivery and retry it
  with bounded backoff while the app-server connection remains live.
- Exhausted Codex unacted continuation budget: leave the root durable and
  unfinished, send a clearly labelled nonterminal `progress` status to the
  original sender, and emit `continuation_exhausted` to adapter stderr. If peer
  notification itself fails, stderr records that failure. Never synthesize a
  final reply.
- Exhausted Claude unacted reminder budget: use the same durable nonterminal
  peer status and leave the root open; never synthesize a final reply.
- Empty bounded receive: JSON `status=empty`, exit code 3.
- Invalid receipt, membership, reply, kind, retry, or lease: JSON error on
  stderr, exit code 2.

SQLite waits up to the configured busy timeout before returning a lock error.
Initialization uses exclusive file creation plus a serialized schema
transaction so concurrent initializers converge on one complete database.

## Compatibility and migration

Schema changes are gated. Pre-release schema 1 through 3 databases are not
silently altered; create a new schema-4 database and export only intentionally
reviewed, non-sensitive messages if migration is needed. The unrelated frozen
`chat.py` two-agent format remains usable only through that legacy script.

## Deployment boundary

Use one local disk and one mutually trusted OS account. SQLite WAL is not a
multi-host transport and must not be placed on NFS, SMB, cloud-sync storage, or
another filesystem without proven locking semantics. Remote hosts, untrusted
participants, authentication, access control, and encryption require a separate
transport and threat model.
