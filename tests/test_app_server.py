from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_chat.app_server import AppServerClient  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
