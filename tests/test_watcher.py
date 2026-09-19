import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memory.watches import WatchStore
from tasks.watcher import Watcher, identifiers


class Session:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def execute(self, slug, arguments=None):
        self.calls.append((slug, arguments))
        payload = self.pages.pop(0) if self.pages else {}
        return SimpleNamespace(data=payload)


def mail(*ids):
    return {"messages": [{"messageId": item, "subject": f"subject {item}"} for item in ids]}


class WatcherTests(unittest.TestCase):
    def build(self, session, *, allow=True):
        directory = tempfile.mkdtemp()
        store = WatchStore(Path(directory) / "state.sqlite3")
        sent = []
        watcher = Watcher(
            store=store,
            session_for=lambda _user: session,
            allow=lambda _user: allow,
            deliver=lambda user, text: sent.append((user, text)) or True,
            describe=lambda what, found: f"about {what}",
        )
        return store, watcher, sent

    def test_identifiers_are_found_wherever_an_app_buries_them(self):
        found = identifiers({"data": {"messages": [{"messageId": "a"}, {"id": "b"}], "next": {"thread_id": "c"}}})

        self.assertEqual(set(found), {"a", "b", "c"})

    def test_what_is_already_there_is_not_announced(self):
        session = Session(mail("old-1", "old-2"), mail("old-1", "old-2"))
        store, watcher, sent = self.build(session)
        store.create(user_id="imessage:+1555", what="mail from lossfunk", tool_slug="GMAIL_FETCH_EMAILS",
                     arguments={"query": "from:lossfunk"}, interval_seconds=60)

        self.assertEqual(watcher.sweep(), 0)  # first run only primes
        self.assertEqual(sent, [])

    def test_a_new_arrival_is_written_up_and_delivered_once(self):
        session = Session(mail("old-1"), mail("old-1", "new-1"), mail("old-1", "new-1"))
        store, watcher, sent = self.build(session)
        store.create(user_id="imessage:+1555", what="mail from lossfunk", tool_slug="GMAIL_FETCH_EMAILS",
                     arguments={"query": "from:lossfunk"}, interval_seconds=60)
        current = lambda: store.active_for("imessage:+1555")[0]

        self.assertFalse(watcher.check(current()))   # primes on old-1
        self.assertTrue(watcher.check(current()))    # new-1 is news
        self.assertEqual(sent, [("imessage:+1555", "about mail from lossfunk")])

        self.assertFalse(watcher.check(current()))   # the same arrival is not repeated
        self.assertEqual(len(sent), 1)

    def test_the_daily_interrupt_limit_is_respected(self):
        session = Session(mail("old-1"), mail("old-1", "new-1"))
        store, watcher, sent = self.build(session, allow=False)
        store.create(user_id="imessage:+1555", what="mail", tool_slug="GMAIL_FETCH_EMAILS",
                     arguments={}, interval_seconds=60)
        current = lambda: store.active_for("imessage:+1555")[0]

        watcher.check(current())
        self.assertFalse(watcher.check(current()))
        self.assertEqual(sent, [])

    def test_a_watch_is_not_rechecked_before_its_interval(self):
        session = Session(mail("old-1"), mail("old-1", "new-1"))
        store, watcher, sent = self.build(session)
        store.create(user_id="imessage:+1555", what="mail", tool_slug="GMAIL_FETCH_EMAILS",
                     arguments={}, interval_seconds=3600)

        self.assertEqual(watcher.sweep(), 0)   # primes
        self.assertEqual(watcher.sweep(), 0)   # too soon to look again
        self.assertEqual(len(session.calls), 1)

    def test_a_watch_can_be_listed_and_stopped(self):
        store, watcher, _sent = self.build(Session())
        tools = {tool.name: tool for tool in watcher.tools(lambda: "imessage:+1555")}

        created = tools["watch_for"].handler(
            {"what": "mail from lossfunk", "tool_slug": "gmail_fetch_emails", "arguments": {"query": "x"}, "minutes": 5}
        )
        self.assertIn("Watching for mail from lossfunk", created)
        self.assertIn("mail from lossfunk", tools["list_watches"].handler({}))

        watch_id = store.active_for("imessage:+1555")[0].id
        self.assertIn("Stopped", tools["stop_watch"].handler({"id": watch_id}))
        self.assertEqual(store.active_for("imessage:+1555"), [])
        self.assertIn("no active watch", tools["stop_watch"].handler({"id": watch_id}))

    def test_a_watch_needs_somewhere_to_look(self):
        _store, watcher, _sent = self.build(Session())
        tools = {tool.name: tool for tool in watcher.tools(lambda: "imessage:+1555")}

        self.assertIn("needs both", tools["watch_for"].handler({"what": "something"}))

    def test_one_broken_watch_does_not_stop_the_others(self):
        class Broken:
            def execute(self, slug, arguments=None):
                raise RuntimeError("Connection error.")

        directory = tempfile.mkdtemp()
        store = WatchStore(Path(directory) / "state.sqlite3")
        store.create(user_id="a", what="one", tool_slug="X", arguments={}, interval_seconds=0)
        store.create(user_id="b", what="two", tool_slug="Y", arguments={}, interval_seconds=0)
        watcher = Watcher(
            store=store,
            session_for=lambda user: Broken() if user == "a" else Session(mail("m-1")),
            allow=lambda _user: True,
            deliver=lambda _user, _text: True,
            describe=lambda _what, _found: "found",
        )

        self.assertEqual(watcher.sweep(), 0)  # the healthy one primes, the broken one is logged
        self.assertEqual(len(store.active_for("b")), 1)


if __name__ == "__main__":
    unittest.main()
