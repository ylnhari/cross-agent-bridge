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
        self.wait_for_member("codex.live")

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

    def wait_for_member(self, endpoint: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.adapter.poll() is not None:
                _, stderr = self.adapter.communicate(timeout=1)
                self.fail(f"adapter exited early: {stderr}")
            connection = self.connection()
            try:
                endpoints = {
                    member["endpoint"]
                    for member in core.members(connection, room="live")["members"]
                }
            finally:
                connection.close()
            if endpoint in endpoints:
                return
            time.sleep(0.05)
        self.fail(f"adapter did not join as {endpoint}")

    def wait_for_online_adapter(self, endpoint: str) -> None:
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
            if member is not None and member["adapter_online"]:
                return
            time.sleep(0.05)
        self.fail(f"adapter did not become live for {endpoint}")

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

    def receive_for_claude(self, wait: float = 10) -> dict:
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
            self.assertIsNotNone(delivery)
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
        self.wait_for_online_adapter("codex.live")
        recovered = self.wait_for_attempt(first_id, 2)
        self.assertEqual(recovered["attempts"], 2)

        second_id = self.send_from_claude("SECOND_CONVERSATION_WHILE_RUNNING")
        replies = [self.receive_for_claude() for _ in range(2)]
        by_parent = {item["reply_to"]: item["text"] for item in replies}
        self.assertEqual(by_parent[second_id], "CODEX_RECEIVED_STEER")
        self.assertEqual(by_parent[first_id], "CODEX_LONG_TASK_COMPLETE")
        self.assertIsNone(self.adapter.poll())


if __name__ == "__main__":
    unittest.main()
