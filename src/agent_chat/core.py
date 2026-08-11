"""SQLite protocol for durable, many-to-many local agent chat."""

from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 4
MAX_TEXT_BYTES = 32_768
MAX_LABEL_CHARS = 120
MAX_DIRECT_RECIPIENTS = 256
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DELIVERY_STATES = ("queued", "claimed", "injected", "observed", "acted", "replied")
DELIVERY_PROGRESS = {state: index for index, state in enumerate(DELIVERY_STATES)}
MESSAGE_KINDS = ("message", "progress", "reply")
SQLITE_BUSY_TIMEOUT_SECONDS = 10.0
SQLITE_BUSY_RETRY_SECONDS = 0.01


class ChatError(RuntimeError):
    """A safe, user-facing protocol or configuration error."""


def stamp(offset_seconds: float = 0) -> str:
    value = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_name(value: str, label: str) -> str:
    if not NAME.fullmatch(value):
        raise ChatError(f"{label} must be 1-64 characters: letters, numbers, dot, dash, underscore")
    return value


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _create_schema(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _tables(connection):
            connection.commit()
            return False

        statements = (
            """CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
            """CREATE TABLE rooms (
            room TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )""",
            """CREATE TABLE endpoints (
            endpoint TEXT PRIMARY KEY,
            system TEXT NOT NULL,
            label TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )""",
            """CREATE TABLE memberships (
            room TEXT NOT NULL REFERENCES rooms(room),
            endpoint TEXT NOT NULL REFERENCES endpoints(endpoint),
            joined_at TEXT NOT NULL,
            left_at TEXT,
            PRIMARY KEY (room, endpoint)
        )""",
            """CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL REFERENCES rooms(room),
            conversation_id TEXT NOT NULL,
            sender TEXT NOT NULL REFERENCES endpoints(endpoint),
            audience TEXT NOT NULL CHECK (audience IN ('room', 'direct')),
            kind TEXT NOT NULL CHECK (kind IN ('message', 'progress', 'reply')),
            text TEXT NOT NULL,
            reply_to INTEGER REFERENCES messages(id),
            dedupe_key TEXT,
            created_at TEXT NOT NULL
        )""",
            """CREATE TABLE adapter_leases (
            room TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            owner_token TEXT NOT NULL,
            adapter TEXT NOT NULL,
            host_ref TEXT,
            acquired_at TEXT NOT NULL,
            renewed_at TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            PRIMARY KEY (room, endpoint),
            FOREIGN KEY (room, endpoint) REFERENCES memberships(room, endpoint)
        )""",
            """CREATE TABLE deliveries (
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            recipient TEXT NOT NULL REFERENCES endpoints(endpoint),
            claim_token TEXT,
            claimed_at TEXT,
            claim_until TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            acked_at TEXT,
            state TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'claimed', 'injected', 'observed', 'acted', 'replied')),
            injected_at TEXT,
            observed_at TEXT,
            acted_at TEXT,
            replied_at TEXT,
            adapter TEXT,
            host_ref TEXT,
            PRIMARY KEY (message_id, recipient)
        )""",
            """CREATE TABLE delivery_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            recipient TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            state TEXT NOT NULL
                CHECK (state IN ('queued', 'claimed', 'injected', 'observed', 'acted', 'replied')),
            adapter TEXT,
            host_ref TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (message_id, recipient)
                REFERENCES deliveries(message_id, recipient) ON DELETE CASCADE,
            UNIQUE (message_id, recipient, attempt, state)
        )""",
            """CREATE UNIQUE INDEX messages_dedupe
            ON messages(room, sender, dedupe_key)
            WHERE dedupe_key IS NOT NULL""",
            """CREATE INDEX deliveries_inbox
            ON deliveries(recipient, acked_at, claim_until, message_id)""",
            """CREATE INDEX messages_room_history
            ON messages(room, id)""",
            """CREATE INDEX messages_conversation_history
            ON messages(room, conversation_id, id)""",
            """CREATE INDEX delivery_events_timeline
            ON delivery_events(message_id, recipient, id)""",
            """CREATE INDEX adapter_leases_expiry
            ON adapter_leases(lease_until)""",
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('bridge_id', ?)",
            (uuid.uuid4().hex,),
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('created_at', ?)",
            (stamp(),),
        )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
        return True


def _validate_schema(connection: sqlite3.Connection, path: Path) -> None:
    """Fail closed unless this is one complete database at the current schema."""
    tables = _tables(connection)
    if "metadata" not in tables:
        raise ChatError(f"not an Agent Chat database: {path}")
    version = _metadata(connection, "schema_version")
    if version is None:
        raise ChatError(
            "legacy two-agent database detected; keep using chat.py for that "
            "database or export it before creating a new group-chat database"
        )
    if version != str(SCHEMA_VERSION):
        raise ChatError(f"unsupported database schema {version}; expected {SCHEMA_VERSION}")
    required = {
        "rooms",
        "endpoints",
        "memberships",
        "messages",
        "deliveries",
        "delivery_events",
        "adapter_leases",
    }
    if not required.issubset(tables):
        raise ChatError(f"Agent Chat database has an incomplete schema: {path}")


def _is_sqlite_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _enable_wal_once(connection: sqlite3.Connection) -> bool:
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    if journal_mode.lower() == "wal":
        return True
    enabled_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
    return enabled_mode.lower() == "wal"


def _ensure_wal(connection: sqlite3.Connection) -> None:
    """Enable persistent WAL mode despite a concurrent initializer doing the same."""
    deadline = time.monotonic() + SQLITE_BUSY_TIMEOUT_SECONDS
    while True:
        try:
            if _enable_wal_once(connection):
                return
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy(exc):
                raise
        if time.monotonic() >= deadline:
            raise ChatError("could not enable SQLite WAL mode before the database became available")
        time.sleep(SQLITE_BUSY_RETRY_SECONDS)


def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    """Open one initialized bridge, creating it only when explicitly requested."""
    path = path.resolve()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)

    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=rw",
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
            uri=True,
        )
    except sqlite3.OperationalError as exc:
        if not create:
            raise ChatError(
                f"chat database does not exist or is not writable: {path}; "
                "initialize that exact path first"
            ) from exc
        raise
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        tables = _tables(connection)
        if not tables:
            if not create:
                raise ChatError(f"not an initialized Agent Chat database: {path}")
            _create_schema(connection)
        # Always re-read after creation. Another initializer may have won the
        # serialized schema transaction after this connection first saw no tables.
        _validate_schema(connection, path)
        connection.execute("PRAGMA foreign_keys = ON")
        _ensure_wal(connection)
        connection.execute("PRAGMA synchronous = FULL")
    except Exception:
        connection.close()
        raise
    return connection


def bridge_info(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    bridge_id = _metadata(connection, "bridge_id")
    if bridge_id is None:
        raise ChatError("chat database has no bridge identity")
    return {
        "id": bridge_id,
        "database": str(path.resolve()),
        "schema": SCHEMA_VERSION,
    }


def require_bridge(connection: sqlite3.Connection, expected: str | None) -> None:
    if expected is None:
        return
    expected = expected.strip().lower()
    actual = _metadata(connection, "bridge_id")
    if actual != expected:
        raise ChatError(f"bridge identity mismatch: expected {expected}, found {actual}")


def create_room(connection: sqlite3.Connection, room: str) -> dict[str, Any]:
    room = validate_name(room, "room")
    cursor = connection.execute(
        "INSERT OR IGNORE INTO rooms(room, created_at) VALUES (?, ?)",
        (room, stamp()),
    )
    return {"status": "ready", "room": room, "created": cursor.rowcount == 1}


def _require_room(connection: sqlite3.Connection, room: str) -> str:
    room = validate_name(room, "room")
    if connection.execute("SELECT 1 FROM rooms WHERE room=?", (room,)).fetchone() is None:
        raise ChatError(f"room does not exist: {room}")
    return room


def _require_member(
    connection: sqlite3.Connection,
    room: str,
    endpoint: str,
    *,
    active: bool = True,
    touch: bool = True,
) -> tuple[str, str]:
    room = _require_room(connection, room)
    endpoint = validate_name(endpoint, "endpoint")
    row = connection.execute(
        """
        SELECT e.system, m.left_at
        FROM memberships AS m
        JOIN endpoints AS e ON e.endpoint=m.endpoint
        WHERE m.room=? AND m.endpoint=?
        """,
        (room, endpoint),
    ).fetchone()
    if row is None:
        raise ChatError(f"endpoint has never joined room {room}: {endpoint}")
    if active and row["left_at"] is not None:
        raise ChatError(f"endpoint is not an active member of room {room}: {endpoint}")
    if touch:
        connection.execute(
            "UPDATE endpoints SET last_seen_at=? WHERE endpoint=?",
            (stamp(), endpoint),
        )
    return room, str(row["system"])


def join(
    connection: sqlite3.Connection,
    *,
    room: str,
    endpoint: str,
    system: str,
    label: str | None = None,
) -> dict[str, Any]:
    room = _require_room(connection, room)
    endpoint = validate_name(endpoint, "endpoint")
    system = validate_name(system, "system")
    if label is not None:
        label = label.strip()
        if not label:
            label = None
        elif len(label) > MAX_LABEL_CHARS:
            raise ChatError(f"label exceeds {MAX_LABEL_CHARS} characters")

    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT * FROM endpoints WHERE endpoint=?", (endpoint,)
        ).fetchone()
        now = stamp()
        if existing is None:
            connection.execute(
                """
                INSERT INTO endpoints(endpoint, system, label, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (endpoint, system, label, now, now),
            )
        else:
            if existing["system"] != system:
                raise ChatError(
                    f"endpoint {endpoint} is already registered to system {existing['system']}"
                )
            if label is not None and existing["label"] not in (None, label):
                raise ChatError(f"endpoint {endpoint} already has a different label")
            connection.execute(
                """
                UPDATE endpoints
                SET label=COALESCE(label, ?), last_seen_at=?
                WHERE endpoint=?
                """,
                (label, now, endpoint),
            )

        membership = connection.execute(
            "SELECT left_at FROM memberships WHERE room=? AND endpoint=?",
            (room, endpoint),
        ).fetchone()
        joined = membership is None or membership["left_at"] is not None
        if membership is None:
            connection.execute(
                """
                INSERT INTO memberships(room, endpoint, joined_at, left_at)
                VALUES (?, ?, ?, NULL)
                """,
                (room, endpoint, now),
            )
        elif membership["left_at"] is not None:
            connection.execute(
                """
                UPDATE memberships SET joined_at=?, left_at=NULL
                WHERE room=? AND endpoint=?
                """,
                (now, room, endpoint),
            )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {
        "status": "joined",
        "joined": joined,
        "member": {"room": room, "endpoint": endpoint, "system": system, "label": label},
    }


