from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_chat.app_server import AppServerClient, AppServerError  # noqa: E402


class AppServerClientTest(unittest.TestCase):
    def test_unicode_is_preserved_over_an_ascii_jsonl_wire(self) -> None:
        async def scenario() -> dict:
            client = AppServerClient([sys.executable, str(ROOT / "tests" / "fake_app_server.py")])
            await client.start()
            try:
                await client.initialize(experimental=True)
                return await client.request(
                    "test/echo",
                    {"text": "em dash — café हिंदी 😀"},
                )
            finally:
                await client.close()

        result = asyncio.run(scenario())
        self.assertEqual(result["text"], "em dash — café हिंदी 😀")
        self.assertTrue(result["wire_ascii"])

    def test_non_integer_response_id_fails_closed(self) -> None:
        script = (
            "import json,sys; json.loads(sys.stdin.readline()); "
            "print(json.dumps({'id':'1','result':{}}), flush=True)"
        )

        async def scenario() -> None:
            client = AppServerClient([sys.executable, "-c", script])
            await client.start()
            try:
                await client.request("test", {})
            finally:
                await client.close()

        with self.assertRaisesRegex(AppServerError, "response id must be an integer"):
            asyncio.run(scenario())

    def test_oversized_frame_fails_closed(self) -> None:
        script = (
            "import json,sys; json.loads(sys.stdin.readline()); "
            "sys.stdout.write(json.dumps({'id':1,'result':{'value':'x'*256}})+'\\n'); "
            "sys.stdout.flush()"
        )

        async def scenario() -> None:
            client = AppServerClient([sys.executable, "-c", script])
            await client.start()
            try:
                await client.request("test", {})
            finally:
                await client.close()

        with mock.patch("agent_chat.app_server.MAX_JSONL_BYTES", 128):
            with self.assertRaisesRegex(AppServerError, "frame exceeds 128 bytes"):
                asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
