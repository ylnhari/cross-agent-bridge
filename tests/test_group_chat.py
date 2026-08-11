from __future__ import annotations

import concurrent.futures
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_chat import core  # noqa: E402


class GroupChatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "chat.sqlite3"
        self.connection = core.connect(self.path, create=True)
        self.addCleanup(self.connection.close)
        core.create_room(self.connection, "project")
        self.join("orchestrator.desktop", "claude")
        self.join("executor.app", "codex")
        self.join("executor.cli.1", "codex")
        self.join("reviewer.local", "other")

    def join(self, endpoint: str, system: str) -> None:
        core.join(
            self.connection,
            room="project",
            endpoint=endpoint,
            system=system,
        )

    def broadcast(self, text: str = "hello everyone") -> dict:
        return core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            text=text,
            broadcast=True,
        )["message"]

    def receive(self, endpoint: str, visibility: float = 300) -> dict | None:
        return core.receive_one(
            self.connection,
            room="project",
            recipient=endpoint,
            visibility=visibility,
        )

    def test_broadcast_fans_out_to_every_other_session(self) -> None:
        message = self.broadcast()
        self.assertEqual(
            message["to"],
            ["executor.app", "executor.cli.1", "reviewer.local"],
        )
        deliveries = {
            endpoint: self.receive(endpoint)
            for endpoint in ("executor.app", "executor.cli.1", "reviewer.local")
        }
        self.assertEqual({item["message"]["id"] for item in deliveries.values()}, {1})
        self.assertEqual(
            {item["delivery"]["recipient"] for item in deliveries.values()},
            set(deliveries),
        )

        first = deliveries["executor.app"]
        core.acknowledge(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=first["delivery"]["receipt"],
        )
        self.assertEqual(
            core.status(self.connection, room="project")["unread"],
            {"executor.cli.1": 1, "reviewer.local": 1},
        )

    def test_status_distinguishes_visible_from_claimed_without_presence_claim(self) -> None:
        self.broadcast()
        self.receive("executor.app", visibility=30)
        result = core.status(self.connection, room="project")
        self.assertEqual(result["claimed"], {"executor.app": 1})
        self.assertEqual(
            result["visible"],
            {"executor.cli.1": 1, "reviewer.local": 1},
        )

    def test_direct_message_is_visible_only_to_sender_and_recipients(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="private coordination",
        )["message"]
        self.assertIsNotNone(self.receive("executor.app"))
        self.assertIsNone(self.receive("executor.cli.1"))
        self.assertEqual(
            core.history(
                self.connection,
                room="project",
                limit=20,
                viewer="executor.cli.1",
            )["messages"],
            [],
        )
        self.assertEqual(
            core.history(
                self.connection,
                room="project",
                limit=20,
                viewer="orchestrator.desktop",
            )["messages"][0]["id"],
            message["id"],
        )

    def test_independent_conversations_between_same_sessions_do_not_mix(self) -> None:
        first = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="first topic",
        )["message"]
        second = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="second topic",
        )["message"]
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])

        reply = core.send(
            self.connection,
            room="project",
            sender="executor.app",
            recipients=["orchestrator.desktop"],
            text="answer to first",
            reply_to=first["id"],
        )["message"]
        self.assertEqual(reply["conversation_id"], first["conversation_id"])

        first_history = core.history(
            self.connection,
            room="project",
            viewer="orchestrator.desktop",
            conversation_id=first["conversation_id"],
            limit=20,
        )
        second_history = core.history(
            self.connection,
            room="project",
            viewer="orchestrator.desktop",
            conversation_id=second["conversation_id"],
            limit=20,
        )
        self.assertEqual(
            [message["text"] for message in first_history["messages"]],
            ["first topic", "answer to first"],
        )
        self.assertEqual(
            [message["text"] for message in second_history["messages"]],
            ["second topic"],
        )

    def test_membership_is_snapshotted_at_send_time(self) -> None:
        core.leave(self.connection, room="project", endpoint="reviewer.local")
        first = self.broadcast("before rejoin")
        core.join(
            self.connection,
            room="project",
            endpoint="reviewer.local",
            system="other",
        )
        self.assertIsNone(self.receive("reviewer.local"))
        second = self.broadcast("after rejoin")
        delivery = self.receive("reviewer.local")
        self.assertNotIn("reviewer.local", first["to"])
        self.assertIn("reviewer.local", second["to"])
        self.assertEqual(delivery["message"]["id"], second["id"])

    def test_left_endpoint_can_drain_prior_but_not_receive_new_deliveries(self) -> None:
        prior = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["reviewer.local"],
            text="queued before leaving",
        )["message"]
        core.leave(self.connection, room="project", endpoint="reviewer.local")
        later = self.broadcast("sent after leaving")
        delivery = self.receive("reviewer.local")
        self.assertEqual(delivery["message"]["id"], prior["id"])
        core.acknowledge(
            self.connection,
            room="project",
            recipient="reviewer.local",
            message_id=prior["id"],
            receipt=delivery["delivery"]["receipt"],
        )
        self.assertIsNone(self.receive("reviewer.local"))
        self.assertNotIn("reviewer.local", later["to"])

    def test_dedupe_retry_is_idempotent_but_conflicts_fail(self) -> None:
        kwargs = {
            "room": "project",
            "sender": "orchestrator.desktop",
            "recipients": ["executor.app", "executor.cli.1"],
            "text": "retry-safe",
            "dedupe_key": "client-turn-7",
        }
        first = core.send(self.connection, **kwargs)
        second = core.send(self.connection, **kwargs)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["message"]["id"], second["message"]["id"])

        for changed in (
            {**kwargs, "text": "changed"},
            {**kwargs, "recipients": ["reviewer.local"]},
        ):
            with self.assertRaisesRegex(core.ChatError, "already used"):
                core.send(self.connection, **changed)

    def test_broadcast_retry_survives_membership_change(self) -> None:
        first = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            text="one logical broadcast",
            broadcast=True,
            dedupe_key="broadcast-1",
        )
        core.leave(self.connection, room="project", endpoint="reviewer.local")
        second = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            text="one logical broadcast",
            broadcast=True,
            dedupe_key="broadcast-1",
        )
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["message"], second["message"])

    def test_claim_prevents_duplicate_processing_and_release_redelivers(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="claim me",
        )["message"]
        first = self.receive("executor.app", visibility=30)
        self.assertIsNone(self.receive("executor.app", visibility=30))
        core.release(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=first["delivery"]["receipt"],
        )
        second = self.receive("executor.app", visibility=30)
        self.assertEqual(second["delivery"]["attempt"], 2)
        self.assertNotEqual(first["delivery"]["receipt"], second["delivery"]["receipt"])
        with self.assertRaisesRegex(core.ChatError, "receipt"):
            core.acknowledge(
                self.connection,
                room="project",
                recipient="executor.app",
                message_id=message["id"],
                receipt=first["delivery"]["receipt"],
            )
        core.acknowledge(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=second["delivery"]["receipt"],
        )
        self.assertIsNone(self.receive("executor.app"))

    def test_release_rolls_back_if_its_event_cannot_be_recorded(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="keep claim and audit event atomic",
        )["message"]
        delivery = self.receive("executor.app", visibility=30)
        receipt = delivery["delivery"]["receipt"]

        with mock.patch.object(
            core,
            "_record_delivery_event",
            side_effect=sqlite3.OperationalError("injected event failure"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "event failure"):
                core.release(
                    self.connection,
                    room="project",
                    recipient="executor.app",
                    message_id=message["id"],
                    receipt=receipt,
                )

        detail = core.delivery_detail(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
        )["delivery"]
        self.assertEqual(detail["state"], "claimed")
        core.release(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=receipt,
        )

    def test_delivery_lifecycle_tracks_injection_observation_action_and_reply(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="please investigate",
        )["message"]
        queued = core.delivery_detail(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
        )
        self.assertEqual(queued["delivery"]["state"], "queued")

        delivery = self.receive("executor.app", visibility=30)
        receipt = delivery["delivery"]["receipt"]
        core.mark_delivery(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=receipt,
            state="injected",
            adapter="codex.app",
            host_ref="thread-test",
        )
        renewed = core.renew_claim(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=receipt,
            visibility=60,
        )
        self.assertEqual(renewed["status"], "renewed")
        observed = core.acknowledge(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=receipt,
        )
        self.assertEqual(observed["state"], "observed")
        core.mark_delivery(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=receipt,
            state="acted",
        )
        core.send(
            self.connection,
            room="project",
            sender="executor.app",
            recipients=["orchestrator.desktop"],
            text="investigation complete",
            reply_to=message["id"],
        )

        detail = core.delivery_detail(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
        )["delivery"]
        self.assertEqual(detail["state"], "replied")
        self.assertEqual(detail["adapter"], "codex.app")
        self.assertEqual(detail["host_ref"], "thread-test")
        self.assertTrue(detail["injected_at"])
        self.assertTrue(detail["observed_at"])
        self.assertTrue(detail["acted_at"])
        self.assertTrue(detail["replied_at"])
        self.assertEqual(
            [event["state"] for event in detail["events"]],
            ["queued", "claimed", "injected", "observed", "acted", "replied"],
        )
        self.assertEqual(
            core.status(self.connection, room="project")["delivery_states"]["executor.app"],
            {"replied": 1},
        )

    def test_progress_is_nonterminal_and_final_reply_completes_delivery(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="long request",
        )["message"]
        delivery = self.receive("executor.app", visibility=30)
        core.acknowledge(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=delivery["delivery"]["receipt"],
        )
        progress = core.send(
            self.connection,
            room="project",
            sender="executor.app",
            recipients=["orchestrator.desktop"],
            text="halfway",
            reply_to=message["id"],
            kind="progress",
        )["message"]
        detail = core.delivery_detail(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
        )["delivery"]
        self.assertEqual(progress["kind"], "progress")
        self.assertEqual(detail["state"], "acted")
        self.assertIsNone(detail["replied_at"])

        final = core.send(
            self.connection,
            room="project",
            sender="executor.app",
            recipients=["orchestrator.desktop"],
            text="done",
            reply_to=message["id"],
            kind="reply",
        )["message"]
        detail = core.delivery_detail(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
        )["delivery"]
        self.assertEqual(final["kind"], "reply")
        self.assertEqual(detail["state"], "replied")
        self.assertTrue(detail["replied_at"])

        with self.assertRaisesRegex(core.ChatError, "final reply was already sent"):
            core.send(
                self.connection,
                room="project",
                sender="executor.app",
                recipients=["orchestrator.desktop"],
                text="different second answer",
                reply_to=message["id"],
                dedupe_key="second-final",
                kind="reply",
            )

        with self.assertRaisesRegex(core.ChatError, "after a final reply"):
            core.send(
                self.connection,
                room="project",
                sender="executor.app",
                recipients=["orchestrator.desktop"],
                text="late progress",
                reply_to=message["id"],
                kind="progress",
            )
        conversation = core.history(
            self.connection,
            room="project",
            viewer="orchestrator.desktop",
            conversation_id=message["conversation_id"],
            limit=20,
        )["messages"]
        self.assertNotIn("late progress", [item["text"] for item in conversation])

    def test_response_to_inbound_message_must_reach_original_sender(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="question",
        )["message"]
        with self.assertRaisesRegex(core.ChatError, "original sender"):
            core.send(
                self.connection,
                room="project",
                sender="executor.app",
                recipients=["reviewer.local"],
                text="misrouted answer",
                reply_to=message["id"],
                kind="reply",
            )
        detail = core.delivery_detail(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
        )["delivery"]
        self.assertEqual(detail["state"], "queued")

    def test_concurrent_final_replies_commit_exactly_once(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="one final answer only",
        )["message"]
        barrier = threading.Barrier(2)

        def reply(index: int) -> str:
            connection = core.connect(self.path)
            try:
                barrier.wait(timeout=5)
                try:
                    core.send(
                        connection,
                        room="project",
                        sender="executor.app",
                        recipients=["orchestrator.desktop"],
                        text=f"answer {index}",
                        reply_to=message["id"],
                        dedupe_key=f"parallel-final-{index}",
                        kind="reply",
                    )
                except core.ChatError as exc:
                    return str(exc)
                return "sent"
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reply, range(2)))

        self.assertEqual(outcomes.count("sent"), 1)
        self.assertEqual(sum("final reply was already sent" in outcome for outcome in outcomes), 1)
        conversation = core.history(
            self.connection,
            room="project",
            viewer="orchestrator.desktop",
            conversation_id=message["conversation_id"],
            limit=20,
        )["messages"]
        self.assertEqual(sum(item["kind"] == "reply" for item in conversation), 1)

    def test_replied_state_requires_an_actual_reply_message(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="answer me",
        )["message"]
        delivery = self.receive("executor.app", visibility=30)
        with self.assertRaisesRegex(core.ChatError, "injected, observed, or acted"):
            core.mark_delivery(
                self.connection,
                room="project",
                recipient="executor.app",
                message_id=message["id"],
                receipt=delivery["delivery"]["receipt"],
                state="replied",
            )

    def test_adapter_lease_is_exclusive_and_recovers_incomplete_work(self) -> None:
        acquired = core.acquire_adapter_lease(
            self.connection,
            room="project",
            endpoint="executor.app",
            owner_token="owner-one",
            adapter="codex.appserver",
            host_ref="thread-one",
            ttl=30,
        )
        self.assertEqual(acquired["recovered"], 0)
        member = next(
            item
            for item in core.members(self.connection, room="project")["members"]
            if item["endpoint"] == "executor.app"
        )
        self.assertTrue(member["adapter_online"])
        with self.assertRaisesRegex(core.ChatError, "already has a live"):
            core.acquire_adapter_lease(
                self.connection,
                room="project",
                endpoint="executor.app",
                owner_token="owner-two",
                adapter="codex.appserver",
                ttl=30,
            )

        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="recover after crash",
        )["message"]
        delivery = self.receive("executor.app", visibility=30)
        core.mark_delivery(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=delivery["delivery"]["receipt"],
            state="injected",
            adapter="codex.appserver",
            host_ref="thread-one",
        )
        core.acknowledge(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=delivery["delivery"]["receipt"],
        )
        core.mark_delivery(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=delivery["delivery"]["receipt"],
            state="acted",
        )
        self.connection.execute(
            "UPDATE adapter_leases SET lease_until=? WHERE room=? AND endpoint=?",
            (core.stamp(-1), "project", "executor.app"),
        )
        takeover = core.acquire_adapter_lease(
            self.connection,
            room="project",
            endpoint="executor.app",
            owner_token="owner-two",
            adapter="codex.appserver",
            host_ref="thread-one",
            ttl=30,
        )
        self.assertEqual(takeover["recovered"], 1)
        recovered = self.receive("executor.app", visibility=30)
        self.assertEqual(recovered["message"]["id"], message["id"])
        self.assertEqual(recovered["delivery"]["attempt"], 2)

        released = core.release_adapter_lease(
            self.connection,
            room="project",
            endpoint="executor.app",
            owner_token="owner-two",
        )
        self.assertFalse(released["already_released"])

    def test_adapter_takeover_immediately_requeues_claimed_and_injected_work(self) -> None:
        core.acquire_adapter_lease(
            self.connection,
            room="project",
            endpoint="executor.app",
            owner_token="owner-one",
            adapter="codex.appserver",
            ttl=30,
        )
        first = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="claimed before crash",
        )["message"]
        first_delivery = self.receive("executor.app", visibility=300)

        second = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="injected before crash",
        )["message"]
        second_delivery = self.receive("executor.app", visibility=300)
        core.mark_delivery(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=second["id"],
            receipt=second_delivery["delivery"]["receipt"],
            state="injected",
            adapter="codex.appserver",
        )

        self.connection.execute(
            "UPDATE adapter_leases SET lease_until=? WHERE room=? AND endpoint=?",
            (core.stamp(-1), "project", "executor.app"),
        )
        takeover = core.acquire_adapter_lease(
            self.connection,
            room="project",
            endpoint="executor.app",
            owner_token="owner-two",
            adapter="codex.appserver",
            ttl=30,
        )
        self.assertEqual(takeover["recovered"], 2)

        redelivered = [self.receive("executor.app", visibility=30) for _ in range(2)]
        self.assertEqual(
            [item["message"]["id"] for item in redelivered],
            [first["id"], second["id"]],
        )
        self.assertEqual(
            [item["delivery"]["attempt"] for item in redelivered],
            [2, 2],
        )
        self.assertNotEqual(
            redelivered[0]["delivery"]["receipt"],
            first_delivery["delivery"]["receipt"],
        )

    def test_adapter_takeover_does_not_requeue_an_observed_reply(self) -> None:
        root = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="question",
        )["message"]
        reply = core.send(
            self.connection,
            room="project",
            sender="executor.app",
            recipients=["orchestrator.desktop"],
            text="answer",
            reply_to=root["id"],
        )["message"]
        core.acquire_adapter_lease(
            self.connection,
            room="project",
            endpoint="orchestrator.desktop",
            owner_token="owner-one",
            adapter="claude.channel",
            ttl=30,
        )
        delivery = self.receive("orchestrator.desktop", visibility=30)
        self.assertEqual(delivery["message"]["id"], reply["id"])
        core.acknowledge(
            self.connection,
            room="project",
            recipient="orchestrator.desktop",
            message_id=reply["id"],
            receipt=delivery["delivery"]["receipt"],
        )
        self.connection.execute(
            "UPDATE adapter_leases SET lease_until=? WHERE room=? AND endpoint=?",
            (core.stamp(-1), "project", "orchestrator.desktop"),
        )

        takeover = core.acquire_adapter_lease(
            self.connection,
            room="project",
            endpoint="orchestrator.desktop",
            owner_token="owner-two",
            adapter="claude.channel",
            ttl=30,
        )

        self.assertEqual(takeover["recovered"], 0)
        self.assertIsNone(self.receive("orchestrator.desktop", visibility=30))
        detail = core.delivery_detail(
            self.connection,
            room="project",
            recipient="orchestrator.desktop",
            message_id=reply["id"],
        )["delivery"]
        self.assertEqual(detail["state"], "observed")

    def test_ack_idempotency_requires_the_original_receipt(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="observe once",
        )["message"]
        delivery = self.receive("executor.app")
        receipt = delivery["delivery"]["receipt"]
        core.acknowledge(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=receipt,
        )
        repeated = core.acknowledge(
            self.connection,
            room="project",
            recipient="executor.app",
            message_id=message["id"],
            receipt=receipt,
        )
        self.assertTrue(repeated["already_acked"])
        with self.assertRaisesRegex(core.ChatError, "receipt"):
            core.acknowledge(
                self.connection,
                room="project",
                recipient="executor.app",
                message_id=message["id"],
                receipt="wrong",
            )

    def test_expired_claim_redelivers_after_restart(self) -> None:
        self.connection.close()
        self.addCleanup(lambda: None)
        first_connection = core.connect(self.path)
        message = core.send(
            first_connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="survive a crash",
        )["message"]
        first = core.receive_one(
            first_connection,
            room="project",
            recipient="executor.app",
            visibility=0.05,
        )
        first_connection.close()
        time.sleep(0.2)
        restarted = core.connect(self.path)
        self.addCleanup(restarted.close)
        second = core.receive_one(
            restarted,
            room="project",
            recipient="executor.app",
            visibility=30,
        )
        self.assertEqual(second["message"]["id"], message["id"])
        self.assertEqual(second["delivery"]["attempt"], 2)
        self.assertNotEqual(first["delivery"]["receipt"], second["delivery"]["receipt"])

    def test_zero_visibility_is_rejected(self) -> None:
        self.broadcast("needs a usable claim")
        with self.assertRaisesRegex(core.ChatError, "greater than 0"):
            self.receive("executor.app", visibility=0)

    def test_wrong_endpoint_or_receipt_cannot_ack(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="bounded delivery",
        )["message"]
        delivery = self.receive("executor.app")
        with self.assertRaisesRegex(core.ChatError, "not delivered"):
            core.acknowledge(
                self.connection,
                room="project",
                recipient="reviewer.local",
                message_id=message["id"],
                receipt=delivery["delivery"]["receipt"],
            )
        with self.assertRaisesRegex(core.ChatError, "receipt"):
            core.acknowledge(
                self.connection,
                room="project",
                recipient="executor.app",
                message_id=message["id"],
                receipt="wrong",
            )

    def test_reply_must_be_visible_to_sender(self) -> None:
        message = core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="only one executor sees this",
        )["message"]
        with self.assertRaisesRegex(core.ChatError, "not visible"):
            core.send(
                self.connection,
                room="project",
                sender="reviewer.local",
                recipients=["orchestrator.desktop"],
                text="I should not know this",
                reply_to=message["id"],
            )

    def test_message_size_is_measured_in_utf8_bytes(self) -> None:
        core.send(
            self.connection,
            room="project",
            sender="orchestrator.desktop",
            recipients=["executor.app"],
            text="a" * core.MAX_TEXT_BYTES,
        )
        with self.assertRaisesRegex(core.ChatError, "exceeds"):
            core.send(
                self.connection,
                room="project",
                sender="orchestrator.desktop",
                recipients=["executor.app"],
                text="é" * (core.MAX_TEXT_BYTES // 2 + 1),
            )

    def test_bridge_identity_is_stable_and_can_be_pinned(self) -> None:
        identity = core.bridge_info(self.connection, self.path)["id"]
        self.connection.close()
        reopened = core.connect(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(core.bridge_info(reopened, self.path)["id"], identity)
        core.require_bridge(reopened, identity)
        with self.assertRaisesRegex(core.ChatError, "mismatch"):
            core.require_bridge(reopened, "0" * 32)

    def test_foreign_sqlite_database_is_rejected_without_added_tables(self) -> None:
        foreign = Path(self.temp.name) / "foreign.sqlite3"
        connection = sqlite3.connect(foreign)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(core.ChatError, "not an Agent Chat"):
            core.connect(foreign, create=True)
        check = sqlite3.connect(foreign)
        tables = {
            row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        check.close()
        self.assertEqual(tables, {"unrelated"})

    def test_connect_retries_a_transient_wal_configuration_lock(self) -> None:
        path = Path(self.temp.name) / "wal-race.sqlite3"
        original = core._enable_wal_once
        attempts = 0

        def transient_lock(connection: sqlite3.Connection) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(connection)

        with mock.patch.object(core, "_enable_wal_once", side_effect=transient_lock):
            connection = core.connect(path, create=True)
        self.addCleanup(connection.close)

        self.assertEqual(attempts, 2)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_concurrent_senders_persist_every_message_once(self) -> None:
        def worker(index: int) -> int:
            connection = core.connect(self.path)
            try:
                return core.send(
                    connection,
                    room="project",
                    sender="executor.app",
                    recipients=["orchestrator.desktop"],
                    text=f"message {index}",
                    dedupe_key=f"concurrent-{index}",
                )["message"]["id"]
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(worker, range(32)))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(core.status(self.connection, room="project")["messages"], 32)

    def test_send_serializes_sender_membership_with_concurrent_leave(self) -> None:
        leaver = core.connect(self.path)
        self.addCleanup(leaver.close)
        leaver.execute("BEGIN IMMEDIATE")
        leaver.execute(
            "UPDATE memberships SET left_at=? WHERE room=? AND endpoint=?",
            (core.stamp(), "project", "executor.app"),
        )

        def send_after_leave_started() -> str:
            connection = core.connect(self.path)
            try:
                try:
                    core.send(
                        connection,
                        room="project",
                        sender="executor.app",
                        recipients=["orchestrator.desktop"],
                        text="must serialize after leave",
                    )
                except core.ChatError as exc:
                    return str(exc)
                return "sent"
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send_after_leave_started)
            time.sleep(0.05)
            self.assertFalse(future.done())
            leaver.commit()
            result = future.result(timeout=5)

        self.assertIn("not an active member", result)
        self.assertEqual(core.status(self.connection, room="project")["messages"], 0)

    def test_concurrent_receivers_create_only_one_active_claim(self) -> None:
        self.broadcast("race")

        def worker() -> dict | None:
            connection = core.connect(self.path)
            try:
                return core.receive_one(
                    connection,
                    room="project",
                    recipient="executor.app",
                    visibility=30,
                )
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)


if __name__ == "__main__":
    unittest.main()