def leave(connection: sqlite3.Connection, *, room: str, endpoint: str) -> dict[str, Any]:
    room, _ = _require_member(connection, room, endpoint)
    connection.execute(
        "UPDATE memberships SET left_at=? WHERE room=? AND endpoint=?",
        (stamp(), room, endpoint),
    )
    return {"status": "left", "room": room, "endpoint": endpoint}


def members(connection: sqlite3.Connection, *, room: str) -> dict[str, Any]:
    room = _require_room(connection, room)
    rows = connection.execute(
        """
        SELECT e.endpoint, e.system, e.label, m.joined_at,
               e.last_seen_at AS last_command_at,
               l.adapter, l.host_ref AS adapter_host_ref,
               l.lease_until AS adapter_lease_until,
               CASE WHEN l.lease_until>? THEN 1 ELSE 0 END AS adapter_online
        FROM memberships AS m
        JOIN endpoints AS e ON e.endpoint=m.endpoint
        LEFT JOIN adapter_leases AS l
          ON l.room=m.room AND l.endpoint=m.endpoint
        WHERE m.room=? AND m.left_at IS NULL
        ORDER BY e.endpoint
        """,
        (stamp(), room),
    ).fetchall()
    values = []
    for row in rows:
        value = dict(row)
        value["adapter_online"] = bool(value["adapter_online"])
        values.append(value)
    return {
        "status": "ok",
        "room": room,
        "members": values,
    }


