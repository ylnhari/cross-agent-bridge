"""Launch one interactive Claude Code session with a live bridge Channel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from . import core
from . import profile as bridge_profile

MINIMUM_CLAUDE_VERSION = (2, 1, 80)
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\s+\(Claude Code\))?")


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep launcher failures machine-readable like the core CLI."""

    def error(self, message: str) -> None:
        raise core.ChatError(f"argument error: {message}")


def parser() -> argparse.ArgumentParser:
    result = JsonArgumentParser(
        prog="agent-chat-claude",
        description="Start one interactive Claude Code session with a live bridge Channel.",
    )
    result.add_argument("--db", type=Path, default=os.environ.get("AGENT_CHAT_DB"))
    result.add_argument("--expect-bridge", default=os.environ.get("AGENT_CHAT_BRIDGE"))
    result.add_argument(
        "--profile",
        type=Path,
        default=Path(os.environ["AGENT_CHAT_PROFILE"])
        if os.environ.get("AGENT_CHAT_PROFILE")
        else None,
    )
    result.add_argument("--room", default=os.environ.get("AGENT_CHAT_ROOM"))
    result.add_argument("--endpoint", default=os.environ.get("AGENT_CHAT_ENDPOINT"))
    result.add_argument("--cwd", type=Path, default=Path.cwd())
    result.add_argument("--name", help="Claude session display name")
    continuation = result.add_mutually_exclusive_group()
    continuation.add_argument("--resume", help="resume this Claude session ID")
    continuation.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="continue the most recent Claude session in --cwd",
    )
    continuation.add_argument("--session-id", help="UUID for a new Claude session")
    result.add_argument("--model")
    result.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    result.add_argument(
        "--permission-mode",
        choices=("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"),
    )
    result.add_argument("--prompt", help="optional first user prompt")
    result.add_argument(
        "--strict-mcp-config",
        action="store_true",
        help="disable every MCP server except this bridge for the launched session",
    )
    result.add_argument("--no-chrome", action="store_true")
    result.add_argument(
        "--check",
        action="store_true",
        help="validate the bridge and host prerequisites without starting Claude",
    )
    result.add_argument("--claude-program", default=os.environ.get("AGENT_CHAT_CLAUDE", "claude"))
    return result


def _required(value: Any, option: str, environment: str) -> Any:
    if value is None or value == "":
        raise core.ChatError(f"{option} is required, or set {environment}")
    return value


def _resolve_program(value: str, label: str) -> str:
    candidate = Path(value)
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(value)
    if found:
        return str(Path(found).resolve())
    raise core.ChatError(f"{label} executable not found: {value}")


