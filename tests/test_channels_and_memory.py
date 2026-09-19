import tempfile
import unittest
import json
from pathlib import Path
import sqlite3

from channels.bluebubbles import BlueBubblesAdapter
from memory.threads import ThreadStore
from memory.identities import IdentityStore
from proactive import DailyInterruptLimit, LocalEventListener


class ChannelsAndMemoryTests(unittest.TestCase):
    def test_bluebubbles_normalizes_inbound_message(self):
        message = BlueBubblesAdapter.parse_webhook(
            {"event": "new-message", "data": {"chatGuid": "iMessage;+;chat", "handle": "+15551234567", "text": " kal ka schedule kya hai ", "isFromMe": False}}
        )
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.user_id, "imessage:+15551234567")
        self.assertEqual(message.text, "kal ka schedule kya hai")
        self.assertIsNone(BlueBubblesAdapter.parse_webhook({"event": "new-message", "data": {"isFromMe": True, "text": "echo"}}))

    def test_bluebubbles_accepts_serialized_event_data_and_chat_list(self):
        message = BlueBubblesAdapter.parse_webhook(
            {
                "type": "new-message",
                "data": json.dumps({"chats": [{"guid": "iMessage;+;chat"}], "handle": "+15551234567", "text": "hello"}),
            }
        )
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.thread_id, "iMessage;+;chat")

    def test_bluebubbles_send_uses_the_message_field_the_server_validates(self):
        sent = {}

        def opener_returning(capabilities):
            class Response:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *_exception):
                    return False

                def read(self):
                    return json.dumps({"data": capabilities}).encode()

            def opener(request, timeout=None):
                if request.data is not None:
                    sent["url"] = request.full_url
                    sent["body"] = json.loads(request.data)
                return Response()

            return opener

        adapter = BlueBubblesAdapter(base_url="https://server.example", password="not-a-real-password", opener=opener_returning({}))
        adapter.send(thread_id="any;-;+15551234567", text="on it")
        self.assertEqual(sent["body"]["chatGuid"], "any;-;+15551234567")
        self.assertEqual(sent["body"]["message"], "on it")
        # AppleScript sending is rejected outright without a tempGuid.
        self.assertTrue(sent["body"]["tempGuid"])
        self.assertEqual(sent["body"]["method"], "apple-script")
        self.assertIn("/api/v1/message/text?", sent["url"])

        capable = BlueBubblesAdapter(
            base_url="https://server.example",
            password="not-a-real-password",
            opener=opener_returning({"private_api": True, "helper_connected": True}),
        )
        capable.send(thread_id="any;-;+15551234567", text="on it")
        self.assertEqual(sent["body"]["method"], "private-api")

    def test_bluebubbles_answers_each_message_once_across_webhook_and_polling(self):
        adapter = BlueBubblesAdapter(base_url="https://server.example", password="not-a-real-password")
        adapter.send = lambda **_kwargs: None
        answered = []
        payload = {
            "event": "new-message",
            "data": {"guid": "MSG-1", "text": "hi", "isFromMe": False, "handle": {"address": "+15551234567"}, "chats": [{"guid": "any;-;+15551234567"}]},
        }
        adapter._answer(lambda message: answered.append(message.text) or "ok", payload)
        adapter._answer(lambda message: answered.append(message.text) or "ok", payload)
        self.assertEqual(answered, ["hi"])

    def test_bluebubbles_ignores_a_message_that_surfaces_far_too_late(self):
        import time

        adapter = BlueBubblesAdapter(base_url="https://server.example", password="not-a-real-password", max_age=300)
        adapter.send = lambda **_kwargs: None
        answered = []

        def payload(guid, age_seconds):
            return {
                "event": "new-message",
                "data": {
                    "guid": guid,
                    "text": "what are my upcoming events?",
                    "isFromMe": False,
                    "dateCreated": int((time.time() - age_seconds) * 1000),
                    "handle": {"address": "+15551234567"},
                    "chats": [{"guid": "any;-;+15551234567"}],
                },
            }

        adapter._answer(lambda message: answered.append("fresh") or "ok", payload("M-1", 5))
        adapter._answer(lambda message: answered.append("stale") or "ok", payload("M-2", 1400))
        self.assertEqual(answered, ["fresh"])

    def test_a_slow_answer_gets_a_holding_reply_and_a_quick_one_does_not(self):
        import time

        sent = []

        def adapter_for(ack_after):
            adapter = BlueBubblesAdapter(
                base_url="https://server.example", password="not-a-real-password", ack_after=ack_after
            )
            adapter.send = lambda *, thread_id, text: sent.append(text)
            return adapter

        def payload(guid):
            return {
                "event": "new-message",
                "data": {
                    "guid": guid,
                    "text": "check my mail",
                    "isFromMe": False,
                    "handle": {"address": "+15551234567"},
                    "chats": [{"guid": "any;-;+15551234567"}],
                },
            }

        slow = adapter_for(0.05)
        slow.ack_writer = lambda incoming: f"Checking {incoming.text.split()[-1]} now."
        slow._answer(lambda message: time.sleep(0.3) or "here it is", payload("M-slow"))
        self.assertEqual(sent, ["Checking mail now.", "here it is"])

        # A writer that fails or has nothing to say leaves the plain stand-in.
        for writer in (lambda _incoming: None, lambda _incoming: (_ for _ in ()).throw(RuntimeError("no"))):
            sent.clear()
            fallback = adapter_for(0.05)
            fallback.ack_writer = writer
            fallback._answer(lambda message: time.sleep(0.3) or "done", payload(f"M-{id(writer)}"))
            self.assertEqual(sent, ["One sec\u2026", "done"])

        sent.clear()
        quick = adapter_for(0.5)
        quick._answer(lambda message: "instant", payload("M-quick"))
        self.assertEqual(sent, ["instant"])

    def test_a_failure_still_gets_a_reply_rather_than_silence(self):
        sent = []
        adapter = BlueBubblesAdapter(
            base_url="https://server.example", password="not-a-real-password", ack_after=0
        )
        adapter.send = lambda *, thread_id, text: sent.append(text)

        def broken(_message):
            raise RuntimeError("Connection error.")

        adapter._answer(
            broken,
            {
                "event": "new-message",
                "data": {
                    "guid": "M-broken",
                    "text": "add this to my notion page",
                    "isFromMe": False,
                    "handle": {"address": "+15551234567"},
                    "chats": [{"guid": "any;-;+15551234567"}],
                },
            },
        )

        self.assertEqual(sent, [adapter.failure_text])

    def test_bluebubbles_ignores_tapbacks(self):
        self.assertIsNone(
            BlueBubblesAdapter.parse_webhook(
                {
                    "event": "new-message",
                    "data": {"text": 'Liked "hello"', "isFromMe": False, "associatedMessageGuid": "p:0/ABC", "handle": {"address": "+15551234567"}, "chats": [{"guid": "any;-;+1555"}]},
                }
            )
        )

    def test_bluebubbles_loads_local_server_config(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "config.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE config (name TEXT PRIMARY KEY, value TEXT)")
                connection.executemany("INSERT INTO config VALUES (?, ?)", [("socket_port", "1234"), ("password", "not-a-real-password")])
            import os
            old_value = os.environ.get("BLUEBUBBLES_CONFIG_DB")
            os.environ["BLUEBUBBLES_CONFIG_DB"] = str(database)
            try:
                adapter = BlueBubblesAdapter.from_local_server_config()
            finally:
                if old_value is None:
                    del os.environ["BLUEBUBBLES_CONFIG_DB"]
                else:
                    os.environ["BLUEBUBBLES_CONFIG_DB"] = old_value
            self.assertEqual(adapter.base_url, "http://localhost:1234")

    def test_the_cli_can_build_an_adapter_with_every_setting_it_passes(self):
        import os

        import cli

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "config.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE config (name TEXT PRIMARY KEY, value TEXT)")
                connection.executemany(
                    "INSERT INTO config VALUES (?, ?)",
                    [("socket_port", "1234"), ("password", "not-a-real-password")],
                )
            names = ("BLUEBUBBLES_CONFIG_DB", "BLUEBUBBLES_URL", "BLUEBUBBLES_PASSWORD")
            previous = {name: os.environ.get(name) for name in names}
            os.environ["BLUEBUBBLES_CONFIG_DB"] = str(database)
            os.environ.pop("BLUEBUBBLES_URL", None)
            os.environ.pop("BLUEBUBBLES_PASSWORD", None)
            try:
                # Every setting the CLI hands over has to survive the trip, or
                # the service dies on startup rather than in a test.
                adapter = cli._bluebubbles_adapter()
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
            self.assertEqual(adapter.base_url, "http://localhost:1234")
            self.assertEqual(adapter.poll_interval, 3.0)
            self.assertEqual(adapter.max_age, 300.0)
            self.assertEqual(adapter.ack_after, 4.0)
            self.assertTrue(adapter.ack_text)
            self.assertTrue(adapter.failure_text)

    def test_threads_include_required_product_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ThreadStore(Path(directory) / "state.sqlite3")
            thread = store.create(user_id="imessage:+1555", why_it_matters="Find a flight", next_check_at="2026-09-20T10:00:00+00:00")
            updated = store.update_status(thread.id, "waiting_approval")
            self.assertEqual(updated.status, "waiting_approval")
            self.assertEqual(store.pending_for("imessage:+1555")[0].why_it_matters, "Find a flight")

    def test_proactive_limit_is_three_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            limit = DailyInterruptLimit(Path(directory) / "state.sqlite3")
            self.assertEqual([limit.allow("imessage:+1555") for _ in range(4)], [True, True, True, False])

    def test_identity_map_and_event_candidate_are_local(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            identities = IdentityStore(database)
            identity = identities.remember(channel="imessage", sender="+1555", thread_id="iMessage;+;chat")
            self.assertEqual(identities.get(identity.user_id).thread_id, "iMessage;+;chat")
            delivered: list[tuple[str, str]] = []
            listener = LocalEventListener(limit=DailyInterruptLimit(database), deliver=lambda user, text: delivered.append((user, text)) or True)
            self.assertTrue(listener.dispatch({"user_id": identity.user_id, "text": "Your flight price changed."}))
            self.assertEqual(delivered, [("imessage:+1555", "Your flight price changed.")])
