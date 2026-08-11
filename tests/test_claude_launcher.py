from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from agent_chat import claude_launcher, core


class ClaudeLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "chat.sqlite3"
        with closing(core.connect(self.database, create=True)) as connection:
            core.create_room(connection, "project")
            self.bridge_id = core.bridge_info(connection, self.database)["id"]

    def arguments(self, *extra: str):
        return claude_launcher.parser().parse_args(
            [
                "--db",
                str(self.database),
                "--expect-bridge",
                self.bridge_id,
                "--room",
                "project",
                "--endpoint",
                "claude.live.1",
                "--cwd",
                str(self.root),
                *extra,
            ]
        )

    @mock.patch("agent_chat.claude_launcher.subprocess.run")
    def test_version_parser_uses_the_exact_stdout_version_line(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["claude-test", "--version"],
            returncode=0,
            stdout="2.1.227 (Claude Code)\n",
            stderr="dependency 99.99.99 warning\n",
        )
        self.assertEqual(
            claude_launcher._program_version("claude-test", "Claude Code"),
            (2, 1, 227),
        )

    @mock.patch("agent_chat.claude_launcher.subprocess.run")
    def test_version_parser_rejects_a_nonversion_stdout_prefix(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["claude-test", "--version"],
            returncode=0,
            stdout="dependency 99.99.99 warning\n2.1.227 (Claude Code)\n",
            stderr="",
        )
        with self.assertRaisesRegex(core.ChatError, "could not identify"):
            claude_launcher._program_version("claude-test", "Claude Code")

    @mock.patch("agent_chat.claude_launcher._program_version")
    @mock.patch("agent_chat.claude_launcher._resolve_program")
    def test_check_validates_without_starting_claude(
        self,
        resolve_program: mock.Mock,
        program_version: mock.Mock,
    ) -> None:
        resolve_program.return_value = "claude-test"
        program_version.return_value = (2, 1, 227)
        with mock.patch("agent_chat.claude_launcher.subprocess.call") as call:
            self.assertEqual(claude_launcher.run(self.arguments("--check")), 0)
        call.assert_not_called()

    @mock.patch("agent_chat.claude_launcher._program_version")
    @mock.patch("agent_chat.claude_launcher._resolve_program")
    def test_launch_generates_one_isolated_live_channel_config(
        self,
        resolve_program: mock.Mock,
        program_version: mock.Mock,
    ) -> None:
        resolve_program.return_value = "claude-test"
        program_version.return_value = (2, 1, 227)
        observed: dict[str, object] = {}

        def launch(command: list[str], *, cwd: str) -> int:
            config_path = Path(command[command.index("--mcp-config") + 1])
            observed["config_path"] = config_path
            observed["config"] = json.loads(config_path.read_text(encoding="utf-8"))
            observed["command"] = command
            observed["cwd"] = cwd
            return 7

        args = self.arguments(
            "--resume",
            "session-test",
            "--model",
            "sonnet",
            "--permission-mode",
            "plan",
            "--prompt",
            "Wait for bridge events.",
        )
        with mock.patch("agent_chat.claude_launcher.subprocess.call", side_effect=launch) as call:
            self.assertEqual(claude_launcher.run(args), 7)
        call.assert_called_once()
        command = observed["command"]
        self.assertIn("--dangerously-load-development-channels", command)
        self.assertIn("--resume", command)
        self.assertEqual(command[-1], "Wait for bridge events.")
        self.assertEqual(observed["cwd"], str(self.root.resolve()))
        config = observed["config"]
        servers = config["mcpServers"]
        self.assertEqual(len(servers), 1)
        server_name, server = next(iter(servers.items()))
        self.assertIn(f"server:{server_name}", command)
        allowed = command[command.index("--allowedTools") + 1]
        self.assertEqual(
            set(allowed.split(",")),
            {
                f"mcp__{server_name}__agent_chat_observe",
                f"mcp__{server_name}__agent_chat_progress",
                f"mcp__{server_name}__agent_chat_reply",
                f"mcp__{server_name}__agent_chat_send",
            },
        )
        self.assertEqual(server["command"], str(Path(sys.executable).resolve()))
        self.assertEqual(server["args"], ["-m", "agent_chat.claude_channel"])
        self.assertEqual(server["env"]["AGENT_CHAT_BRIDGE"], self.bridge_id)
        self.assertEqual(server["env"]["AGENT_CHAT_ENDPOINT"], "claude.live.1")
        self.assertFalse(observed["config_path"].exists())

    @mock.patch("agent_chat.claude_launcher._program_version")
    @mock.patch("agent_chat.claude_launcher._resolve_program")
    def test_live_duplicate_endpoint_fails_before_launch(
        self,
        resolve_program: mock.Mock,
        program_version: mock.Mock,
    ) -> None:
        with closing(core.connect(self.database)) as connection:
            core.join(
                connection,
                room="project",
                endpoint="claude.live.1",
                system="claude",
            )
            core.acquire_adapter_lease(
                connection,
                room="project",
                endpoint="claude.live.1",
                owner_token="existing-owner",
                adapter="claude.channel",
                ttl=30,
            )
        with self.assertRaisesRegex(core.ChatError, "already has a live"):
            claude_launcher.run(self.arguments())
        resolve_program.assert_not_called()
        program_version.assert_not_called()

    @mock.patch("agent_chat.claude_launcher._program_version")
    @mock.patch("agent_chat.claude_launcher._resolve_program")
    def test_old_claude_version_fails_closed(
        self,
        resolve_program: mock.Mock,
        program_version: mock.Mock,
    ) -> None:
        resolve_program.return_value = "claude-test"
        program_version.return_value = (2, 1, 79)
        with self.assertRaisesRegex(core.ChatError, "2.1.80 or newer"):
            claude_launcher.run(self.arguments())


if __name__ == "__main__":
    unittest.main()
