from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from agent_chat import core
from agent_chat import profile as bridge_profile


class BridgeProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_round_trip_uses_an_absolute_database_and_exact_identity(self) -> None:
        database = self.root / "chat.sqlite3"
        profile = self.root / "nested" / "bridge.json"
        written = bridge_profile.write(
            profile,
            database=database,
            bridge_id="bridge-test",
            room="project",
        )
        self.assertEqual(written, profile.resolve())
        value = bridge_profile.load(profile)
        self.assertEqual(value["database"], database.resolve())
        self.assertEqual(value["bridge_id"], "bridge-test")
        self.assertEqual(value["room"], "project")

    def test_profile_fills_missing_values_and_accepts_exact_matches(self) -> None:
        profile = self.root / "bridge.json"
        database = self.root / "profile.sqlite3"
        bridge_profile.write(
            profile,
            database=database,
            bridge_id="profile-bridge",
            room="profile-room",
        )
        args = argparse.Namespace(
            profile=profile,
            db=database,
            expect_bridge="profile-bridge",
            room=None,
        )
        bridge_profile.apply(args)
        self.assertEqual(args.db, database.resolve())
        self.assertEqual(args.expect_bridge, "profile-bridge")
        self.assertEqual(args.room, "profile-room")

    def test_profile_rejects_conflicting_connection_sources(self) -> None:
        profile = self.root / "bridge.json"
        bridge_profile.write(
            profile,
            database=self.root / "profile.sqlite3",
            bridge_id="profile-bridge",
            room="profile-room",
        )
        cases = (
            argparse.Namespace(
                profile=profile,
                db=self.root / "other.sqlite3",
                expect_bridge=None,
                room=None,
            ),
            argparse.Namespace(
                profile=profile,
                db=None,
                expect_bridge="other-bridge",
                room=None,
            ),
            argparse.Namespace(
                profile=profile,
                db=None,
                expect_bridge=None,
                room="other-room",
            ),
        )
        for args in cases:
            with self.subTest(args=args), self.assertRaisesRegex(core.ChatError, "conflicts"):
                bridge_profile.apply(args)

    def test_relative_database_and_unknown_fields_fail_closed(self) -> None:
        profile = self.root / "bridge.json"
        profile.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "database": "relative.sqlite3",
                    "bridge_id": "bridge-test",
                    "room": "project",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(core.ChatError, "absolute path"):
            bridge_profile.load(profile)
        profile.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "database": str((self.root / "chat.sqlite3").resolve()),
                    "bridge_id": "bridge-test",
                    "room": "project",
                    "typo": True,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(core.ChatError, "contain exactly"):
            bridge_profile.load(profile)


if __name__ == "__main__":
    unittest.main()
