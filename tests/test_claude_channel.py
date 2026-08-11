from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

from agent_chat import core
from agent_chat.claude_channel import (
    MAX_JSONL_BYTES,
    ChannelConfig,
    ClaudeChannelServer,
    TrackedDelivery,
)


class McpChannelProcess:
    def __init__(self, environment: dict[str, str]) -> None:
        source = str(Path(__file__).resolve().parents[1] / "src")
        python_path = source
        if os.environ.get("PYTHONPATH"):
            python_path += os.pathsep + os.environ["PYTHONPATH"]
        self.process = subprocess.Popen(
            [sys.executable, "-m", "agent_chat.claude_channel"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": python_path, **environment},
        )
        assert self.process.stdout is not None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.deferred: list[dict[str, Any]] = []
        self.next_id = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def send(self, value: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def _matching(self, predicate, timeout: float = 10) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for index, item in enumerate(self.deferred):
                if predicate(item):
                    return self.deferred.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for MCP message")
            try:
                item = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("timed out waiting for MCP message") from exc
            if predicate(item):
                return item
            self.deferred.append(item)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        response = self._matching(lambda item: item.get("id") == request_id)
        if "error" in response:
            raise AssertionError(response["error"])
        return response["result"]

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "channel-test", "version": "1"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def notification(self, method: str, timeout: float = 10) -> dict[str, Any]:
        return self._matching(lambda item: item.get("method") == method, timeout)

    def close(self) -> tuple[int, str]:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait(timeout=5)
        assert self.process.stderr is not None
        stderr = self.process.stderr.read().decode("utf-8", errors="replace")
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.process.stderr.close()
        return code, stderr

    def kill(self) -> tuple[int, str]:
        self.process.kill()
        code = self.process.wait(timeout=5)
        assert self.process.stderr is not None
        stderr = self.process.stderr.read().decode("utf-8", errors="replace")
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.process.stderr.close()
        return code, stderr


class ClaudeChannelProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "chat.sqlite3"
        self.room = "test"
        self.claude = "claude.channel.test"
        self.source = "source.channel.test"
        with closing(core.connect(self.database, create=True)) as connection:
            core.create_room(connection, self.room)
            self.bridge_id = core.bridge_info(connection, self.database)["id"]
            core.join(
                connection,
                room=self.room,
                endpoint=self.source,
                system="test",
            )

    def environment(self) -> dict[str, str]:
        return {
            "AGENT_CHAT_DB": str(self.database),
            "AGENT_CHAT_BRIDGE": self.bridge_id,
            "AGENT_CHAT_ROOM": self.room,
            "AGENT_CHAT_ENDPOINT": self.claude,
            "AGENT_CHAT_SYSTEM": "claude",
            "AGENT_CHAT_ADAPTER_LEASE_SECONDS": "5",
        }

    def start(self) -> McpChannelProcess:
        channel = McpChannelProcess(self.environment())
        self.addCleanup(self._close_if_running, channel)
        initialized = channel.initialize()
        self.assertIn("claude/channel", initialized["capabilities"]["experimental"])
        return channel

    @staticmethod
    def _close_if_running(channel: McpChannelProcess) -> None:
        if channel.process.poll() is None:
            channel.close()

    def send_root(self, text: str = "synthetic request") -> dict[str, Any]:
        with closing(core.connect(self.database)) as connection:
            return core.send(
                connection,
                room=self.room,
                sender=self.source,
                text=text,
                recipients=[self.claude],
            )

    def delivery(self, message_id: int) -> dict[str, Any]:
        with closing(core.connect(self.database)) as connection:
            return core.delivery_detail(
                connection,
                room=self.room,
                recipient=self.claude,
                message_id=message_id,
            )["delivery"]

    def test_process_delivers_tools_and_exactly_one_final_reply(self) -> None:
        channel = self.start()
        root = self.send_root()
        message_id = root["message"]["id"]
        event = channel.notification("notifications/claude/channel")
        self.assertEqual(event["params"]["meta"]["message_id"], str(message_id))
        self.assertEqual(event["params"]["meta"]["sender"], self.source)

        tools = channel.request("tools/list", {})["tools"]
        send_tool = next(item for item in tools if item["name"] == "agent_chat_send")
        self.assertEqual(send_tool["inputSchema"]["required"], ["to", "text", "request_key"])

        observed = channel.request(
            "tools/call",
            {"name": "agent_chat_observe", "arguments": {"message_id": message_id}},
        )
        self.assertFalse(observed["isError"])
        progress = channel.request(
            "tools/call",
            {
                "name": "agent_chat_progress",
                "arguments": {"message_id": message_id, "text": "working"},
            },
        )
        self.assertFalse(progress["isError"])
        replied = channel.request(
            "tools/call",
            {
                "name": "agent_chat_reply",
                "arguments": {"message_id": message_id, "text": "done"},
            },
        )
        self.assertFalse(replied["isError"])
        retried = channel.request(
            "tools/call",
            {
                "name": "agent_chat_reply",
                "arguments": {"message_id": message_id, "text": "done"},
            },
        )
        self.assertFalse(retried["isError"])
        retry_result = json.loads(retried["content"][0]["text"])
        self.assertTrue(retry_result["duplicate"])
        conflicting = channel.request(
            "tools/call",
            {
                "name": "agent_chat_reply",
                "arguments": {"message_id": message_id, "text": "different"},
            },
        )
        self.assertTrue(conflicting["isError"])
        self.assertIn("final reply was already sent", conflicting["content"][0]["text"])
        self.assertEqual(self.delivery(message_id)["state"], "replied")

        with closing(core.connect(self.database)) as connection:
            first = core.receive_one(
                connection,
                room=self.room,
                recipient=self.source,
                visibility=30,
            )
            assert first is not None
            core.acknowledge(
                connection,
                room=self.room,
                recipient=self.source,
                message_id=first["message"]["id"],
                receipt=first["delivery"]["receipt"],
            )
            second = core.receive_one(
                connection,
                room=self.room,
                recipient=self.source,
                visibility=30,
            )
            assert second is not None
            core.acknowledge(
                connection,
                room=self.room,
                recipient=self.source,
                message_id=second["message"]["id"],
                receipt=second["delivery"]["receipt"],
            )
            self.assertEqual(
                [
                    (first["message"]["kind"], first["message"]["text"]),
                    (second["message"]["kind"], second["message"]["text"]),
                ],
                [("progress", "working"), ("reply", "done")],
            )
            self.assertIsNone(
                core.receive_one(
                    connection,
                    room=self.room,
                    recipient=self.source,
                    visibility=30,
                )
            )

        code, stderr = channel.close()
        self.assertEqual(code, 0, stderr)

    def test_eof_releases_unfinished_delivery_and_adapter_lease(self) -> None:
        channel = self.start()
        root = self.send_root("unfinished")
        message_id = root["message"]["id"]
        channel.notification("notifications/claude/channel")
        deadline = time.monotonic() + 5
        while self.delivery(message_id)["state"] != "injected":
            if time.monotonic() >= deadline:
                self.fail("delivery did not reach injected")
            time.sleep(0.05)
        code, stderr = channel.close()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.delivery(message_id)["state"], "queued")
        with closing(core.connect(self.database)) as connection:
            member = next(
                item
                for item in core.members(connection, room=self.room)["members"]
                if item["endpoint"] == self.claude
            )
        self.assertFalse(member["adapter_online"])

    def test_observed_reply_is_terminal_across_channel_reconnect(self) -> None:
        with closing(core.connect(self.database)) as connection:
            core.join(
                connection,
                room=self.room,
                endpoint=self.claude,
                system="claude",
            )
            root = core.send(
                connection,
                room=self.room,
                sender=self.claude,
                text="question",
                recipients=[self.source],
            )
            claimed = core.receive_one(
                connection,
                room=self.room,
                recipient=self.source,
                visibility=30,
            )
            assert claimed is not None
            core.acknowledge(
                connection,
                room=self.room,
                recipient=self.source,
                message_id=claimed["message"]["id"],
                receipt=claimed["delivery"]["receipt"],
            )
            answer = core.send(
                connection,
                room=self.room,
                sender=self.source,
                text="answer",
                recipients=[self.claude],
                reply_to=root["message"]["id"],
                kind="reply",
            )

        first = self.start()
        event = first.notification("notifications/claude/channel")
        answer_id = answer["message"]["id"]
        self.assertEqual(event["params"]["meta"]["message_id"], str(answer_id))
        result = first.request(
            "tools/call",
            {"name": "agent_chat_observe", "arguments": {"message_id": answer_id}},
        )
        self.assertFalse(result["isError"])
        code, stderr = first.close()
        self.assertEqual(code, 0, stderr)

        second = self.start()
        with self.assertRaises(TimeoutError):
            second.notification("notifications/claude/channel", timeout=0.5)
        self.assertEqual(self.delivery(answer_id)["state"], "observed")
        code, stderr = second.close()
        self.assertEqual(code, 0, stderr)

    def test_hard_kill_requeues_observed_root_after_lease_expiry(self) -> None:
        first = self.start()
        root = self.send_root("observed before hard kill")
        message_id = root["message"]["id"]
        first.notification("notifications/claude/channel")
        observed = first.request(
            "tools/call",
            {"name": "agent_chat_observe", "arguments": {"message_id": message_id}},
        )
        self.assertFalse(observed["isError"])
        code, _ = first.kill()
        self.assertNotEqual(code, 0)

        time.sleep(5.2)
        second = self.start()
        event = second.notification("notifications/claude/channel")
        self.assertEqual(event["params"]["meta"]["message_id"], str(message_id))
        deadline = time.monotonic() + 5
        detail = self.delivery(message_id)
        while detail["state"] != "injected":
            if time.monotonic() >= deadline:
                self.fail(f"recovered delivery remained {detail['state']}")
            time.sleep(0.05)
            detail = self.delivery(message_id)
        self.assertEqual(detail["attempts"], 2)
        self.assertEqual(detail["state"], "injected")
        code, stderr = second.close()
        self.assertEqual(code, 0, stderr)

    def test_oversized_protocol_frame_fails_closed_and_releases_lease(self) -> None:
        source = str(Path(__file__).resolve().parents[1] / "src")
        python_path = source
        if os.environ.get("PYTHONPATH"):
            python_path += os.pathsep + os.environ["PYTHONPATH"]
        process = subprocess.run(
            [sys.executable, "-m", "agent_chat.claude_channel"],
            input=b"x" * (MAX_JSONL_BYTES + 1) + b"\n",
            capture_output=True,
            env={**os.environ, "PYTHONPATH": python_path, **self.environment()},
            timeout=20,
            check=False,
        )
        stderr = process.stderr.decode("utf-8", errors="replace")
        self.assertEqual(process.returncode, 2, stderr)
        self.assertIn("JSON-RPC frame exceeds", stderr)
        with closing(core.connect(self.database)) as connection:
            member = next(
                item
                for item in core.members(connection, room=self.room)["members"]
                if item["endpoint"] == self.claude
            )
        self.assertFalse(member["adapter_online"])


class ClaudeChannelOrderingTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_waits_for_durable_injection(self) -> None:
        server = ClaudeChannelServer(
            ChannelConfig(
                database=Path("unused.sqlite3"),
                bridge_id="unused",
                room="test",
                endpoint="claude.test",
            )
        )
        delivery = TrackedDelivery(receipt="receipt", sender="source.test", kind="message")
        server.tracked[1] = delivery
        database_called = asyncio.Event()

        async def fake_db(*args, **kwargs):
            database_called.set()
            return {"status": "observed"}

        server._db = fake_db  # type: ignore[method-assign]
        call = asyncio.create_task(server._call_tool("agent_chat_observe", {"message_id": 1}))
        await asyncio.sleep(0)
        self.assertFalse(database_called.is_set())
        self.assertFalse(call.done())

        delivery.injected.set()
        result = await asyncio.wait_for(call, timeout=1)
        self.assertFalse(result["isError"])
        self.assertTrue(database_called.is_set())

    async def test_unacted_watchdog_reminds_then_notifies_peer(self) -> None:
        server = ClaudeChannelServer(
            ChannelConfig(
                database=Path("unused.sqlite3"),
                bridge_id="unused",
                room="test",
                endpoint="claude.test",
            ),
            unacted_reminder_seconds=0.01,
            unacted_reminder_limit=1,
        )
        delivery = TrackedDelivery(
            receipt="receipt",
            sender="source.test",
            kind="message",
            acknowledged=True,
            observed_at=time.monotonic() - 1,
        )
        delivery.injected.set()
        server.tracked[1] = delivery
        notifications: list[dict[str, Any]] = []
        database_calls: list[tuple[object, dict[str, Any]]] = []

        async def fake_notification(method: str, params: dict[str, Any]) -> None:
            notifications.append({"method": method, "params": params})

        async def fake_db(function, **kwargs):
            database_calls.append((function, kwargs))
            return {"status": "sent"}

        server._notification = fake_notification  # type: ignore[method-assign]
        server._db = fake_db  # type: ignore[method-assign]
        watchdog = asyncio.create_task(server._unacted_watchdog())
        try:
            await asyncio.wait_for(self._wait_until(lambda: delivery.attention_reported), timeout=1)
        finally:
            server.stopping.set()
            await watchdog

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["params"]["meta"]["kind"], "bridge_reminder")
        self.assertTrue(delivery.acted)
        self.assertEqual(database_calls[0][0], core.send)
        self.assertEqual(database_calls[0][1]["kind"], "progress")

    async def test_startup_failure_after_lease_still_runs_shutdown(self) -> None:
        server = ClaudeChannelServer(
            ChannelConfig(
                database=Path("unused.sqlite3"),
                bridge_id="unused",
                room="test",
                endpoint="claude.test",
            )
        )
        server._bootstrap = mock.AsyncMock()  # type: ignore[method-assign]
        server._start_reader = mock.Mock(side_effect=RuntimeError("reader failed"))  # type: ignore[method-assign]
        server._shutdown = mock.AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "reader failed"):
            await server.run()
        server._shutdown.assert_awaited_once()

    async def test_shutdown_releases_lease_after_unexpected_pump_error(self) -> None:
        server = ClaudeChannelServer(
            ChannelConfig(
                database=Path("unused.sqlite3"),
                bridge_id="unused",
                room="test",
                endpoint="claude.test",
            )
        )
        server.lease_acquired = True

        async def broken_pump() -> None:
            raise RuntimeError("unexpected pump failure")

        server._pump_task = asyncio.create_task(broken_pump())
        await asyncio.sleep(0)
        calls: list[object] = []

        async def fake_db(function, **kwargs):
            calls.append(function)
            return {"status": "released"}

        server._db = fake_db  # type: ignore[method-assign]
        await server._shutdown()
        self.assertIn(core.release_adapter_lease, calls)
        self.assertFalse(server.lease_acquired)

    @staticmethod
    async def _wait_until(predicate) -> None:
        while not predicate():
            await asyncio.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
