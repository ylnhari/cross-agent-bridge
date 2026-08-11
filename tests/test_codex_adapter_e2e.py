from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_chat import core  # noqa: E402
from agent_chat.codex_adapter import dynamic_tools  # noqa: E402


class CodexAdapterE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "chat.sqlite3"
        connection = core.connect(self.database, create=True)
        core.create_room(connection, "live")
        core.join(
            connection,
            room="live",
            endpoint="claude.live",
            system="claude",
        )
        self.bridge_id = core.bridge_info(connection, self.database)["id"]
        connection.close()
        self.env = os.environ.copy()
        existing = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + existing if existing else "")
        self.env["AGENT_CHAT_FAKE_DB"] = str(self.database)
        self.command = [
            sys.executable,
            "-m",
            "agent_chat.codex_adapter",
            "--db",
            str(self.database),
            "--expect-bridge",
            self.bridge_id,
            "--room",
            "live",
            "--endpoint",
            "codex.live",
            "--create-thread",
            "--cwd",
            str(ROOT),
            "--title",
            "Bridge adapter test",
            "--transport",
            "standalone",
            "--wait",
            "0.1",
            "--visibility",
            "3",
            "--adapter-lease",
            "5",
            "--app-server-program",
            sys.executable,
            "--app-server-arg",
            str(ROOT / "tests" / "fake_app_server.py"),
        ]
        self.adapter = self.launch(self.command)
        self.addCleanup(self.stop_adapter)
        self.wait_for_online_adapter("codex.live", host_ref="thread-visible")

    def launch(self, command: list[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            command,
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def attached_command(self) -> list[str]:
        command = self.command.copy()
        create_index = command.index("--create-thread")
        command[create_index : create_index + 1] = [
            "--thread-id",
            "thread-visible",
        ]
        return command

    def stop_adapter(self) -> None:
        if self.adapter.poll() is None:
            self.adapter.terminate()
            try:
                self.adapter.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.adapter.kill()
                self.adapter.communicate(timeout=5)

    def test_dynamic_send_tool_requires_a_caller_idempotency_key(self) -> None:
        send_tool = next(item for item in dynamic_tools() if item["name"] == "agent_chat_send")
        schema = send_tool["inputSchema"]
        self.assertEqual(schema["required"], ["to", "text", "request_key"])
        self.assertIn("request_key", schema["properties"])

    def connection(self):
        return core.connect(self.database)

    def wait_for_online_adapter(self, endpoint: str, host_ref: str | None = None) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.adapter.poll() is not None:
                _, stderr = self.adapter.communicate(timeout=1)
                self.fail(f"adapter exited early: {stderr}")
            connection = self.connection()
            try:
                member = next(
                    (
                        item
                        for item in core.members(connection, room="live")["members"]
                        if item["endpoint"] == endpoint
                    ),
                    None,
                )
            finally:
                connection.close()
            if (
                member is not None
                and member["adapter_online"]
                and (host_ref is None or member["adapter_host_ref"] == host_ref)
            ):
                return
            time.sleep(0.05)
        self.fail(f"adapter did not become live for {endpoint}")

    def wait_for_offline_adapter(self, endpoint: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            connection = self.connection()
            try:
                member = next(
                    item
                    for item in core.members(connection, room="live")["members"]
                    if item["endpoint"] == endpoint
                )
            finally:
                connection.close()
            if not member["adapter_online"]:
                return
            time.sleep(0.05)
        self.fail(f"adapter lease did not expire for {endpoint}")

    def wait_for_attempt(self, message_id: int, attempt: int) -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            connection = self.connection()
            try:
                detail = core.delivery_detail(
                    connection,
                    room="live",
                    recipient="codex.live",
                    message_id=message_id,
                )["delivery"]
            finally:
                connection.close()
            if detail["attempts"] >= attempt and detail["state"] in {
                "injected",
                "observed",
                "acted",
                "replied",
            }:
                return detail
            time.sleep(0.05)
        self.fail(f"message {message_id} did not reach attempt {attempt}")

    def send_from_claude(self, text: str, reply_to: int | None = None) -> int:
        connection = self.connection()
        try:
            return core.send(
                connection,
                room="live",
                sender="claude.live",
                recipients=["codex.live"],
                text=text,
                reply_to=reply_to,
            )["message"]["id"]
        finally:
            connection.close()

    def receive_for_claude(self, wait: float = 30) -> dict:
        connection = self.connection()
        try:
            delivery = core.wait_for_message(
                connection,
                room="live",
                recipient="claude.live",
                wait=wait,
                poll=0.05,
                visibility=30,
            )
            if delivery is None:
                diagnostic = {
                    "adapter_returncode": self.adapter.poll(),
                    "status": core.status(connection, room="live"),
                    "history": core.history(
                        connection,
                        room="live",
                        viewer="claude.live",
                        limit=50,
                    )["messages"],
                }
                self.fail(f"timed out waiting for Claude delivery: {diagnostic!r}")
            core.acknowledge(
                connection,
                room="live",
                recipient="claude.live",
                message_id=delivery["message"]["id"],
                receipt=delivery["delivery"]["receipt"],
            )
            return delivery["message"]
        finally:
            connection.close()

    def test_live_turn_is_steered_and_conversations_remain_isolated(self) -> None:
        first_id = self.send_from_claude("BEGIN_LONG")
        started = self.receive_for_claude()
        self.assertEqual(started["text"], "CODEX_STARTED_LONG_TASK")
        self.assertEqual(started["reply_to"], first_id)

        second_id = self.send_from_claude("SECOND_CONVERSATION_WHILE_RUNNING")
        replies = [self.receive_for_claude() for _ in range(2)]
        second_replies = [item for item in replies if item["reply_to"] == second_id]
        self.assertEqual(
            {item["text"] for item in second_replies},
            {"CODEX_RECEIVED_STEER"},
        )
        first_replies = [item for item in replies if item["reply_to"] == first_id]
        self.assertEqual(
            {item["text"] for item in first_replies},
            {"CODEX_LONG_TASK_COMPLETE"},
        )
        self.assertEqual(started["conversation_id"], first_replies[0]["conversation_id"])
        self.assertNotEqual(started["conversation_id"], second_replies[0]["conversation_id"])

        connection = self.connection()
        try:
            self.assertIsNone(
                core.wait_for_message(
                    connection,
                    room="live",
                    recipient="claude.live",
                    wait=0.2,
                    poll=0.05,
                    visibility=30,
                )
            )
            for message_id in (first_id, second_id):
                detail = core.delivery_detail(
                    connection,
                    room="live",
                    recipient="codex.live",
                    message_id=message_id,
                )["delivery"]
                self.assertEqual(detail["state"], "replied")
                self.assertEqual(detail["adapter"], "codex.appserver")
                self.assertEqual(detail["host_ref"], "thread-visible")
        finally:
            connection.close()

    def test_transient_dispatch_error_retries_without_stopping_adapter(self) -> None:
        message_id = self.send_from_claude("TRANSIENT_ONCE")
        reply = self.receive_for_claude()
        self.assertEqual(reply["reply_to"], message_id)
        self.assertEqual(reply["text"], "CODEX_RECOVERED_TRANSIENT_DISPATCH")
        self.assertIsNone(self.adapter.poll())

        connection = self.connection()
        try:
            detail = core.delivery_detail(
                connection,
                room="live",
                recipient="codex.live",
                message_id=message_id,
            )["delivery"]
            self.assertEqual(detail["state"], "replied")
            self.assertEqual(detail["attempts"], 2)
            self.assertEqual(
                [event["state"] for event in detail["events"]],
                [
                    "queued",
                    "claimed",
                    "queued",
                    "claimed",
                    "injected",
                    "observed",
                    "acted",
                    "replied",
                ],
            )
        finally:
            connection.close()

    def test_root_remains_pending_across_turns_until_peer_reply_arrives(self) -> None:
        root_id = self.send_from_claude("WAIT_ACROSS_TURN")
        initial = [self.receive_for_claude() for _ in range(2)]
        progress = next(item for item in initial if item["kind"] == "progress")
        question = next(item for item in initial if item["kind"] == "message")
        self.assertEqual(progress["reply_to"], root_id)
        self.assertEqual(progress["text"], "CODEX_WAITING_FOR_CROSS_TURN")
        self.assertEqual(question["text"], "CODEX_CROSS_TURN_QUESTION")

        answer_id = self.send_from_claude(
            "CROSS_TURN_ANSWER",
            reply_to=question["id"],
        )
        final = self.receive_for_claude()
        self.assertEqual(final["reply_to"], root_id)
        self.assertEqual(final["text"], "CODEX_CROSS_TURN_DONE")

        connection = self.connection()
        try:
            root = core.delivery_detail(
                connection,
                room="live",
                recipient="codex.live",
                message_id=root_id,
            )["delivery"]
            answer = core.delivery_detail(
                connection,
                room="live",
                recipient="codex.live",
                message_id=answer_id,
            )["delivery"]
            self.assertEqual(root["state"], "replied")
            self.assertEqual(answer["state"], "observed")
            self.assertIsNone(
                core.wait_for_message(
                    connection,
                    room="live",
                    recipient="claude.live",
                    wait=0.2,
                    poll=0.05,
                    visibility=30,
                )
            )
        finally:
            connection.close()

    def test_observed_but_unacted_root_is_continued_without_peer_nudge(self) -> None:
        root_id = self.send_from_claude("OBSERVE_ONLY_ONCE")
        final = self.receive_for_claude()
        self.assertEqual(final["reply_to"], root_id)
        self.assertEqual(final["text"], "CODEX_CONTINUATION_COMPLETE")

        connection = self.connection()
        try:
            detail = core.delivery_detail(
                connection,
                room="live",
                recipient="codex.live",
                message_id=root_id,
            )["delivery"]
            self.assertEqual(detail["state"], "replied")
            self.assertEqual(detail["attempts"], 1)
            self.assertEqual(
                [event["state"] for event in detail["events"]],
                ["queued", "claimed", "injected", "observed", "acted", "replied"],
            )
        finally:
            connection.close()

    def test_exhausted_continuations_notify_the_peer_without_a_fake_reply(self) -> None:
        root_id = self.send_from_claude("NEVER_ACT")
        status = self.receive_for_claude()
        self.assertEqual(status["reply_to"], root_id)
        self.assertEqual(status["kind"], "progress")
        self.assertIn("Bridge adapter status", status["text"])
        self.assertIn("request remains open", status["text"])

        connection = self.connection()
        try:
            detail = core.delivery_detail(
                connection,
                room="live",
                recipient="codex.live",
                message_id=root_id,
            )["delivery"]
            self.assertEqual(detail["state"], "acted")
            self.assertIsNone(detail["replied_at"])
            self.assertIsNone(
                core.wait_for_message(
                    connection,
                    room="live",
                    recipient="claude.live",
                    wait=0.2,
                    poll=0.05,
                    visibility=30,
                )
            )
        finally:
            connection.close()

    def test_reply_tool_retry_is_idempotent_and_conflict_is_rejected(self) -> None:
        root_id = self.send_from_claude("DUPLICATE_REPLY")
        reply = self.receive_for_claude()
        self.assertEqual(reply["reply_to"], root_id)
        self.assertEqual(reply["text"], "CODEX_DUPLICATE_OK")

        connection = self.connection()
        try:
            self.assertIsNone(
                core.wait_for_message(
                    connection,
                    room="live",
                    recipient="claude.live",
                    wait=0.2,
                    poll=0.05,
                    visibility=30,
                )
            )
            messages = core.history(
                connection,
                room="live",
                viewer="claude.live",
                limit=50,
            )["messages"]
            final_replies = [
                item for item in messages if item["reply_to"] == root_id and item["kind"] == "reply"
            ]
            self.assertEqual([item["text"] for item in final_replies], ["CODEX_DUPLICATE_OK"])
            self.assertEqual(
                core.delivery_detail(
                    connection,
                    room="live",
                    recipient="codex.live",
                    message_id=root_id,
                )["delivery"]["state"],
                "replied",
            )
        finally:
            connection.close()
        self.assertIsNone(self.adapter.poll())

    def test_duplicate_adapter_is_rejected_and_hard_kill_can_be_taken_over(self) -> None:
        duplicate = subprocess.run(
            self.attached_command(),
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("already has a live", duplicate.stderr)

        first_id = self.send_from_claude("BEGIN_LONG")
        progress = self.receive_for_claude()
        self.assertEqual(progress["reply_to"], first_id)
        self.assertEqual(progress["text"], "CODEX_STARTED_LONG_TASK")

        self.adapter.kill()
        self.adapter.communicate(timeout=5)
        time.sleep(5.2)
        self.adapter = self.launch(self.attached_command())
        self.wait_for_online_adapter("codex.live", host_ref="thread-visible")
        recovered = self.wait_for_attempt(first_id, 2)
        self.assertEqual(recovered["attempts"], 2)

        second_id = self.send_from_claude("SECOND_CONVERSATION_WHILE_RUNNING")
        replies = [self.receive_for_claude() for _ in range(2)]
        by_parent = {item["reply_to"]: item["text"] for item in replies}
        self.assertEqual(by_parent[second_id], "CODEX_RECEIVED_STEER")
        self.assertEqual(by_parent[first_id], "CODEX_LONG_TASK_COMPLETE")
        self.assertIsNone(self.adapter.poll())

    def test_legacy_cli_progress_is_reconciled_without_a_duplicate_wake(self) -> None:
        self.stop_adapter()
        self.wait_for_offline_adapter("codex.live")
        self.adapter = self.launch([*self.attached_command(), "--legacy-cli-bridge"])
        self.wait_for_online_adapter("codex.live", host_ref="thread-visible")

        root_id = self.send_from_claude("LEGACY_EXTERNAL_PROGRESS")
        progress = self.receive_for_claude()
        self.assertEqual(progress["reply_to"], root_id)
        self.assertEqual(progress["kind"], "progress")
        self.assertEqual(progress["text"], "CODEX_LEGACY_PROGRESS")
        time.sleep(0.3)

        connection = self.connection()
        try:
            detail = core.delivery_detail(
                connection,
                room="live",
                recipient="codex.live",
                message_id=root_id,
            )["delivery"]
            self.assertEqual(detail["state"], "acted")
            self.assertEqual(detail["attempts"], 1)
            self.assertEqual(
                [event["state"] for event in detail["events"]],
                ["queued", "claimed", "injected", "acted"],
            )
            self.assertIsNone(
                core.wait_for_message(
                    connection,
                    room="live",
                    recipient="claude.live",
                    wait=0.2,
                    poll=0.05,
                    visibility=30,
                )
            )
        finally:
            connection.close()
        self.assertIsNone(self.adapter.poll())

    def test_legacy_cli_final_reply_is_reconciled_at_turn_completion(self) -> None:
        self.stop_adapter()
        self.wait_for_offline_adapter("codex.live")
        self.adapter = self.launch([*self.attached_command(), "--legacy-cli-bridge"])
        self.wait_for_online_adapter("codex.live", host_ref="thread-visible")

        root_id = self.send_from_claude("LEGACY_EXTERNAL_FINAL")
        reply = self.receive_for_claude()
        self.assertEqual(reply["reply_to"], root_id)
        self.assertEqual(reply["kind"], "reply")
        self.assertEqual(reply["text"], "CODEX_LEGACY_FINAL")

        connection = self.connection()
        try:
            detail = core.delivery_detail(
                connection,
                room="live",
                recipient="codex.live",
                message_id=root_id,
            )["delivery"]
            self.assertEqual(detail["state"], "replied")
            self.assertEqual(detail["attempts"], 1)
            self.assertEqual(
                [event["state"] for event in detail["events"]],
                ["queued", "claimed", "injected", "replied"],
            )
        finally:
            connection.close()
        self.assertIsNone(self.adapter.poll())


if __name__ == "__main__":
    unittest.main()
