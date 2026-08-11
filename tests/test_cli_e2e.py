from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CliE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "chat.sqlite3"
        self.env = os.environ.copy()
        existing = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + existing if existing else "")

    def command(self, *args: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "agent_chat",
            "--db",
            str(self.db),
            *args,
        ]

    def run_cli(
        self,
        *args: str,
        expected: int = 0,
        env: dict[str, str] | None = None,
    ) -> dict:
        result = subprocess.run(
            self.command(*args),
            cwd=ROOT,
            env=env or self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            expected,
            msg=f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        output = result.stdout if expected in (0, 3, 5) else result.stderr
        return json.loads(output)

    def initialize(self) -> str:
        result = self.run_cli("init", "--room", "project")
        for endpoint, system in (
            ("claude.desktop", "claude"),
            ("codex.app", "codex"),
            ("codex.cli.1", "codex"),
        ):
            self.run_cli(
                "join",
                "--room",
                "project",
                "--endpoint",
                endpoint,
                "--system",
                system,
            )
        return result["bridge"]["id"]

    def test_subprocess_group_conversation_and_independent_receipts(self) -> None:
        bridge_id = self.initialize()
        sent = self.run_cli(
            "--expect-bridge",
            bridge_id,
            "send",
            "--room",
            "project",
            "--from",
            "claude.desktop",
            "--broadcast",
            "--text",
            "Can both Codex sessions answer?",
        )
        self.assertEqual(sent["message"]["to"], ["codex.app", "codex.cli.1"])

        receipts: dict[str, str] = {}
        for endpoint in ("codex.app", "codex.cli.1"):
            received = self.run_cli(
                "receive",
                "--room",
                "project",
                "--as",
                endpoint,
                "--wait",
                "0",
            )
            self.assertEqual(received["message"]["id"], sent["message"]["id"])
            receipts[endpoint] = received["delivery"]["receipt"]

        self.run_cli(
            "ack",
            "--room",
            "project",
            "--as",
            "codex.app",
            "--id",
            str(sent["message"]["id"]),
            "--receipt",
            receipts["codex.app"],
        )
        status = self.run_cli("status", "--room", "project")
        self.assertEqual(status["unread"], {"codex.cli.1": 1})

    def test_long_poll_unblocks_when_another_process_sends(self) -> None:
        self.initialize()
        receiver = subprocess.Popen(
            self.command(
                "receive",
                "--room",
                "project",
                "--as",
                "codex.app",
                "--wait",
                "5",
                "--poll",
                "0.05",
            ),
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        time.sleep(0.2)
        sent = self.run_cli(
            "send",
            "--room",
            "project",
            "--from",
            "claude.desktop",
            "--to",
            "codex.app",
            "--text",
            "deliver to the waiting adapter",
        )
        stdout, stderr = receiver.communicate(timeout=10)
        self.assertEqual(receiver.returncode, 0, msg=stderr)
        received = json.loads(stdout)
        self.assertEqual(received["message"]["id"], sent["message"]["id"])

    def test_environment_pins_shorten_repeated_agent_commands(self) -> None:
        bridge_id = self.initialize()
        configured = dict(self.env)
        configured.update(
            {
                "AGENT_CHAT_DB": str(self.db),
                "AGENT_CHAT_ROOM": "project",
                "AGENT_CHAT_ENDPOINT": "claude.desktop",
                "AGENT_CHAT_BRIDGE": bridge_id,
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_chat",
                "send",
                "--to",
                "codex.app",
                "--text",
                "short command",
            ],
            cwd=ROOT,
            env=configured,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["message"]["from"], "claude.desktop")
        self.assertEqual(payload["bridge"]["id"], bridge_id)

    def test_cli_exposes_adapter_delivery_lifecycle(self) -> None:
        self.initialize()
        sent = self.run_cli(
            "send",
            "--room",
            "project",
            "--from",
            "claude.desktop",
            "--to",
            "codex.app",
            "--text",
            "lifecycle probe",
        )
        received = self.run_cli(
            "receive",
            "--room",
            "project",
            "--as",
            "codex.app",
            "--visibility",
            "30",
        )
        message_id = str(sent["message"]["id"])
        receipt = received["delivery"]["receipt"]
        marked = self.run_cli(
            "mark",
            "--room",
            "project",
            "--as",
            "codex.app",
            "--id",
            message_id,
            "--receipt",
            receipt,
            "--state",
            "injected",
            "--adapter",
            "codex.app",
            "--host-ref",
            "thread-probe",
        )
        self.assertEqual(marked["state"], "injected")
        self.run_cli(
            "renew",
            "--room",
            "project",
            "--as",
            "codex.app",
            "--id",
            message_id,
            "--receipt",
            receipt,
            "--visibility",
            "60",
        )
        detail = self.run_cli(
            "delivery",
            "--room",
            "project",
            "--as",
            "codex.app",
            "--id",
            message_id,
        )
        self.assertEqual(detail["delivery"]["state"], "injected")
        self.assertEqual(detail["delivery"]["host_ref"], "thread-probe")

    def test_wrong_bridge_id_fails_before_a_send(self) -> None:
        self.initialize()
        result = self.run_cli(
            "--expect-bridge",
            "0" * 32,
            "send",
            "--room",
            "project",
            "--from",
            "claude.desktop",
            "--to",
            "codex.app",
            "--text",
            "must not land",
            expected=2,
        )
        self.assertIn("identity mismatch", result["error"])
        self.assertEqual(self.run_cli("status", "--room", "project")["messages"], 0)

    def test_redirected_cli_json_preserves_unicode_on_windows(self) -> None:
        self.initialize()
        text = "em dash — café हिंदी 😀"
        sent = self.run_cli(
            "send",
            "--room",
            "project",
            "--from",
            "claude.desktop",
            "--to",
            "codex.app",
            "--text",
            text,
        )
        self.assertEqual(sent["message"]["text"], text)
        received = self.run_cli(
            "receive",
            "--room",
            "project",
            "--as",
            "codex.app",
            "--wait",
            "0",
        )
        self.assertEqual(received["message"]["text"], text)

        piped = subprocess.run(
            self.command(
                "send",
                "--room",
                "project",
                "--from",
                "claude.desktop",
                "--to",
                "codex.app",
                "--stdin",
            ),
            cwd=ROOT,
            env=self.env,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(piped.returncode, 0, msg=piped.stderr)
        self.assertEqual(json.loads(piped.stdout)["message"]["text"], text)

    def test_non_init_command_does_not_create_typo_database(self) -> None:
        result = self.run_cli("doctor", expected=2)
        self.assertIn("does not exist", result["error"])
        self.assertFalse(self.db.exists())

    def test_argument_errors_are_single_json_objects(self) -> None:
        result = subprocess.run(
            self.command("receive", "--room", "project", "--as", "codex.app", "--wait", "nope"),
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        payload = json.loads(result.stderr)
        self.assertEqual(payload["status"], "error")
        self.assertIn("argument error", payload["error"])
        self.assertNotIn("usage:", result.stderr)

    def test_concurrent_init_processes_share_one_complete_database(self) -> None:
        def initialize() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                self.command("init", "--room", "project"),
                cwd=ROOT,
                env=self.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                check=False,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: initialize(), range(8)))
        self.assertTrue(
            all(item.returncode == 0 for item in results),
            msg="\n".join(item.stderr for item in results if item.stderr),
        )
        bridge_ids = {json.loads(item.stdout)["bridge"]["id"] for item in results}
        self.assertEqual(len(bridge_ids), 1)
        self.assertEqual(self.run_cli("doctor")["integrity"], "ok")

    def test_init_revalidates_schema_after_a_competing_creator_wins(self) -> None:
        script = """
from pathlib import Path
from unittest import mock
from agent_chat import core

path = Path(__import__('sys').argv[1])
def competing_creator(connection):
    connection.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
        (str(core.SCHEMA_VERSION),),
    )
    return False

with mock.patch.object(core, '_create_schema', competing_creator):
    try:
        core.connect(path, create=True)
    except core.ChatError as error:
        print(error)
    else:
        raise SystemExit('incomplete competing schema was accepted')
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.db)],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("incomplete schema", result.stdout)

    def test_pinned_bridge_prevents_accidental_new_init(self) -> None:
        configured = dict(self.env)
        configured["AGENT_CHAT_BRIDGE"] = "0" * 32
        result = self.run_cli(
            "init",
            "--room",
            "project",
            expected=2,
            env=configured,
        )
        self.assertIn("expected bridge ID is pinned", result["error"])
        self.assertFalse(self.db.exists())

    def test_init_refuses_to_modify_foreign_sqlite_file(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE keep_me(value TEXT)")
        connection.commit()
        connection.close()
        result = self.run_cli("init", "--room", "project", expected=2)
        self.assertIn("not an Agent Chat", result["error"])
        check = sqlite3.connect(self.db)
        tables = {
            row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        check.close()
        self.assertEqual(tables, {"keep_me"})

    def test_concurrent_cli_senders_are_lossless(self) -> None:
        self.initialize()

        def send(index: int) -> dict:
            return self.run_cli(
                "send",
                "--room",
                "project",
                "--from",
                "codex.app",
                "--to",
                "claude.desktop",
                "--key",
                f"process-{index}",
                "--text",
                f"parallel {index}",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            messages = list(pool.map(send, range(16)))
        ids = [item["message"]["id"] for item in messages]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.run_cli("status", "--room", "project")["messages"], 16)


if __name__ == "__main__":
    unittest.main()