def _program_version(program: str, label: str) -> tuple[int, int, int]:
    try:
        result = subprocess.run(
            [program, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except OSError as exc:
        raise core.ChatError(f"could not run {label}: {exc}") from exc
    output = "\n".join((result.stdout, result.stderr))
    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    match = VERSION.fullmatch(stdout_lines[0]) if stdout_lines else None
    if result.returncode != 0 or match is None:
        detail = output.strip() or f"exit {result.returncode}"
        raise core.ChatError(f"could not identify {label} version: {detail}")
    return tuple(int(part) for part in match.groups())


def _bridge_context(args: argparse.Namespace) -> dict[str, str]:
    database = Path(_required(args.db, "--db", "AGENT_CHAT_DB")).resolve()
    bridge_id = str(_required(args.expect_bridge, "--expect-bridge", "AGENT_CHAT_BRIDGE"))
    room = core.validate_name(str(_required(args.room, "--room", "AGENT_CHAT_ROOM")), "room")
    endpoint = core.validate_name(
        str(_required(args.endpoint, "--endpoint", "AGENT_CHAT_ENDPOINT")), "endpoint"
    )
    with closing(core.connect(database)) as connection:
        core.require_bridge(connection, bridge_id)
        # This gives an early, friendly error only. The Channel process performs
        # the authoritative atomic adapter-lease acquisition after launch.
        current = core.members(connection, room=room)["members"]
        duplicate = next(
            (
                member
                for member in current
                if member["endpoint"] == endpoint and member["adapter_online"]
            ),
            None,
        )
        if duplicate is not None:
            raise core.ChatError(
                f"endpoint {endpoint} already has a live {duplicate['adapter']} adapter"
            )
    working_directory = args.cwd.resolve()
    if not working_directory.is_dir():
        raise core.ChatError(f"Claude working directory does not exist: {working_directory}")
    return {
        "database": str(database),
        "bridge_id": bridge_id,
        "room": room,
        "endpoint": endpoint,
        "cwd": str(working_directory),
    }


def _channel_name(context: dict[str, str]) -> str:
    identity = "\0".join((context["bridge_id"], context["room"], context["endpoint"])).encode(
        "utf-8"
    )
    return "agent-chat-" + hashlib.sha256(identity).hexdigest()[:12]


def _mcp_config(
    *,
    context: dict[str, str],
    channel_name: str,
) -> dict[str, Any]:
    return {
        "mcpServers": {
            channel_name: {
                "command": str(Path(sys.executable).resolve()),
                "args": ["-m", "agent_chat.claude_channel"],
                "env": {
                    "AGENT_CHAT_DB": context["database"],
                    "AGENT_CHAT_BRIDGE": context["bridge_id"],
                    "AGENT_CHAT_ROOM": context["room"],
                    "AGENT_CHAT_ENDPOINT": context["endpoint"],
                    "AGENT_CHAT_SYSTEM": "claude",
                },
            }
        }
    }


def _launch_arguments(
    args: argparse.Namespace,
    *,
    claude: str,
    config_path: Path,
    channel_name: str,
    endpoint: str,
) -> list[str]:
    bridge_tools = ",".join(
        f"mcp__{channel_name}__{tool}"
        for tool in (
            "agent_chat_observe",
            "agent_chat_progress",
            "agent_chat_reply",
            "agent_chat_send",
        )
    )
    command = [
        claude,
        "--mcp-config",
        str(config_path),
        "--dangerously-load-development-channels",
        f"server:{channel_name}",
        "--name",
        args.name or f"Cross-Agent Bridge — {endpoint}",
        "--allowedTools",
        bridge_tools,
    ]
    if args.strict_mcp_config:
        command.append("--strict-mcp-config")
    if args.resume:
        command.extend(("--resume", args.resume))
    elif args.continue_session:
        command.append("--continue")
    elif args.session_id:
        command.extend(("--session-id", args.session_id))
    if args.model:
        command.extend(("--model", args.model))
    if args.effort:
        command.extend(("--effort", args.effort))
    if args.permission_mode:
        command.extend(("--permission-mode", args.permission_mode))
    if args.no_chrome:
        command.append("--no-chrome")
    if args.prompt:
        command.append(args.prompt)
    return command


def run(args: argparse.Namespace) -> int:
    bridge_profile.apply(args)
    context = _bridge_context(args)
    claude = _resolve_program(args.claude_program, "Claude Code")
    claude_version = _program_version(claude, "Claude Code")
    if claude_version < MINIMUM_CLAUDE_VERSION:
        raise core.ChatError(
            "Claude Code 2.1.80 or newer is required for Channels; found "
            + ".".join(str(part) for part in claude_version)
        )
    channel_name = _channel_name(context)
    readiness = {
        "status": "ready" if args.check else "launching",
        "surface": "claude-code-interactive-terminal",
        "endpoint": context["endpoint"],
        "room": context["room"],
        "bridge_id": context["bridge_id"],
        "channel": channel_name,
        "claude_version": ".".join(str(part) for part in claude_version),
        "channel_runtime": "packaged-python",
        "desktop_code_tab_push_supported": False,
    }
    print(json.dumps(readiness, ensure_ascii=True, separators=(",", ":")), flush=True)
    if args.check:
        return 0

    with tempfile.TemporaryDirectory(prefix="agent-chat-claude-") as directory:
        config_path = Path(directory) / "channel.mcp.json"
        config_path.write_text(
            json.dumps(
                _mcp_config(
                    context=context,
                    channel_name=channel_name,
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        command = _launch_arguments(
            args,
            claude=claude,
            config_path=config_path,
            channel_name=channel_name,
            endpoint=context["endpoint"],
        )
        try:
            return subprocess.call(command, cwd=context["cwd"])
        except OSError as exc:
            raise core.ChatError(f"could not start Claude Code: {exc}") from exc


def main() -> None:
    args = parser().parse_args()
    try:
        raise SystemExit(run(args))
    except (core.ChatError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
