import unittest
from datetime import UTC, datetime

from memory.knowledge import Knowledge


class FakeIndex:
    def __init__(self, matches=None):
        self.upserts = []
        self.queries = []
        self.matches = matches or []

    def upsert(self, *, vectors, namespace):
        self.upserts.append((vectors, namespace))

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"matches": self.matches}


def knowledge(index):
    return Knowledge(index=index, embed=lambda text: [0.1, 0.2, 0.3])


class KnowledgeTests(unittest.TestCase):
    def test_each_person_is_kept_in_their_own_namespace(self):
        index = FakeIndex()
        knowledge(index).remember(user_id="imessage:+916282440799", text="we agreed on Friday")

        _vectors, namespace = index.upserts[0]
        self.assertEqual(namespace, "imessage--916282440799")
        # One person's memories must never be reachable from another's.
        self.assertNotIn("+", namespace)

    def test_what_is_kept_carries_the_day_it_happened(self):
        index = FakeIndex()
        knowledge(index).remember(
            user_id="u", text="the deck is due Friday", kind="watch", when=datetime(2026, 9, 19, 14, 30, tzinfo=UTC)
        )

        metadata = index.upserts[0][0][0]["metadata"]
        self.assertEqual(metadata["day"], "2026-09-19")
        self.assertEqual(metadata["kind"], "watch")
        self.assertEqual(metadata["text"], "the deck is due Friday")

    def test_nothing_is_stored_for_an_empty_thought(self):
        index = FakeIndex()

        self.assertFalse(knowledge(index).remember(user_id="u", text="   "))
        self.assertEqual(index.upserts, [])

    def test_a_named_day_narrows_the_search(self):
        index = FakeIndex()
        knowledge(index).recall(user_id="u", query="what did we decide", on_day="2026-09-19")

        self.assertEqual(index.queries[0]["filter"], {"day": {"$eq": "2026-09-19"}})

    def test_a_range_and_a_kind_narrow_it_too(self):
        index = FakeIndex()
        knowledge(index).recall(user_id="u", query="anything", since="2026-09-01", kind="watch")

        self.assertEqual(index.queries[0]["filter"], {"day": {"$gte": "2026-09-01"}, "kind": {"$eq": "watch"}})

    def test_an_unfiltered_search_asks_for_no_filter(self):
        index = FakeIndex()
        knowledge(index).recall(user_id="u", query="anything")

        self.assertIsNone(index.queries[0]["filter"])

    def test_results_come_back_readable(self):
        index = FakeIndex(matches=[{"metadata": {"text": "we agreed Friday", "when": "2026-09-19T14:30:00", "kind": "conversation"}, "score": 0.83}])
        found = knowledge(index).recall(user_id="u", query="when did we agree")

        self.assertEqual(found[0]["text"], "we agreed Friday")
        self.assertEqual(found[0]["score"], 0.83)

    def test_a_search_that_fails_says_nothing_rather_than_raising(self):
        class Broken(FakeIndex):
            def query(self, **_kwargs):
                raise RuntimeError("pinecone is down")

        self.assertEqual(knowledge(Broken()).recall(user_id="u", query="anything"), [])

    def test_storing_that_fails_is_not_fatal_either(self):
        class Broken(FakeIndex):
            def upsert(self, **_kwargs):
                raise RuntimeError("pinecone is down")

        self.assertFalse(knowledge(Broken()).remember(user_id="u", text="something"))

    def test_the_tool_reports_when_there_is_nothing_to_find(self):
        tool = knowledge(FakeIndex()).tool(lambda: "u")

        self.assertIn("Nothing from before", tool.handler({"about": "anything"}))

    def test_recall_is_not_offered_without_a_key(self):
        import os

        previous = os.environ.pop("PINECONE_API_KEY", None)
        try:
            self.assertIsNone(Knowledge.from_environment())
        finally:
            if previous is not None:
                os.environ["PINECONE_API_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
