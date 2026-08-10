#!/usr/bin/env python3
"""A tiny durable chat between local agent sessions."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MAX_TEXT_BYTES = 32_768
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
CHECK_IN_TEXT = (
    "CHECK-IN: I have no unread messages. Are you waiting on me, should I "
    "continue, or is the run complete? Please acknowledge and answer."
)


class ChatError(RuntimeError):
    pass


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _print(value: Any) -> None:
    print(_json(value))


def _name(value: str, label: str) -> str:
    if not NAME.fullmatch(value):
        raise ChatError(f"{label} must be a 1-64 character agent/room name")
    return value


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    path = path.resolve()
    existed = path.exists()
    if not existed and not create:
        raise ChatError(
            f"chat database does not exist: {path}; initialize that exact path first"
        )
    if not existed:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    has_messages = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if existed and has_messages is None and not create:
        connection.close()
        raise ChatError(f"not an initialized Agent Chat database: {path}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            text TEXT NOT NULL,
            reply_to INTEGER REFERENCES messages(id),
            dedupe_key TEXT,
            created_at TEXT NOT NULL,
            acked_at TEXT
        );

        CREATE INDEX IF NOT EXISTS messages_inbox
            ON messages(room, recipient, acked_at, id);

        CREATE UNIQUE INDEX IF NOT EXISTS messages_dedupe
            ON messages(room, sender, dedupe_key)
            WHERE dedupe_key IS NOT NULL;
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES ('bridge_id', ?)",
        (uuid.uuid4().hex,),
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES ('created_at', ?)",
        (_stamp(),),
    )
    return connection


def bridge_info(connection: sqlite3.Connection, path: Path) -> dict[str, str]:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='bridge_id'"
    ).fetchone()
    if row is None:
        raise ChatError("chat database has no bridge identity")
    return {"id": row["value"], "database": str(path.resolve())}


def _emit(
    connection: sqlite3.Connection, path: Path, value: dict[str, Any]
) -> None:
    result = dict(value)
    result["bridge"] = bridge_info(connection, path)
    _print(result)


def _message(row: sqlite3.Row, *, include_text: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row["id"],
        "room": row["room"],
        "from": row["sender"],
        "to": row["recipient"],
        "reply_to": row["reply_to"],
        "created_at": row["created_at"],
        "acked": row["acked_at"] is not None,
    }
    if include_text:
        result["text"] = row["text"]
    return result


def send(
    connection: sqlite3.Connection,
    *,
    room: str,
    sender: str,
    recipient: str,
    text: str,
    reply_to: int | None,
    dedupe_key: str | None,
) -> dict[str, Any]:
    room = _name(room, "room")
    sender = _name(sender, "sender")
    recipient = _name(recipient, "recipient")
    if sender == recipient:
        raise ChatError("sender and recipient must differ")
    if not text.strip():
        raise ChatError("message text cannot be empty")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ChatError(f"message text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
    if dedupe_key is not None:
        dedupe_key = _name(dedupe_key, "dedupe key")

    connection.execute("BEGIN IMMEDIATE")
    try:
        if dedupe_key is not None:
            existing = connection.execute(
                "SELECT * FROM messages WHERE room=? AND sender=? AND dedupe_key=?",
                (room, sender, dedupe_key),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return {"status": "sent", "duplicate": True, "message": _message(existing)}
        if reply_to is not None:
            parent = connection.execute(
                "SELECT id FROM messages WHERE id=? AND room=?",
                (reply_to, room),
            ).fetchone()
            if parent is None:
                raise ChatError("reply_to does not exist in this room")
        cursor = connection.execute(
            """
            INSERT INTO messages(
                room, sender, recipient, text, reply_to, dedupe_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (room, sender, recipient, text, reply_to, dedupe_key, _stamp()),
        )
        row = connection.execute(
            "SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {"status": "sent", "duplicate": False, "message": _message(row)}


def receive_one(
    connection: sqlite3.Connection, *, room: str, recipient: str
) -> dict[str, Any] | None:
    room = _name(room, "room")
    recipient = _name(recipient, "recipient")
    row = connection.execute(
        """
        SELECT * FROM messages
        WHERE room=? AND recipient=? AND acked_at IS NULL
        ORDER BY id
        LIMIT 1
        """,
        (room, recipient),
    ).fetchone()
    return None if row is None else _message(row)


def _pending_seconds(created_at: str) -> int:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return max(0, int((datetime.now(UTC) - created).total_seconds()))


def ask_peer(
    connection: sqlite3.Connection,
    *,
    room: str,
    sender: str,
    recipient: str,
    text: str = CHECK_IN_TEXT,
) -> dict[str, Any]:
    room = _name(room, "room")
    sender = _name(sender, "sender")
    recipient = _name(recipient, "recipient")
    if sender == recipient:
        raise ChatError("sender and recipient must differ")

    pending = connection.execute(
        """
        SELECT * FROM messages
        WHERE room=? AND sender=? AND recipient=? AND acked_at IS NULL
        ORDER BY id
        LIMIT 1
        """,
        (room, sender, recipient),
    ).fetchone()
    if pending is not None:
        return {
            "status": "peer_pending",
            "reason": "outgoing_message_unacknowledged",
            "delivery_state": "queued_unacknowledged",
            "peer_state": "unknown_may_be_busy",
            "pending_seconds": _pending_seconds(pending["created_at"]),
            "pending_message": _message(pending, include_text=False),
        }

    latest = connection.execute(
        """
        SELECT * FROM messages
        WHERE room=? AND sender=? AND recipient=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (room, sender, recipient),
    ).fetchone()
    if (
        latest is not None
        and latest["acked_at"] is not None
        and isinstance(latest["dedupe_key"], str)
        and latest["dedupe_key"].startswith("checkin-")
    ):
        response = connection.execute(
            """
            SELECT id FROM messages
            WHERE room=? AND sender=? AND recipient=? AND id>?
            ORDER BY id
            LIMIT 1
            """,
            (room, recipient, sender, latest["id"]),
        ).fetchone()
        if response is None:
            return {
                "status": "peer_reply_pending",
                "reason": "check_in_acknowledged_without_answer",
                "delivery_state": "acknowledged_without_reply",
                "peer_state": "unknown_may_be_busy",
                "pending_seconds": _pending_seconds(latest["created_at"]),
                "check_in": _message(latest, include_text=False),
            }

    result = send(
        connection,
        room=room,
        sender=sender,
        recipient=recipient,
        text=text,
        reply_to=None,
        dedupe_key=f"checkin-{uuid.uuid4().hex}",
    )
    return {"status": "check_in_sent", "message": result["message"]}


def wait_for_message(
    connection: sqlite3.Connection,
    *,
    room: str,
    recipient: str,
    wait: float,
    poll: float,
) -> dict[str, Any] | None:
    if not 0 <= wait <= 3600:
        raise ChatError("wait must be between 0 and 3600 seconds")
    if not 0.1 <= poll <= 5:
        raise ChatError("poll must be between 0.1 and 5 seconds")
    deadline = time.monotonic() + wait
    while True:
        message = receive_one(connection, room=room, recipient=recipient)
        if message is not None:
            return message
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))


def acknowledge(
    connection: sqlite3.Connection, *, room: str, recipient: str, message_id: int
) -> dict[str, Any]:
    room = _name(room, "room")
    recipient = _name(recipient, "recipient")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT * FROM messages WHERE id=? AND room=?",
            (message_id, room),
        ).fetchone()
        if row is None:
            raise ChatError("message does not exist in this room")
        if row["recipient"] != recipient:
            raise ChatError("only the recipient can acknowledge this message")
        already = row["acked_at"] is not None
        if not already:
            connection.execute(
                "UPDATE messages SET acked_at=? WHERE id=?",
                (_stamp(), message_id),
            )
        current = connection.execute(
            "SELECT * FROM messages WHERE id=?", (message_id,)
        ).fetchone()
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return {"status": "acked", "already_acked": already, "message": _message(current)}


def status(connection: sqlite3.Connection, *, room: str) -> dict[str, Any]:
    room = _name(room, "room")
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM messages WHERE room=?", (room,)
        ).fetchone()[0]
    )
    unread = {
        row["recipient"]: row["count"]
        for row in connection.execute(
            """
            SELECT recipient, COUNT(*) AS count
            FROM messages
            WHERE room=? AND acked_at IS NULL
            GROUP BY recipient
            """,
            (room,),
        )
    }
    return {"status": "ok", "room": room, "messages": total, "unread": unread}


def history(
    connection: sqlite3.Connection, *, room: str, limit: int
) -> dict[str, Any]:
    room = _name(room, "room")
    if not 1 <= limit <= 100:
        raise ChatError("history limit must be between 1 and 100")
    rows = connection.execute(
        "SELECT * FROM messages WHERE room=? ORDER BY id DESC LIMIT ?",
        (room, limit),
    ).fetchall()
    return {
        "status": "ok",
        "room": room,
        "messages": [_message(row) for row in reversed(rows)],
    }


def _read_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if args.file is not None:
        try:
            return args.file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ChatError(f"cannot read message file: {exc}") from exc
    return sys.stdin.read()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "--db",
        type=Path,
        required=True,
        help="explicit Agent Chat SQLite path; there is no silent default",
    )
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize the chat database")
    init.add_argument("--room", required=True)

    send_parser = commands.add_parser("send", help="send free-form text")
    send_parser.add_argument("--room", required=True)
    send_parser.add_argument("--from", dest="sender", required=True)
    send_parser.add_argument("--to", dest="recipient", required=True)
    send_parser.add_argument("--reply-to", type=int)
    send_parser.add_argument("--key", dest="dedupe_key")
    text_source = send_parser.add_mutually_exclusive_group(required=True)
    text_source.add_argument("--text")
    text_source.add_argument("--file", type=Path)
    text_source.add_argument("--stdin", action="store_true")

    receive = commands.add_parser("receive", help="wait for the oldest unread message")
    receive.add_argument("--room", required=True)
    receive.add_argument("--as", dest="recipient", required=True)
    receive.add_argument("--wait", type=float, default=0.0)
    receive.add_argument("--poll", type=float, default=0.5)

    sync = commands.add_parser(
        "sync",
        help="wait once, then ask the peer instead of silently polling forever",
    )
    sync.add_argument("--room", required=True)
    sync.add_argument("--as", dest="sender", required=True)
    sync.add_argument("--with", dest="recipient", required=True)
    sync.add_argument("--wait", type=float, default=30.0)
    sync.add_argument("--poll", type=float, default=0.5)
    sync.add_argument("--ask", default=CHECK_IN_TEXT)

    ack = commands.add_parser("ack", help="acknowledge one received message")
    ack.add_argument("--room", required=True)
    ack.add_argument("--as", dest="recipient", required=True)
    ack.add_argument("--id", dest="message_id", type=int, required=True)

    status_parser = commands.add_parser("status", help="show message counts")
    status_parser.add_argument("--room", required=True)

    history_parser = commands.add_parser("history", help="show recent conversation")
    history_parser.add_argument("--room", required=True)
    history_parser.add_argument("--limit", type=int, default=20)
    return root


def run(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    db_path = args.db.resolve()
    connection = connect(db_path, create=args.command == "init")
    try:
        if args.command == "init":
            _emit(connection, db_path, status(connection, room=args.room))
            return 0
        if args.command == "send":
            _emit(
                connection,
                db_path,
                send(
                    connection,
                    room=args.room,
                    sender=args.sender,
                    recipient=args.recipient,
                    text=_read_text(args),
                    reply_to=args.reply_to,
                    dedupe_key=args.dedupe_key,
                )
            )
            return 0
        if args.command == "receive":
            message = wait_for_message(
                connection,
                room=args.room,
                recipient=args.recipient,
                wait=args.wait,
                poll=args.poll,
            )
            if message is not None:
                _emit(
                    connection,
                    db_path,
                    {"status": "message", "message": message},
                )
                return 0
            _emit(
                connection,
                db_path,
                {
                    "status": "empty",
                    "room": args.room,
                    "recipient": args.recipient,
                    "waited_seconds": args.wait,
                },
            )
            return 3
        if args.command == "sync":
            message = wait_for_message(
                connection,
                room=args.room,
                recipient=args.sender,
                wait=args.wait,
                poll=args.poll,
            )
            result = (
                {"status": "message", "message": message}
                if message is not None
                else ask_peer(
                    connection,
                    room=args.room,
                    sender=args.sender,
                    recipient=args.recipient,
                    text=args.ask,
                )
            )
            _emit(connection, db_path, result)
            return 4 if result["status"].startswith("peer_") else 0
        if args.command == "ack":
            _emit(
                connection,
                db_path,
                acknowledge(
                    connection,
                    room=args.room,
                    recipient=args.recipient,
                    message_id=args.message_id,
                )
            )
            return 0
        if args.command == "status":
            _emit(connection, db_path, status(connection, room=args.room))
            return 0
        if args.command == "history":
            _emit(
                connection,
                db_path,
                history(connection, room=args.room, limit=args.limit),
            )
            return 0
        raise ChatError("unknown command")
    finally:
        connection.close()


def main() -> None:
    try:
        raise SystemExit(run())
    except ChatError as exc:
        print(_json({"status": "error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
