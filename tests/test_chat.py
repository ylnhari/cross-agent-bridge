from __future__ import annotations

import io
import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


CHAT_PATH = Path(__file__).resolve().parents[1] / "chat.py"
SPEC = importlib.util.spec_from_file_location("cross_agent_chat", CHAT_PATH)
assert SPEC is not None and SPEC.loader is not None
chat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chat)


class ChatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = chat.connect(Path(self.temp.name) / "chat.sqlite3")
        self.addCleanup(self.connection.close)

    def test_free_form_question_and_reply(self) -> None:
        question = chat.send(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
            text="Which interpretation fits the evidence?\nA, B, or something else?",
            reply_to=None,
            dedupe_key=None,
        )["message"]
        received = chat.receive_one(self.connection, room="project", recipient="claude")
        self.assertEqual(received["text"], question["text"])
        chat.acknowledge(
            self.connection,
            room="project",
            recipient="claude",
            message_id=question["id"],
        )
        answer = chat.send(
            self.connection,
            room="project",
            sender="claude",
            recipient="codex",
            text="Neither. Preserve both facts and explain the conflict.",
            reply_to=question["id"],
            dedupe_key=None,
        )["message"]
        self.assertEqual(answer["reply_to"], question["id"])
        self.assertEqual(
            chat.receive_one(self.connection, room="project", recipient="codex")["id"],
            answer["id"],
        )

    def test_unacked_message_is_redelivered(self) -> None:
        sent = chat.send(
            self.connection,
            room="project",
            sender="claude",
            recipient="codex",
            text="Please inspect this uncertainty.",
            reply_to=None,
            dedupe_key=None,
        )["message"]
        first = chat.receive_one(self.connection, room="project", recipient="codex")
        second = chat.receive_one(self.connection, room="project", recipient="codex")
        self.assertEqual(first["id"], sent["id"])
        self.assertEqual(second["id"], sent["id"])
        chat.acknowledge(
            self.connection,
            room="project",
            recipient="codex",
            message_id=sent["id"],
        )
        self.assertIsNone(
            chat.receive_one(self.connection, room="project", recipient="codex")
        )

    def test_optional_dedupe_key(self) -> None:
        first = chat.send(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
            text="Same retry-safe message",
            reply_to=None,
            dedupe_key="turn-7",
        )
        second = chat.send(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
            text="Same retry-safe message",
            reply_to=None,
            dedupe_key="turn-7",
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["message"]["id"], second["message"]["id"])

    def test_rooms_are_isolated(self) -> None:
        chat.send(
            self.connection,
            room="one",
            sender="claude",
            recipient="codex",
            text="Room one",
            reply_to=None,
            dedupe_key=None,
        )
        self.assertIsNone(chat.receive_one(self.connection, room="two", recipient="codex"))

    def test_empty_sync_sends_one_check_in_instead_of_waiting_forever(self) -> None:
        result = chat.ask_peer(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
        )
        self.assertEqual(result["status"], "check_in_sent")
        received = chat.receive_one(
            self.connection, room="project", recipient="claude"
        )
        self.assertEqual(received["id"], result["message"]["id"])
        self.assertIn("Are you waiting on me", received["text"])

    def test_unacknowledged_outgoing_message_surfaces_peer_pending(self) -> None:
        first = chat.ask_peer(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
        )
        second = chat.ask_peer(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
        )
        self.assertEqual(first["status"], "check_in_sent")
        self.assertEqual(second["status"], "peer_pending")
        self.assertEqual(second["delivery_state"], "queued_unacknowledged")
        self.assertEqual(second["peer_state"], "unknown_may_be_busy")
        self.assertEqual(second["pending_message"]["id"], first["message"]["id"])
        self.assertEqual(chat.status(self.connection, room="project")["messages"], 1)

    def test_acknowledged_check_in_without_answer_does_not_spam(self) -> None:
        first = chat.ask_peer(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
        )
        chat.acknowledge(
            self.connection,
            room="project",
            recipient="claude",
            message_id=first["message"]["id"],
        )
        second = chat.ask_peer(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
        )
        self.assertEqual(second["status"], "peer_reply_pending")
        self.assertEqual(second["delivery_state"], "acknowledged_without_reply")
        self.assertEqual(chat.status(self.connection, room="project")["messages"], 1)

    def test_peer_answer_allows_a_later_check_in(self) -> None:
        first = chat.ask_peer(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
        )
        chat.acknowledge(
            self.connection,
            room="project",
            recipient="claude",
            message_id=first["message"]["id"],
        )
        answer = chat.send(
            self.connection,
            room="project",
            sender="claude",
            recipient="codex",
            text="Continue with the bounded task.",
            reply_to=first["message"]["id"],
            dedupe_key=None,
        )["message"]
        chat.acknowledge(
            self.connection,
            room="project",
            recipient="codex",
            message_id=answer["id"],
        )
        second = chat.ask_peer(
            self.connection,
            room="project",
            sender="codex",
            recipient="claude",
        )
        self.assertEqual(second["status"], "check_in_sent")
        self.assertNotEqual(first["message"]["id"], second["message"]["id"])

    def test_cli_requires_explicit_database_and_room(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            chat.parser().parse_args(["status", "--room", "project"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            chat.parser().parse_args(
                ["--db", str(Path(self.temp.name) / "chat.sqlite3"), "status"]
            )

    def test_non_init_command_refuses_to_create_a_database(self) -> None:
        missing = Path(self.temp.name) / "missing.sqlite3"
        with self.assertRaises(chat.ChatError):
            chat.run(["--db", str(missing), "status", "--room", "project"])
        self.assertFalse(missing.exists())

    def test_cli_output_identifies_the_resolved_database(self) -> None:
        database = Path(self.temp.name) / "identified.sqlite3"
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                chat.run(
                    ["--db", str(database), "init", "--room", "project"]
                ),
                0,
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["bridge"]["database"], str(database.resolve()))
        self.assertRegex(payload["bridge"]["id"], r"^[0-9a-f]{32}$")

    def test_cli_sync_asks_once_then_surfaces_the_pending_peer(self) -> None:
        database = Path(self.temp.name) / "sync.sqlite3"
        with redirect_stdout(io.StringIO()):
            chat.run(["--db", str(database), "init", "--room", "project"])

        first_output = io.StringIO()
        with redirect_stdout(first_output):
            first_exit = chat.run(
                [
                    "--db",
                    str(database),
                    "sync",
                    "--room",
                    "project",
                    "--as",
                    "codex",
                    "--with",
                    "claude",
                    "--wait",
                    "0",
                ]
            )
        self.assertEqual(first_exit, 0)
        self.assertEqual(json.loads(first_output.getvalue())["status"], "check_in_sent")

        second_output = io.StringIO()
        with redirect_stdout(second_output):
            second_exit = chat.run(
                [
                    "--db",
                    str(database),
                    "sync",
                    "--room",
                    "project",
                    "--as",
                    "codex",
                    "--with",
                    "claude",
                    "--wait",
                    "0",
                ]
            )
        self.assertEqual(second_exit, 4)
        self.assertEqual(
            json.loads(second_output.getvalue())["status"], "peer_pending"
        )


if __name__ == "__main__":
    unittest.main()
