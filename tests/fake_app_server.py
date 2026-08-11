"""Deterministic JSONL app-server double used by process-boundary adapter tests."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable

thread_id = "thread-visible"
turn_id = "turn-long"
active = False
first_message_id: int | None = None
second_message_id: int | None = None
transient_message_id: int | None = None
transient_failures: set[int] = set()
next_server_id = 900
pending_calls: dict[int, Callable[[], None]] = {}


def emit(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def response(request: dict, result: dict) -> None:
    emit({"id": request["id"], "result": result})


def message_id(params: dict) -> int:
    encoded = json.dumps(params, ensure_ascii=False)
    match = re.search(r"\[agent-chat message (\d+)\]", encoded)
    if match is None:
        raise RuntimeError("adapter input did not contain an agent-chat message ID")
    return int(match.group(1))


def agent_message(item_id: str, text: str, phase: str) -> None:
    emit(
        {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "id": item_id,
                    "type": "agentMessage",
                    "text": text,
                    "phase": phase,
                },
            },
        }
    )


def call_tool(tool: str, arguments: dict, after: Callable[[], None]) -> None:
    global next_server_id
    request_id = next_server_id
    next_server_id += 1
    pending_calls[request_id] = after
    emit(
        {
            "id": request_id,
            "method": "item/tool/call",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "callId": f"call-{request_id}",
                "tool": tool,
                "arguments": arguments,
            },
        }
    )


def complete_turn() -> None:
    global active
    agent_message("ui-final", "VISIBLE_FINAL_ONLY_NOT_RELAYED", "final_answer")
    emit(
        {
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {
                    "id": turn_id,
                    "status": "completed",
                    "items": [],
                    "error": None,
                },
            },
        }
    )
    active = False


def after_first_observe() -> None:
    assert first_message_id is not None
    call_tool(
        "agent_chat_progress",
        {"message_id": first_message_id, "text": "CODEX_STARTED_LONG_TASK"},
        lambda: None,
    )


def after_second_observe() -> None:
    assert second_message_id is not None
    call_tool(
        "agent_chat_reply",
        {"message_id": second_message_id, "text": "CODEX_RECEIVED_STEER"},
        after_second_reply,
    )


def after_second_reply() -> None:
    assert first_message_id is not None
    call_tool(
        "agent_chat_reply",
        {"message_id": first_message_id, "text": "CODEX_LONG_TASK_COMPLETE"},
        complete_turn,
    )


def after_transient_observe() -> None:
    assert transient_message_id is not None
    call_tool(
        "agent_chat_reply",
        {
            "message_id": transient_message_id,
            "text": "CODEX_RECOVERED_TRANSIENT_DISPATCH",
        },
        complete_turn,
    )


for raw_line in sys.stdin:
    request = json.loads(raw_line)
    method = request.get("method")
    if method is None and "id" in request:
        callback = pending_calls.pop(int(request["id"]), None)
        if callback is not None:
            callback()
        continue
    if "id" not in request:
        continue
    if method == "initialize":
        response(
            request,
            {
                "userAgent": "fake-app-server",
                "platformFamily": "windows",
                "platformOs": "windows",
            },
        )
    elif method == "thread/start":
        names = {tool.get("name") for tool in request["params"].get("dynamicTools", [])}
        required = {
            "agent_chat_observe",
            "agent_chat_progress",
            "agent_chat_reply",
            "agent_chat_send",
        }
        if names != required:
            emit(
                {
                    "id": request["id"],
                    "error": {"code": -32602, "message": "bridge tools missing"},
                }
            )
        else:
            response(request, {"thread": {"id": thread_id, "turns": []}})
    elif method == "test/echo":
        response(
            request,
            {
                "text": request["params"]["text"],
                "wire_ascii": raw_line.isascii(),
            },
        )
    elif method == "thread/name/set":
        response(request, {})
    elif method == "thread/resume":
        response(request, {"thread": {"id": thread_id, "turns": []}})
    elif method == "thread/read":
        turns = [{"id": turn_id, "status": "inProgress", "items": []}] if active else []
        response(request, {"thread": {"id": thread_id, "turns": turns}})
    elif method == "turn/start":
        candidate_id = message_id(request["params"])
        encoded = json.dumps(request["params"], ensure_ascii=False)
        if "TRANSIENT_ONCE" in encoded and candidate_id not in transient_failures:
            transient_failures.add(candidate_id)
            emit(
                {
                    "id": request["id"],
                    "error": {
                        "code": -32000,
                        "message": "injected transient dispatch failure",
                    },
                }
            )
            continue
        active = True
        if "TRANSIENT_ONCE" in encoded:
            transient_message_id = candidate_id
        else:
            first_message_id = candidate_id
        response(
            request,
            {"turn": {"id": turn_id, "status": "inProgress", "items": []}},
        )
        emit(
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": "inProgress", "items": []},
                },
            }
        )
        agent_message("ui-start", "VISIBLE_ONLY_NOT_RELAYED", "commentary")
        if "TRANSIENT_ONCE" in encoded:
            call_tool(
                "agent_chat_observe",
                {"message_id": transient_message_id},
                after_transient_observe,
            )
        else:
            call_tool(
                "agent_chat_observe",
                {"message_id": first_message_id},
                after_first_observe,
            )
    elif method == "turn/steer":
        second_message_id = message_id(request["params"])
        response(request, {"turnId": turn_id})
        call_tool(
            "agent_chat_observe",
            {"message_id": second_message_id},
            after_second_observe,
        )
    else:
        emit(
            {
                "id": request["id"],
                "error": {"code": -32601, "message": f"unsupported: {method}"},
            }
        )
