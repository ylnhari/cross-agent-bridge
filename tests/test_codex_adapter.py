from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_chat import core
from agent_chat.codex_adapter import CodexAdapter, PendingDelivery


class CodexAdapterCoordinationTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def adapter() -> CodexAdapter:
        return CodexAdapter(
            database=Path("unused.sqlite3"),
            bridge_id="unused",
            room="test",
            endpoint="codex.test",
            thread_id="thread-test",
            app_server_argv=[sys.executable, "-c", "pass"],
        )

    @staticmethod
    def delivery() -> PendingDelivery:
        return PendingDelivery(
            message_id=1,
            sender="sender.test",
            text="test",
            receipt="receipt-test",
            attempt=1,
        )

    async def test_injection_barrier_is_bounded(self) -> None:
        delivery = self.delivery()
        with mock.patch("agent_chat.codex_adapter.INJECTION_BARRIER_TIMEOUT", 0.01):
            with self.assertRaisesRegex(core.ChatError, "not durably injected"):
                await self.adapter()._await_injected(delivery)

    async def test_one_waiting_tool_request_does_not_block_another(self) -> None:
        adapter = self.adapter()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()

        async def handle(request: dict[str, object]) -> None:
            if request["id"] == 1:
                first_started.set()
                await release_first.wait()
            else:
                second_finished.set()

        adapter._handle_dynamic_tool = handle  # type: ignore[method-assign]
        loop = asyncio.create_task(adapter.server_request_loop())
        try:
            await adapter.app.server_requests.put({"id": 1, "method": "item/tool/call"})
            await adapter.app.server_requests.put({"id": 2, "method": "item/tool/call"})
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.wait_for(second_finished.wait(), timeout=1)
        finally:
            release_first.set()
            loop.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop
