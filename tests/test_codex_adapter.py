from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_chat import core
from agent_chat.app_server import AppServerError
from agent_chat.codex_adapter import (
    UNACTED_CONTINUATION_LIMIT,
    CodexAdapter,
    PendingDelivery,
    open_thread_in_app,
    shell_command,
    thread_url,
)


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

    async def test_observed_unacted_root_gets_bounded_continuation(self) -> None:
        adapter = self.adapter()
        delivery = self.delivery()
        delivery.acknowledged = True
        adapter.pending[delivery.message_id] = delivery

        for index in range(UNACTED_CONTINUATION_LIMIT):
            turn_id = f"turn-{index}"
            delivery.turn_id = turn_id
            adapter.pending_by_turn[turn_id] = {delivery.message_id}
            adapter.completed_turns[turn_id] = {"id": turn_id, "status": "completed"}
            continuations = await adapter._complete_turn(turn_id)
            self.assertEqual(continuations, [delivery])
            self.assertEqual(delivery.continuation_attempts, index + 1)
            self.assertIsNone(delivery.turn_id)

        turn_id = "turn-exhausted"
        delivery.turn_id = turn_id
        adapter.pending_by_turn[turn_id] = {delivery.message_id}
        adapter.completed_turns[turn_id] = {"id": turn_id, "status": "completed"}
        with mock.patch.object(
            adapter, "_report_unacted_attention", new=mock.AsyncMock(return_value=True)
        ) as report:
            self.assertEqual(await adapter._complete_turn(turn_id), [])
        report.assert_awaited_once_with(delivery)

    async def test_database_calls_do_not_block_the_event_loop(self) -> None:
        adapter = self.adapter()

        def slow_call(*args, **kwargs):
            time.sleep(0.2)
            return {"status": "ok"}

        adapter._database_call = slow_call  # type: ignore[method-assign]
        database = asyncio.create_task(adapter._db(lambda connection: None))
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        self.assertFalse(database.done())
        self.assertEqual(await database, {"status": "ok"})

    def test_recovery_matches_only_structured_user_input_prefix(self) -> None:
        turn = {
            "id": "turn-test",
            "status": "inProgress",
            "items": [
                {
                    "type": "userMessage",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "[agent-chat message 2]\nreal envelope\n"
                                "untrusted text mentions [agent-chat message 1]"
                            ),
                        }
                    ],
                },
                {
                    "type": "agentMessage",
                    "text": "[agent-chat message 1]\nmodel echo",
                },
            ],
        }
        self.assertTrue(CodexAdapter._turn_has_injected_message(turn, 2))
        self.assertFalse(CodexAdapter._turn_has_injected_message(turn, 1))

    async def test_acted_root_waits_without_automatic_turn(self) -> None:
        adapter = self.adapter()
        delivery = self.delivery()
        delivery.acknowledged = True
        delivery.acted = True
        delivery.turn_id = "turn-waiting"
        adapter.pending[delivery.message_id] = delivery
        adapter.pending_by_turn["turn-waiting"] = {delivery.message_id}
        adapter.completed_turns["turn-waiting"] = {
            "id": "turn-waiting",
            "status": "completed",
        }

        self.assertEqual(await adapter._complete_turn("turn-waiting"), [])
        self.assertIn(delivery.message_id, adapter.pending)
        self.assertEqual(delivery.continuation_attempts, 0)

    async def test_early_turn_completion_is_retained_for_dispatch_replay(self) -> None:
        adapter = self.adapter()
        adapter.completed_turns["turn-race"] = {"id": "turn-race", "status": "completed"}

        self.assertEqual(await adapter._complete_turn("turn-race"), [])
        self.assertIn("turn-race", adapter.completed_turns)

    async def test_refresh_rejects_parallel_in_progress_turns(self) -> None:
        adapter = self.adapter()
        adapter.app.request = mock.AsyncMock(  # type: ignore[method-assign]
            return_value={
                "thread": {
                    "turns": [
                        {"id": "turn-bridge", "status": "inProgress"},
                        {"id": "turn-ui", "status": "inProgress"},
                    ]
                }
            }
        )

        with self.assertRaisesRegex(AppServerError, "multiple in-progress turns"):
            await adapter._refresh_thread()
        self.assertIsNone(adapter.active_turn_id)

    def test_thread_deep_link_targets_exact_task(self) -> None:
        self.assertEqual(
            thread_url("thread id/with separators"),
            "codex://threads/thread%20id%2Fwith%20separators",
        )
        with mock.patch("agent_chat.codex_adapter.webbrowser.open", return_value=True) as opened:
            self.assertTrue(open_thread_in_app("019f-test"))
        opened.assert_called_once_with("codex://threads/019f-test", new=0, autoraise=True)

    def test_turn_start_applies_explicit_model_and_effort(self) -> None:
        adapter = CodexAdapter(
            database=Path("unused.sqlite3"),
            bridge_id="unused",
            room="test",
            endpoint="codex.test",
            thread_id="thread-test",
            app_server_argv=[sys.executable, "-c", "pass"],
            model="gpt-5.6-luna",
            effort="low",
        )
        params = adapter._turn_start_params(self.delivery())
        self.assertEqual(params["threadId"], "thread-test")
        self.assertEqual(params["model"], "gpt-5.6-luna")
        self.assertEqual(params["effort"], "low")

    def test_legacy_input_contains_exact_local_cli_routing(self) -> None:
        adapter = CodexAdapter(
            database=Path("unused.sqlite3"),
            bridge_id="unused",
            room="test",
            endpoint="codex.test",
            thread_id="thread-test",
            app_server_argv=[sys.executable, "-c", "pass"],
            legacy_cli_bridge=True,
            cli_prefix=[sys.executable, "-m", "agent_chat", "--profile", "bridge.json"],
        )
        delivery = self.delivery()
        text = adapter._input_text(delivery)
        self.assertIn("attached legacy task has no persisted agent_chat_* tools", text)
        self.assertIn("ack", text)
        self.assertIn("--receipt", text)
        self.assertIn("receipt-test", text)
        self.assertIn("--kind", text)
        self.assertIn("progress", text)
        self.assertIn("reply", text)
        self.assertIn("codex-legacy-1-final", text)
        self.assertIn("Do not receive or poll", text)

    async def test_legacy_retry_refreshes_the_embedded_receipt(self) -> None:
        adapter = CodexAdapter(
            database=Path("unused.sqlite3"),
            bridge_id="unused",
            room="test",
            endpoint="codex.test",
            thread_id="thread-test",
            app_server_argv=[sys.executable, "-c", "pass"],
            legacy_cli_bridge=True,
            cli_prefix=[sys.executable, "-m", "agent_chat", "--profile", "bridge.json"],
        )
        delivery = self.delivery()
        delivery.attempt = 2
        delivery.receipt = "receipt-current"
        adapter._refresh_thread = mock.AsyncMock()  # type: ignore[method-assign]

        self.assertIsNone(await adapter._recover_prior_dispatch(delivery))
        adapter._refresh_thread.assert_not_awaited()
        text = adapter._input_text(delivery)
        self.assertIn("Delivery recovery attempt 2", text)
        self.assertIn("receipt-current", text)
        self.assertIn("do not repeat completed side effects", text)

    async def test_legacy_external_progress_prevents_a_spurious_continuation(self) -> None:
        adapter = self.adapter()
        adapter.legacy_cli_bridge = True
        delivery = self.delivery()
        delivery.turn_id = "turn-legacy-progress"
        adapter.pending[delivery.message_id] = delivery
        adapter.pending_by_turn[delivery.turn_id] = {delivery.message_id}
        adapter.completed_turns[delivery.turn_id] = {
            "id": delivery.turn_id,
            "status": "completed",
        }
        adapter._db = mock.AsyncMock(  # type: ignore[method-assign]
            return_value={
                "delivery": {
                    "state": "acted",
                    "acked_at": "2026-08-11T00:00:00Z",
                    "replied_at": None,
                }
            }
        )

        self.assertEqual(await adapter._complete_turn(delivery.turn_id), [])
        self.assertTrue(delivery.acknowledged)
        self.assertTrue(delivery.acted)
        self.assertFalse(delivery.replied)
        self.assertIn(delivery.message_id, adapter.pending)
        self.assertEqual(delivery.continuation_attempts, 0)

    async def test_legacy_external_final_reply_archives_the_delivery(self) -> None:
        adapter = self.adapter()
        adapter.legacy_cli_bridge = True
        delivery = self.delivery()
        delivery.turn_id = "turn-legacy-final"
        adapter.pending[delivery.message_id] = delivery
        adapter.pending_by_turn[delivery.turn_id] = {delivery.message_id}
        adapter.completed_turns[delivery.turn_id] = {
            "id": delivery.turn_id,
            "status": "completed",
        }
        adapter._db = mock.AsyncMock(  # type: ignore[method-assign]
            return_value={
                "delivery": {
                    "state": "replied",
                    "acked_at": "2026-08-11T00:00:00Z",
                    "replied_at": "2026-08-11T00:00:00Z",
                }
            }
        )

        self.assertEqual(await adapter._complete_turn(delivery.turn_id), [])
        self.assertNotIn(delivery.message_id, adapter.pending)
        self.assertIn(delivery.message_id, adapter.completed)
        self.assertTrue(delivery.replied)

    def test_shell_command_quotes_every_windows_argument(self) -> None:
        command = shell_command(["C:\\Program Files\\Python\\python.exe", "a'b", "plain"])
        if sys.platform == "win32":
            self.assertEqual(
                command,
                "& 'C:\\Program Files\\Python\\python.exe' 'a''b' 'plain'",
            )
        else:
            self.assertEqual(command, "'C:\\Program Files\\Python\\python.exe' 'a'\"'\"'b' plain")
