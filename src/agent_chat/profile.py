"""Small local connection profiles that prevent bridge path and identity drift."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import core

PROFILE_SCHEMA = 1
MAX_PROFILE_BYTES = 64 * 1024
PROFILE_FIELDS = {"schema", "database", "bridge_id", "room"}


def load(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        if resolved.stat().st_size > MAX_PROFILE_BYTES:
            raise core.ChatError(f"bridge profile exceeds {MAX_PROFILE_BYTES} bytes")
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise core.ChatError(f"bridge profile does not exist: {resolved}") from exc
    except OSError as exc:
        raise core.ChatError(f"cannot read bridge profile: {exc}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise core.ChatError(f"bridge profile is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != PROFILE_FIELDS:
        raise core.ChatError(
            "bridge profile must contain exactly schema, database, bridge_id, and room"
        )
    if value["schema"] != PROFILE_SCHEMA:
        raise core.ChatError(f"unsupported bridge profile schema: {value['schema']!r}")
    database_value = value["database"]
    if not isinstance(database_value, str) or not database_value:
        raise core.ChatError("bridge profile database must be a non-empty absolute path")
    database = Path(database_value)
    if not database.is_absolute():
        raise core.ChatError("bridge profile database must be an absolute path")
    bridge_id = value["bridge_id"]
    if not isinstance(bridge_id, str) or not bridge_id or len(bridge_id) > 128:
        raise core.ChatError("bridge profile bridge_id must be a non-empty string")
    room = value["room"]
    if not isinstance(room, str):
        raise core.ChatError("bridge profile room must be a string")
    core.validate_name(room, "room")
    return {
        "schema": PROFILE_SCHEMA,
        "database": database.resolve(),
        "bridge_id": bridge_id,
        "room": room,
        "profile": resolved,
    }


def apply(args: Any) -> Any:
    """Apply one authoritative connection identity and reject mixed configurations."""
    profile_path = getattr(args, "profile", None)
    if profile_path is None:
        return args
    value = load(Path(profile_path))
    current_database = getattr(args, "db", None)
    if current_database is not None and Path(current_database).resolve() != value["database"]:
        raise core.ChatError("--db or AGENT_CHAT_DB conflicts with the bridge profile")
    args.db = value["database"]
    current_bridge = getattr(args, "expect_bridge", None)
    if current_bridge is not None and current_bridge != value["bridge_id"]:
        raise core.ChatError(
            "--expect-bridge or AGENT_CHAT_BRIDGE conflicts with the bridge profile"
        )
    args.expect_bridge = value["bridge_id"]
    if hasattr(args, "room"):
        current_room = args.room
        if current_room is not None and current_room != value["room"]:
            raise core.ChatError("--room or AGENT_CHAT_ROOM conflicts with the bridge profile")
        args.room = value["room"]
    return args


def write(
    path: Path,
    *,
    database: Path,
    bridge_id: str,
    room: str,
) -> Path:
    """Atomically write one value-free local connection profile."""
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "schema": PROFILE_SCHEMA,
            "database": str(database.resolve()),
            "bridge_id": bridge_id,
            "room": core.validate_name(room, "room"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=resolved.parent,
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, resolved)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return resolved
