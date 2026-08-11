"""Command-line interface for Cross-Agent Bridge."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import core


class JsonArgumentParser(argparse.ArgumentParser):
    """Route parser failures through the CLI's JSON error contract."""

    def error(self, message: str) -> None:
        raise core.ChatError(f"argument error: {message}")


def encode(value: Any) -> str:
    # CLI output is a machine protocol. ASCII JSON escapes preserve Unicode and
    # remain writable when Windows redirects stdout through a legacy code page.
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def emit(
    connection: sqlite3.Connection,
    path: Path,
    value: dict[str, Any],
) -> None:
    result = dict(value)
    result["bridge"] = core.bridge_info(connection, path)
    print(encode(result))


def read_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if args.file is not None:
        try:
            if args.file.stat().st_size > core.MAX_TEXT_BYTES:
                raise core.ChatError(f"message file exceeds {core.MAX_TEXT_BYTES} bytes")
            return args.file.read_text(encoding="utf-8")
        except OSError as exc:
            raise core.ChatError(f"cannot read message file: {exc}") from exc
        except UnicodeError as exc:
            raise core.ChatError("message file is not valid UTF-8") from exc
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(core.MAX_TEXT_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > core.MAX_TEXT_BYTES:
        raise core.ChatError(f"stdin exceeds {core.MAX_TEXT_BYTES} UTF-8 bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise core.ChatError("stdin is not valid UTF-8") from exc


def parser() -> argparse.ArgumentParser:
    configured_db = os.environ.get("AGENT_CHAT_DB")
    configured_room = os.environ.get("AGENT_CHAT_ROOM")
    configured_endpoint = os.environ.get("AGENT_CHAT_ENDPOINT")
    configured_system = os.environ.get("AGENT_CHAT_SYSTEM")
    root = JsonArgumentParser(
        prog="agent-chat",
        description="Durable local group chat for AI agent sessions.",
    )
    root.add_argument(
        "--db",
        type=Path,
        default=Path(configured_db) if configured_db else None,
        help="explicit SQLite path, or AGENT_CHAT_DB; only init may create it",
    )
    root.add_argument(
        "--expect-bridge",
        default=os.environ.get("AGENT_CHAT_BRIDGE"),
        help="expected bridge ID, or AGENT_CHAT_BRIDGE",
    )
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize a database and room")
    init.add_argument("--room", default=configured_room)

    create_room = commands.add_parser("create-room", help="add another room")
    create_room.add_argument("--room", default=configured_room)

    join = commands.add_parser("join", help="register one live agent session")
    join.add_argument("--room", default=configured_room)
    join.add_argument("--endpoint", default=configured_endpoint)
    join.add_argument("--system", default=configured_system)
    join.add_argument("--label")

    leave = commands.add_parser("leave", help="stop future room deliveries")
    leave.add_argument("--room", default=configured_room)
    leave.add_argument("--as", dest="endpoint", default=configured_endpoint)

    member_list = commands.add_parser("members", help="list active room endpoints")
    member_list.add_argument("--room", default=configured_room)

    send = commands.add_parser("send", help="send free-form text")
    send.add_argument("--room", default=configured_room)
    send.add_argument("--from", dest="sender", default=configured_endpoint)
    audience = send.add_mutually_exclusive_group(required=True)
    audience.add_argument(
        "--to",
        dest="recipients",
        action="append",
        help="direct recipient endpoint; repeat for several endpoints",
    )
    audience.add_argument(
        "--broadcast",
        action="store_true",
        help="snapshot all other active room members",
    )
    send.add_argument("--reply-to", type=int)
    send.add_argument(
        "--kind",
        choices=core.MESSAGE_KINDS,
        help="message for a new conversation; progress or reply with --reply-to",
    )
    send.add_argument("--key", dest="dedupe_key")
    source = send.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--file", type=Path)
    source.add_argument("--stdin", action="store_true")

    receive = commands.add_parser("receive", help="long-poll for one unacknowledged delivery")
    receive.add_argument("--room", default=configured_room)
    receive.add_argument("--as", dest="recipient", default=configured_endpoint)
    receive.add_argument("--wait", type=float, default=0.0)
    receive.add_argument("--poll", type=float, default=0.25)
    receive.add_argument("--visibility", type=float, default=300.0)

    ack = commands.add_parser("ack", help="acknowledge a claimed delivery")
    ack.add_argument("--room", default=configured_room)
    ack.add_argument("--as", dest="recipient", default=configured_endpoint)
    ack.add_argument("--id", dest="message_id", type=int, required=True)
    ack.add_argument("--receipt", required=True)

    release = commands.add_parser("release", help="release a claim without acknowledging it")
    release.add_argument("--room", default=configured_room)
    release.add_argument("--as", dest="recipient", default=configured_endpoint)
    release.add_argument("--id", dest="message_id", type=int, required=True)
    release.add_argument("--receipt", required=True)

    renew = commands.add_parser(
        "renew", help="extend an active delivery claim while an adapter is alive"
    )
    renew.add_argument("--room", default=configured_room)
    renew.add_argument("--as", dest="recipient", default=configured_endpoint)
    renew.add_argument("--id", dest="message_id", type=int, required=True)
    renew.add_argument("--receipt", required=True)
    renew.add_argument("--visibility", type=float, default=300.0)

    mark = commands.add_parser("mark", help="record injected, observed, or acted delivery progress")
    mark.add_argument("--room", default=configured_room)
    mark.add_argument("--as", dest="recipient", default=configured_endpoint)
    mark.add_argument("--id", dest="message_id", type=int, required=True)
    mark.add_argument("--receipt", required=True)
    mark.add_argument(
        "--state",
        choices=("injected", "observed", "acted"),
        required=True,
    )
    mark.add_argument("--adapter")
    mark.add_argument("--host-ref")

    status = commands.add_parser("status", help="show room delivery counts")
    status.add_argument("--room", default=configured_room)

    history = commands.add_parser("history", help="show recent visible messages")
    history.add_argument("--room", default=configured_room)
    history.add_argument("--as", dest="viewer")
    history.add_argument(
        "--conversation",
        dest="conversation_id",
        help="limit history to one conversation ID",
    )
    history.add_argument("--limit", type=int, default=50)

    delivery = commands.add_parser("delivery", help="inspect one endpoint's delivery timeline")
    delivery.add_argument("--room", default=configured_room)
    delivery.add_argument("--as", dest="recipient", default=configured_endpoint)
    delivery.add_argument("--id", dest="message_id", type=int, required=True)

    adapter_acquire = commands.add_parser(
        "adapter-acquire", help="claim exclusive ownership of one session endpoint"
    )
    adapter_acquire.add_argument("--room", default=configured_room)
    adapter_acquire.add_argument("--as", dest="endpoint", default=configured_endpoint)
    adapter_acquire.add_argument("--owner", required=True)
    adapter_acquire.add_argument("--adapter", required=True)
    adapter_acquire.add_argument("--host-ref")
    adapter_acquire.add_argument("--ttl", type=float, default=30.0)

    adapter_renew = commands.add_parser(
        "adapter-renew", help="renew one live-session adapter lease"
    )
    adapter_renew.add_argument("--room", default=configured_room)
    adapter_renew.add_argument("--as", dest="endpoint", default=configured_endpoint)
    adapter_renew.add_argument("--owner", required=True)
    adapter_renew.add_argument("--ttl", type=float, default=30.0)

    adapter_release = commands.add_parser(
        "adapter-release", help="release endpoint ownership and recover incomplete work"
    )
    adapter_release.add_argument("--room", default=configured_room)
    adapter_release.add_argument("--as", dest="endpoint", default=configured_endpoint)
    adapter_release.add_argument("--owner", required=True)

    commands.add_parser("doctor", help="check database identity and integrity")
    return root


def run(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.db is None:
        raise core.ChatError("database is required: use --db or AGENT_CHAT_DB")
    required_by_command = {
        "init": (("room", "--room", "AGENT_CHAT_ROOM"),),
        "create-room": (("room", "--room", "AGENT_CHAT_ROOM"),),
        "join": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("endpoint", "--endpoint", "AGENT_CHAT_ENDPOINT"),
            ("system", "--system", "AGENT_CHAT_SYSTEM"),
        ),
        "leave": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("endpoint", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "members": (("room", "--room", "AGENT_CHAT_ROOM"),),
        "send": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("sender", "--from", "AGENT_CHAT_ENDPOINT"),
        ),
        "receive": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("recipient", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "ack": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("recipient", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "release": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("recipient", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "renew": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("recipient", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "mark": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("recipient", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "status": (("room", "--room", "AGENT_CHAT_ROOM"),),
        "history": (("room", "--room", "AGENT_CHAT_ROOM"),),
        "delivery": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("recipient", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "adapter-acquire": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("endpoint", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "adapter-renew": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("endpoint", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "adapter-release": (
            ("room", "--room", "AGENT_CHAT_ROOM"),
            ("endpoint", "--as", "AGENT_CHAT_ENDPOINT"),
        ),
        "doctor": (),
    }
    for name, option, environment in required_by_command[args.command]:
        if getattr(args, name) is None:
            raise core.ChatError(f"{option} is required, or set {environment}")
    path = args.db.resolve()
    if args.command == "init" and not path.exists() and args.expect_bridge is not None:
        raise core.ChatError(
            "cannot initialize a new database while an expected bridge ID is pinned; "
            "unset AGENT_CHAT_BRIDGE or omit --expect-bridge for init"
        )
    connection = core.connect(path, create=args.command == "init")
    try:
        core.require_bridge(connection, args.expect_bridge)
        if args.command == "init":
            emit(connection, path, core.create_room(connection, args.room))
        elif args.command == "create-room":
            emit(connection, path, core.create_room(connection, args.room))
        elif args.command == "join":
            emit(
                connection,
                path,
                core.join(
                    connection,
                    room=args.room,
                    endpoint=args.endpoint,
                    system=args.system,
                    label=args.label,
                ),
            )
        elif args.command == "leave":
            emit(
                connection,
                path,
                core.leave(connection, room=args.room, endpoint=args.endpoint),
            )
        elif args.command == "members":
            emit(connection, path, core.members(connection, room=args.room))
        elif args.command == "send":
            emit(
                connection,
                path,
                core.send(
                    connection,
                    room=args.room,
                    sender=args.sender,
                    text=read_text(args),
                    recipients=args.recipients or (),
                    broadcast=args.broadcast,
                    reply_to=args.reply_to,
                    dedupe_key=args.dedupe_key,
                    kind=args.kind,
                ),
            )
        elif args.command == "receive":
            delivery = core.wait_for_message(
                connection,
                room=args.room,
                recipient=args.recipient,
                wait=args.wait,
                poll=args.poll,
                visibility=args.visibility,
            )
            if delivery is None:
                emit(
                    connection,
                    path,
                    {
                        "status": "empty",
                        "room": args.room,
                        "endpoint": args.recipient,
                        "waited_seconds": args.wait,
                    },
                )
                return 3
            emit(connection, path, {"status": "message", **delivery})
        elif args.command == "ack":
            emit(
                connection,
                path,
                core.acknowledge(
                    connection,
                    room=args.room,
                    recipient=args.recipient,
                    message_id=args.message_id,
                    receipt=args.receipt,
                ),
            )
        elif args.command == "release":
            emit(
                connection,
                path,
                core.release(
                    connection,
                    room=args.room,
                    recipient=args.recipient,
                    message_id=args.message_id,
                    receipt=args.receipt,
                ),
            )
        elif args.command == "renew":
            emit(
                connection,
                path,
                core.renew_claim(
                    connection,
                    room=args.room,
                    recipient=args.recipient,
                    message_id=args.message_id,
                    receipt=args.receipt,
                    visibility=args.visibility,
                ),
            )
        elif args.command == "mark":
            emit(
                connection,
                path,
                core.mark_delivery(
                    connection,
                    room=args.room,
                    recipient=args.recipient,
                    message_id=args.message_id,
                    receipt=args.receipt,
                    state=args.state,
                    adapter=args.adapter,
                    host_ref=args.host_ref,
                ),
            )
        elif args.command == "status":
            emit(connection, path, core.status(connection, room=args.room))
        elif args.command == "history":
            emit(
                connection,
                path,
                core.history(
                    connection,
                    room=args.room,
                    limit=args.limit,
                    viewer=args.viewer,
                    conversation_id=args.conversation_id,
                ),
            )
        elif args.command == "delivery":
            emit(
                connection,
                path,
                core.delivery_detail(
                    connection,
                    room=args.room,
                    recipient=args.recipient,
                    message_id=args.message_id,
                ),
            )
        elif args.command == "adapter-acquire":
            emit(
                connection,
                path,
                core.acquire_adapter_lease(
                    connection,
                    room=args.room,
                    endpoint=args.endpoint,
                    owner_token=args.owner,
                    adapter=args.adapter,
                    host_ref=args.host_ref,
                    ttl=args.ttl,
                ),
            )
        elif args.command == "adapter-renew":
            emit(
                connection,
                path,
                core.renew_adapter_lease(
                    connection,
                    room=args.room,
                    endpoint=args.endpoint,
                    owner_token=args.owner,
                    ttl=args.ttl,
                ),
            )
        elif args.command == "adapter-release":
            emit(
                connection,
                path,
                core.release_adapter_lease(
                    connection,
                    room=args.room,
                    endpoint=args.endpoint,
                    owner_token=args.owner,
                ),
            )
        elif args.command == "doctor":
            result = core.doctor(connection, path=path)
            emit(connection, path, result)
            return 0 if result["status"] == "ok" else 5
        else:
            raise core.ChatError("unknown command")
        return 0
    finally:
        connection.close()


def main() -> None:
    try:
        raise SystemExit(run())
    except (core.ChatError, sqlite3.Error) as exc:
        print(encode({"status": "error", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