def _message(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_text: bool = True,
) -> dict[str, Any]:
    recipients = [
        str(item["recipient"])
        for item in connection.execute(
            "SELECT recipient FROM deliveries WHERE message_id=? ORDER BY recipient",
            (row["id"],),
        )
    ]
    result: dict[str, Any] = {
        "id": row["id"],
        "room": row["room"],
        "conversation_id": row["conversation_id"],
        "from": row["sender"],
        "audience": row["audience"],
        "kind": row["kind"],
        "to": recipients,
        "reply_to": row["reply_to"],
        "created_at": row["created_at"],
    }
    if include_text:
        result["text"] = row["text"]
    return result


def _record_delivery_event(
    connection: sqlite3.Connection,
    *,
    message_id: int,
    recipient: str,
    attempt: int,
    state: str,
    adapter: str | None = None,
    host_ref: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO delivery_events(
            message_id, recipient, attempt, state, adapter, host_ref, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (message_id, recipient, attempt, state, adapter, host_ref, stamp()),
    )


def _validate_adapter_lease(
    *,
    owner_token: str,
    adapter: str,
    host_ref: str | None,
    ttl: float,
) -> tuple[str, str, str | None, float]:
    owner_token = validate_name(owner_token, "adapter owner token")
    adapter = validate_name(adapter, "adapter")
    if host_ref is not None:
        host_ref = host_ref.strip() or None
        if host_ref is not None and len(host_ref) > 256:
            raise ChatError("host reference exceeds 256 characters")
    if not 5 <= ttl <= 3600:
        raise ChatError("adapter lease TTL must be between 5 and 3600 seconds")
    return owner_token, adapter, host_ref, ttl


def _requeue_incomplete(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
) -> int:
    rows = connection.execute(
        """
        SELECT d.message_id, d.attempts
        FROM deliveries AS d
        JOIN messages AS m ON m.id=d.message_id
        WHERE m.room=? AND d.recipient=? AND d.replied_at IS NULL
          AND d.state IN ('claimed', 'injected', 'observed', 'acted')
        ORDER BY d.message_id
        """,
        (room, recipient),
    ).fetchall()
    for row in rows:
        connection.execute(
            """
            UPDATE deliveries
            SET claim_token=NULL, claimed_at=NULL, claim_until=NULL,
                acked_at=NULL, state='queued', injected_at=NULL,
                observed_at=NULL, acted_at=NULL, adapter=NULL, host_ref=NULL
            WHERE message_id=? AND recipient=? AND replied_at IS NULL
            """,
            (row["message_id"], recipient),
        )
        _record_delivery_event(
            connection,
            message_id=int(row["message_id"]),
            recipient=recipient,
            attempt=int(row["attempts"]),
            state="queued",
        )
    return len(rows)


def acquire_adapter_lease(
    connection: sqlite3.Connection,
    *,
    room: str,
    endpoint: str,
    owner_token: str,
    adapter: str,
    host_ref: str | None = None,
    ttl: float = 30,
) -> dict[str, Any]:
    """Claim exclusive adapter ownership for one live-session endpoint."""
    room = validate_name(room, "room")
    endpoint = validate_name(endpoint, "endpoint")
    owner_token, adapter, host_ref, ttl = _validate_adapter_lease(
        owner_token=owner_token,
        adapter=adapter,
        host_ref=host_ref,
        ttl=ttl,
    )
    now = stamp()
    lease_until = stamp(ttl)
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_member(connection, room, endpoint)
        existing = connection.execute(
            "SELECT * FROM adapter_leases WHERE room=? AND endpoint=?",
            (room, endpoint),
        ).fetchone()
        same_owner = existing is not None and existing["owner_token"] == owner_token
        if existing is not None and not same_owner and str(existing["lease_until"]) > now:
            raise ChatError(
                f"endpoint {endpoint} already has a live {existing['adapter']} adapter "
                f"until {existing['lease_until']}"
            )
        recovered = 0
        if not same_owner:
            recovered = _requeue_incomplete(
                connection,
                room=room,
                recipient=endpoint,
            )
        acquired_at = str(existing["acquired_at"]) if same_owner else now
        connection.execute(
            """
            INSERT INTO adapter_leases(
                room, endpoint, owner_token, adapter, host_ref,
                acquired_at, renewed_at, lease_until
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(room, endpoint) DO UPDATE SET
                owner_token=excluded.owner_token,
                adapter=excluded.adapter,
                host_ref=excluded.host_ref,
                acquired_at=excluded.acquired_at,
                renewed_at=excluded.renewed_at,
                lease_until=excluded.lease_until
            """,
            (
                room,
                endpoint,
                owner_token,
                adapter,
                host_ref,
                acquired_at,
                now,
                lease_until,
            ),
        )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {
        "status": "acquired",
        "room": room,
        "endpoint": endpoint,
        "adapter": adapter,
        "host_ref": host_ref,
        "lease_until": lease_until,
        "recovered": recovered,
    }


def renew_adapter_lease(
    connection: sqlite3.Connection,
    *,
    room: str,
    endpoint: str,
    owner_token: str,
    ttl: float = 30,
) -> dict[str, Any]:
    room = validate_name(room, "room")
    endpoint = validate_name(endpoint, "endpoint")
    owner_token = validate_name(owner_token, "adapter owner token")
    if not 5 <= ttl <= 3600:
        raise ChatError("adapter lease TTL must be between 5 and 3600 seconds")
    now = stamp()
    lease_until = stamp(ttl)
    cursor = connection.execute(
        """
        UPDATE adapter_leases SET renewed_at=?, lease_until=?
        WHERE room=? AND endpoint=? AND owner_token=? AND lease_until>?
        """,
        (now, lease_until, room, endpoint, owner_token, now),
    )
    if cursor.rowcount != 1:
        raise ChatError("live adapter lease was lost or expired")
    return {
        "status": "renewed",
        "room": room,
        "endpoint": endpoint,
        "lease_until": lease_until,
    }


def release_adapter_lease(
    connection: sqlite3.Connection,
    *,
    room: str,
    endpoint: str,
    owner_token: str,
) -> dict[str, Any]:
    room = validate_name(room, "room")
    endpoint = validate_name(endpoint, "endpoint")
    owner_token = validate_name(owner_token, "adapter owner token")
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT owner_token FROM adapter_leases WHERE room=? AND endpoint=?",
            (room, endpoint),
        ).fetchone()
        if existing is None:
            connection.commit()
            return {
                "status": "released",
                "already_released": True,
                "room": room,
                "endpoint": endpoint,
                "requeued": 0,
            }
        if existing["owner_token"] != owner_token:
            raise ChatError("adapter owner token does not match the live lease")
        connection.execute(
            "DELETE FROM adapter_leases WHERE room=? AND endpoint=?",
            (room, endpoint),
        )
        requeued = _requeue_incomplete(
            connection,
            room=room,
            recipient=endpoint,
        )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {
        "status": "released",
        "already_released": False,
        "room": room,
        "endpoint": endpoint,
        "requeued": requeued,
    }


def _normalize_recipients(values: Iterable[str]) -> list[str]:
    recipients = sorted({validate_name(value, "recipient") for value in values})
    if not recipients:
        raise ChatError("at least one recipient is required")
    if len(recipients) > MAX_DIRECT_RECIPIENTS:
        raise ChatError(
            f"direct audience exceeds {MAX_DIRECT_RECIPIENTS} endpoints; use a room broadcast"
        )
    return recipients


def send(
    connection: sqlite3.Connection,
    *,
    room: str,
    sender: str,
    text: str,
    recipients: Iterable[str] = (),
    broadcast: bool = False,
    reply_to: int | None = None,
    dedupe_key: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    room = validate_name(room, "room")
    sender = validate_name(sender, "sender")
    if not text.strip():
        raise ChatError("message text cannot be empty")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ChatError(f"message text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
    if dedupe_key is not None:
        dedupe_key = validate_name(dedupe_key, "dedupe key")
    if kind is not None and kind not in MESSAGE_KINDS:
        raise ChatError("message kind must be message, progress, or reply")
    if reply_to is None:
        if kind not in (None, "message"):
            raise ChatError("progress and reply messages require reply_to")
        kind = "message"
    else:
        if kind == "message":
            raise ChatError("a message with reply_to must be progress or reply")
        kind = kind or "reply"
    requested = list(recipients)
    if broadcast and requested:
        raise ChatError("choose either room broadcast or direct recipients, not both")
    audience = "room" if broadcast else "direct"
    requested_recipients = [] if broadcast else _normalize_recipients(requested)
    if sender in requested_recipients:
        raise ChatError("sender cannot be a recipient of its own message")

    connection.execute("BEGIN IMMEDIATE")
    try:
        room, _ = _require_member(connection, room, sender)
        if dedupe_key is not None:
            existing = connection.execute(
                """
                SELECT * FROM messages
                WHERE room=? AND sender=? AND dedupe_key=?
                """,
                (room, sender, dedupe_key),
            ).fetchone()
            if existing is not None:
                prior_recipients = [
                    str(row["recipient"])
                    for row in connection.execute(
                        """
                        SELECT recipient FROM deliveries
                        WHERE message_id=? ORDER BY recipient
                        """,
                        (existing["id"],),
                    )
                ]
                routing_matches = (
                    existing["audience"] == "room"
                    if broadcast
                    else existing["audience"] == "direct"
                    and prior_recipients == requested_recipients
                )
                same = (
                    existing["text"] == text
                    and existing["reply_to"] == reply_to
                    and existing["kind"] == kind
                    and routing_matches
                )
                if not same:
                    raise ChatError(
                        "dedupe key was already used with different message content or routing"
                    )
                connection.commit()
                return {
                    "status": "sent",
                    "duplicate": True,
                    "message": _message(connection, existing),
                }

        if broadcast:
            recipient_list = [
                str(row["endpoint"])
                for row in connection.execute(
                    """
                    SELECT endpoint FROM memberships
                    WHERE room=? AND left_at IS NULL AND endpoint<>?
                    ORDER BY endpoint
                    """,
                    (room, sender),
                )
            ]
            if not recipient_list:
                raise ChatError("room broadcast has no other active members")
        else:
            recipient_list = requested_recipients
            placeholders = ",".join("?" for _ in recipient_list)
            active = {
                str(row["endpoint"])
                for row in connection.execute(
                    f"""
                    SELECT endpoint FROM memberships
                    WHERE room=? AND left_at IS NULL
                      AND endpoint IN ({placeholders})
                    """,
                    (room, *recipient_list),
                )
            }
            missing = [item for item in recipient_list if item not in active]
            if missing:
                raise ChatError("recipients are not active room members: " + ", ".join(missing))

        conversation_id = uuid.uuid4().hex
        if reply_to is not None:
            parent = connection.execute(
                "SELECT * FROM messages WHERE id=? AND room=?",
                (reply_to, room),
            ).fetchone()
            if parent is None:
                raise ChatError("reply_to does not exist in this room")
            visible = (
                parent["sender"] == sender
                or connection.execute(
                    """
                SELECT 1 FROM deliveries
                WHERE message_id=? AND recipient=?
                """,
                    (reply_to, sender),
                ).fetchone()
                is not None
            )
            if not visible:
                raise ChatError("reply_to is not visible to this sender")
            parent_delivery = connection.execute(
                """
                SELECT 1 FROM deliveries
                WHERE message_id=? AND recipient=?
                """,
                (reply_to, sender),
            ).fetchone()
            if parent_delivery is not None and str(parent["sender"]) not in recipient_list:
                raise ChatError("a response to an inbound message must include its original sender")
            conversation_id = str(parent["conversation_id"])

        cursor = connection.execute(
            """
            INSERT INTO messages(
                room, conversation_id, sender, audience, kind, text, reply_to,
                dedupe_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room,
                conversation_id,
                sender,
                audience,
                kind,
                text,
                reply_to,
                dedupe_key,
                stamp(),
            ),
        )
        message_id = int(cursor.lastrowid)
        connection.executemany(
            """
            INSERT INTO deliveries(message_id, recipient)
            VALUES (?, ?)
            """,
            ((message_id, recipient) for recipient in recipient_list),
        )
        connection.executemany(
            """
            INSERT INTO delivery_events(
                message_id, recipient, attempt, state, created_at
            ) VALUES (?, ?, 0, 'queued', ?)
            """,
            ((message_id, recipient, stamp()) for recipient in recipient_list),
        )
        if reply_to is not None and kind == "reply":
            parent_delivery = connection.execute(
                """
                SELECT attempts, state FROM deliveries
                WHERE message_id=? AND recipient=?
                """,
                (reply_to, sender),
            ).fetchone()
            if parent_delivery is not None:
                replied_at = stamp()
                connection.execute(
                    """
                    UPDATE deliveries
                    SET state='replied',
                        acked_at=COALESCE(acked_at, ?),
                        observed_at=COALESCE(observed_at, ?),
                        acted_at=COALESCE(acted_at, ?),
                        replied_at=COALESCE(replied_at, ?),
                        claim_until=NULL
                    WHERE message_id=? AND recipient=?
                    """,
                    (
                        replied_at,
                        replied_at,
                        replied_at,
                        replied_at,
                        reply_to,
                        sender,
                    ),
                )
                _record_delivery_event(
                    connection,
                    message_id=reply_to,
                    recipient=sender,
                    attempt=int(parent_delivery["attempts"]),
                    state="replied",
                )
        elif reply_to is not None and kind == "progress":
            parent_delivery = connection.execute(
                """
                SELECT attempts, state FROM deliveries
                WHERE message_id=? AND recipient=?
                """,
                (reply_to, sender),
            ).fetchone()
            if parent_delivery is not None:
                if parent_delivery["state"] == "replied":
                    raise ChatError("cannot send progress after a final reply")
                acted_at = stamp()
                connection.execute(
                    """
                    UPDATE deliveries
                    SET state=CASE
                            WHEN state IN ('queued', 'claimed', 'injected', 'observed')
                            THEN 'acted' ELSE state END,
                        acked_at=COALESCE(acked_at, ?),
                        observed_at=COALESCE(observed_at, ?),
                        acted_at=COALESCE(acted_at, ?),
                        claim_until=NULL
                    WHERE message_id=? AND recipient=?
                    """,
                    (acted_at, acted_at, acted_at, reply_to, sender),
                )
                _record_delivery_event(
                    connection,
                    message_id=reply_to,
                    recipient=sender,
                    attempt=int(parent_delivery["attempts"]),
                    state="acted",
                )
        row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {
        "status": "sent",
        "duplicate": False,
        "message": _message(connection, row),
    }


def receive_one(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
    visibility: float = 300,
    touch: bool = True,
) -> dict[str, Any] | None:
    room, _ = _require_member(
        connection,
        room,
        recipient,
        active=False,
        touch=touch,
    )
    recipient = validate_name(recipient, "recipient")
    if not 0 < visibility <= 86_400:
        raise ChatError("visibility must be greater than 0 and at most 86400 seconds")
    now = stamp()
    claim_until = stamp(visibility)
    claim_id = uuid.uuid4().hex

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT m.*, d.attempts
            FROM deliveries AS d
            JOIN messages AS m ON m.id=d.message_id
            WHERE m.room=? AND d.recipient=? AND d.acked_at IS NULL
              AND (d.claim_until IS NULL OR d.claim_until<=?)
            ORDER BY m.id
            LIMIT 1
            """,
            (room, recipient, now),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        connection.execute(
            """
            UPDATE deliveries
            SET claim_token=?, claimed_at=?, claim_until=?, attempts=attempts+1,
                state='claimed', injected_at=NULL, adapter=NULL, host_ref=NULL
            WHERE message_id=? AND recipient=?
            """,
            (claim_id, now, claim_until, row["id"], recipient),
        )
        _record_delivery_event(
            connection,
            message_id=int(row["id"]),
            recipient=recipient,
            attempt=int(row["attempts"]) + 1,
            state="claimed",
        )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {
        "message": _message(connection, row),
        "delivery": {
            "recipient": recipient,
            "attempt": int(row["attempts"]) + 1,
            "receipt": claim_id,
            "visible_again_at": claim_until,
            "state": "claimed",
        },
    }


def mark_delivery(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
    message_id: int,
    receipt: str,
    state: str,
    adapter: str | None = None,
    host_ref: str | None = None,
) -> dict[str, Any]:
    """Record adapter/session progress for one claimed delivery."""
    room, _ = _require_member(connection, room, recipient, active=False)
    recipient = validate_name(recipient, "recipient")
    if state not in ("injected", "observed", "acted"):
        raise ChatError("delivery state must be injected, observed, or acted")
    if adapter is not None:
        adapter = validate_name(adapter, "adapter")
    if host_ref is not None:
        host_ref = host_ref.strip()
        if not host_ref:
            host_ref = None
        elif len(host_ref) > 256:
            raise ChatError("host reference exceeds 256 characters")

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT m.*, d.attempts, d.state, d.acked_at, d.claim_token,
                   d.claim_until
            FROM messages AS m
            JOIN deliveries AS d ON d.message_id=m.id
            WHERE m.id=? AND m.room=? AND d.recipient=?
            """,
            (message_id, room, recipient),
        ).fetchone()
        if row is None:
            raise ChatError("message was not delivered to this endpoint in this room")
        if row["claim_token"] != receipt:
            raise ChatError("receipt does not match the active delivery claim")
        if row["acked_at"] is None and (
            row["claim_until"] is None or row["claim_until"] <= stamp()
        ):
            raise ChatError("delivery claim expired; receive the message again")

        current = str(row["state"])
        target = state if DELIVERY_PROGRESS[state] >= DELIVERY_PROGRESS[current] else current
        changed_at = stamp()
        observed = DELIVERY_PROGRESS[target] >= DELIVERY_PROGRESS["observed"]
        acted = DELIVERY_PROGRESS[target] >= DELIVERY_PROGRESS["acted"]
        replied = DELIVERY_PROGRESS[target] >= DELIVERY_PROGRESS["replied"]
        connection.execute(
            """
            UPDATE deliveries
            SET state=?,
                injected_at=CASE
                    WHEN ? >= 2 THEN COALESCE(injected_at, ?) ELSE injected_at END,
                observed_at=CASE
                    WHEN ? THEN COALESCE(observed_at, ?) ELSE observed_at END,
                acted_at=CASE
                    WHEN ? THEN COALESCE(acted_at, ?) ELSE acted_at END,
                replied_at=CASE
                    WHEN ? THEN COALESCE(replied_at, ?) ELSE replied_at END,
                acked_at=CASE
                    WHEN ? THEN COALESCE(acked_at, ?) ELSE acked_at END,
                claim_until=CASE WHEN ? THEN NULL ELSE claim_until END,
                adapter=COALESCE(?, adapter),
                host_ref=COALESCE(?, host_ref)
            WHERE message_id=? AND recipient=?
            """,
            (
                target,
                DELIVERY_PROGRESS[target],
                changed_at,
                observed,
                changed_at,
                acted,
                changed_at,
                replied,
                changed_at,
                observed,
                changed_at,
                observed,
                adapter,
                host_ref,
                message_id,
                recipient,
            ),
        )
        if DELIVERY_PROGRESS[state] >= DELIVERY_PROGRESS[current]:
            _record_delivery_event(
                connection,
                message_id=message_id,
                recipient=recipient,
                attempt=int(row["attempts"]),
                state=target,
                adapter=adapter,
                host_ref=host_ref,
            )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {
        "status": "marked",
        "state": target,
        "message": _message(connection, row, include_text=False),
        "recipient": recipient,
    }


def renew_claim(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
    message_id: int,
    receipt: str,
    visibility: float,
) -> dict[str, Any]:
    """Extend an unacknowledged adapter claim while its host session is alive."""
    room, _ = _require_member(connection, room, recipient, active=False)
    recipient = validate_name(recipient, "recipient")
    if not 0 < visibility <= 86_400:
        raise ChatError("visibility must be greater than 0 and at most 86400 seconds")
    now = stamp()
    renewed_until = stamp(visibility)
    cursor = connection.execute(
        """
        UPDATE deliveries
        SET claim_until=?
        WHERE message_id=? AND recipient=? AND acked_at IS NULL
          AND claim_token=? AND claim_until>?
          AND EXISTS (
            SELECT 1 FROM messages
            WHERE messages.id=deliveries.message_id AND messages.room=?
          )
        """,
        (renewed_until, message_id, recipient, receipt, now, room),
    )
    if cursor.rowcount != 1:
        raise ChatError("active unacknowledged delivery claim does not exist")
    return {
        "status": "renewed",
        "room": room,
        "message_id": message_id,
        "recipient": recipient,
        "visible_again_at": renewed_until,
    }


def wait_for_message(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
    wait: float,
    poll: float,
    visibility: float,
) -> dict[str, Any] | None:
    if not 0 <= wait <= 3600:
        raise ChatError("wait must be between 0 and 3600 seconds")
    if not 0.05 <= poll <= 5:
        raise ChatError("poll must be between 0.05 and 5 seconds")
    room, _ = _require_member(connection, room, recipient, active=False)
    deadline = time.monotonic() + wait
    while True:
        delivery = receive_one(
            connection,
            room=room,
            recipient=recipient,
            visibility=visibility,
            touch=False,
        )
        if delivery is not None:
            return delivery
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))


def acknowledge(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
    message_id: int,
    receipt: str,
) -> dict[str, Any]:
    room, _ = _require_member(connection, room, recipient, active=False)
    recipient = validate_name(recipient, "recipient")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT m.*, d.attempts, d.state, d.acked_at, d.claim_token, d.claim_until
            FROM messages AS m
            JOIN deliveries AS d ON d.message_id=m.id
            WHERE m.id=? AND m.room=? AND d.recipient=?
            """,
            (message_id, room, recipient),
        ).fetchone()
        if row is None:
            raise ChatError("message was not delivered to this endpoint in this room")
        already = row["acked_at"] is not None
        if row["claim_token"] != receipt:
            raise ChatError("receipt does not match the active delivery claim")
        if not already:
            if row["claim_until"] is None or row["claim_until"] <= stamp():
                raise ChatError("delivery claim expired; receive the message again")
            observed_at = stamp()
            connection.execute(
                """
                UPDATE deliveries
                SET acked_at=?, observed_at=COALESCE(observed_at, ?),
                    state=CASE
                        WHEN state IN ('queued', 'claimed', 'injected')
                        THEN 'observed' ELSE state END,
                    claim_until=NULL
                WHERE message_id=? AND recipient=?
                """,
                (observed_at, observed_at, message_id, recipient),
            )
            _record_delivery_event(
                connection,
                message_id=message_id,
                recipient=recipient,
                attempt=int(row["attempts"]),
                state="observed",
            )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {
        "status": "acked",
        "already_acked": already,
        "message": _message(connection, row, include_text=False),
        "recipient": recipient,
        "state": str(row["state"]) if already else "observed",
    }


def release(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
    message_id: int,
    receipt: str,
) -> dict[str, Any]:
    room = validate_name(room, "room")
    recipient = validate_name(recipient, "recipient")
    connection.execute("BEGIN IMMEDIATE")
    try:
        room, _ = _require_member(connection, room, recipient, active=False)
        cursor = connection.execute(
            """
            UPDATE deliveries
            SET claim_token=NULL, claimed_at=NULL, claim_until=NULL,
                state='queued', injected_at=NULL, adapter=NULL, host_ref=NULL
            WHERE message_id=? AND recipient=? AND acked_at IS NULL
              AND claim_token=?
              AND EXISTS (
                SELECT 1 FROM messages
                WHERE messages.id=deliveries.message_id AND messages.room=?
              )
            """,
            (message_id, recipient, receipt, room),
        )
        if cursor.rowcount != 1:
            raise ChatError("unacknowledged delivery does not exist for this endpoint")
        row = connection.execute(
            "SELECT attempts FROM deliveries WHERE message_id=? AND recipient=?",
            (message_id, recipient),
        ).fetchone()
        _record_delivery_event(
            connection,
            message_id=message_id,
            recipient=recipient,
            attempt=int(row["attempts"]),
            state="queued",
        )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {
        "status": "released",
        "room": room,
        "message_id": message_id,
        "recipient": recipient,
    }


def status(connection: sqlite3.Connection, *, room: str) -> dict[str, Any]:
    room = _require_room(connection, room)
    message_count = int(
        connection.execute("SELECT COUNT(*) FROM messages WHERE room=?", (room,)).fetchone()[0]
    )
    member_count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM memberships
            WHERE room=? AND left_at IS NULL
            """,
            (room,),
        ).fetchone()[0]
    )
    unread = {
        str(row["recipient"]): int(row["count"])
        for row in connection.execute(
            """
            SELECT d.recipient, COUNT(*) AS count
            FROM deliveries AS d
            JOIN messages AS m ON m.id=d.message_id
            WHERE m.room=? AND d.acked_at IS NULL
            GROUP BY d.recipient
            ORDER BY d.recipient
            """,
            (room,),
        )
    }
    now = stamp()
    claimed = {
        str(row["recipient"]): int(row["count"])
        for row in connection.execute(
            """
            SELECT d.recipient, COUNT(*) AS count
            FROM deliveries AS d
            JOIN messages AS m ON m.id=d.message_id
            WHERE m.room=? AND d.acked_at IS NULL AND d.claim_until>?
            GROUP BY d.recipient
            ORDER BY d.recipient
            """,
            (room, now),
        )
    }
    visible = {
        endpoint: count - claimed.get(endpoint, 0)
        for endpoint, count in unread.items()
        if count - claimed.get(endpoint, 0) > 0
    }
    delivery_states: dict[str, dict[str, int]] = {}
    for row in connection.execute(
        """
        SELECT d.recipient, d.state, COUNT(*) AS count
        FROM deliveries AS d
        JOIN messages AS m ON m.id=d.message_id
        WHERE m.room=?
        GROUP BY d.recipient, d.state
        ORDER BY d.recipient, d.state
        """,
        (room,),
    ):
        delivery_states.setdefault(str(row["recipient"]), {})[str(row["state"])] = int(row["count"])
    return {
        "status": "ok",
        "room": room,
        "members": member_count,
        "messages": message_count,
        "unread": unread,
        "claimed": claimed,
        "visible": visible,
        "delivery_states": delivery_states,
    }


def delivery_detail(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
    message_id: int,
) -> dict[str, Any]:
    room, _ = _require_member(connection, room, recipient, active=False)
    recipient = validate_name(recipient, "recipient")
    row = connection.execute(
        """
        SELECT m.*, d.attempts, d.state, d.claimed_at, d.claim_until,
               d.acked_at, d.injected_at, d.observed_at, d.acted_at,
               d.replied_at, d.adapter, d.host_ref
        FROM messages AS m
        JOIN deliveries AS d ON d.message_id=m.id
        WHERE m.id=? AND m.room=? AND d.recipient=?
        """,
        (message_id, room, recipient),
    ).fetchone()
    if row is None:
        raise ChatError("message was not delivered to this endpoint in this room")
    events = [
        dict(event)
        for event in connection.execute(
            """
            SELECT attempt, state, adapter, host_ref, created_at
            FROM delivery_events
            WHERE message_id=? AND recipient=?
            ORDER BY id
            """,
            (message_id, recipient),
        )
    ]
    return {
        "status": "ok",
        "message": _message(connection, row),
        "delivery": {
            "recipient": recipient,
            "attempts": int(row["attempts"]),
            "state": row["state"],
            "claimed_at": row["claimed_at"],
            "visible_again_at": row["claim_until"],
            "acked_at": row["acked_at"],
            "injected_at": row["injected_at"],
            "observed_at": row["observed_at"],
            "acted_at": row["acted_at"],
            "replied_at": row["replied_at"],
            "adapter": row["adapter"],
            "host_ref": row["host_ref"],
            "events": events,
        },
    }


def history(
    connection: sqlite3.Connection,
    *,
    room: str,
    limit: int,
    viewer: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    room = _require_room(connection, room)
    if not 1 <= limit <= 500:
        raise ChatError("history limit must be between 1 and 500")
    if conversation_id is not None:
        conversation_id = validate_name(conversation_id, "conversation id")
    conversation_filter = " AND conversation_id=?" if conversation_id else ""
    conversation_values: tuple[Any, ...] = (conversation_id,) if conversation_id else ()
    if viewer is None:
        rows = connection.execute(
            f"""SELECT * FROM messages
            WHERE room=?{conversation_filter}
            ORDER BY id DESC LIMIT ?""",
            (room, *conversation_values, limit),
        ).fetchall()
    else:
        _require_member(connection, room, viewer, active=False)
        viewer = validate_name(viewer, "viewer")
        viewer_filter = " AND m.conversation_id=?" if conversation_id else ""
        rows = connection.execute(
            f"""
            SELECT * FROM messages AS m
            WHERE m.room=?{viewer_filter} AND (
                m.sender=? OR EXISTS (
                    SELECT 1 FROM deliveries AS d
                    WHERE d.message_id=m.id AND d.recipient=?
                )
            )
            ORDER BY m.id DESC LIMIT ?
            """,
            (room, *conversation_values, viewer, viewer, limit),
        ).fetchall()
    return {
        "status": "ok",
        "room": room,
        "conversation_id": conversation_id,
        "messages": [_message(connection, row) for row in reversed(rows)],
    }


def doctor(connection: sqlite3.Connection, *, path: Path) -> dict[str, Any]:
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    return {
        "status": "ok" if integrity == "ok" else "error",
        "integrity": integrity,
        "journal_mode": journal,
        "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
        "bridge": bridge_info(connection, path),
    }
