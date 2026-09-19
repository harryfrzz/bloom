import tempfile
import unittest
from pathlib import Path

from agent.converse import ConversationAgent
from app import BloomApp
from memory.approvals import ApprovalStore


class ApprovalTests(unittest.TestCase):
    def store(self):
        return ApprovalStore(Path(tempfile.mkdtemp()) / "state.sqlite3")

    def test_a_bare_approve_answers_what_was_last_asked(self):
        self.assertEqual(BloomApp._parse_decision("Approve"), (None, True))
        self.assertEqual(BloomApp._parse_decision("approve"), (None, True))
        self.assertEqual(BloomApp._parse_decision("deny"), (None, False))
        self.assertEqual(BloomApp._parse_decision("approve ABC123"), ("ABC123", True))

    def test_ordinary_sentences_are_not_decisions(self):
        for text in ("approve this and also send it", "what needs approval?", "hello"):
            self.assertIsNone(BloomApp._parse_decision(text), text)

    def test_the_same_action_asked_twice_keeps_one_code(self):
        store = self.store()
        first = store.request(user_id="u", tool_name="add_apple_event", arguments={"title": "college"})
        second = store.request(user_id="u", tool_name="add_apple_event", arguments={"title": "college"})

        # A new code each time means their answer to the first never matches.
        self.assertEqual(first.token, second.token)
        self.assertEqual(len(store.waiting_for("u")), 1)

    def test_a_different_action_gets_its_own_code(self):
        store = self.store()
        first = store.request(user_id="u", tool_name="add_apple_event", arguments={"title": "college"})
        other = store.request(user_id="u", tool_name="add_apple_event", arguments={"title": "dentist"})

        self.assertNotEqual(first.token, other.token)
        self.assertEqual(len(store.waiting_for("u")), 2)

    def test_what_is_waiting_is_newest_first(self):
        store = self.store()
        store.request(user_id="u", tool_name="t", arguments={"n": 1})
        newest = store.request(user_id="u", tool_name="t", arguments={"n": 2})

        self.assertEqual(store.waiting_for("u")[0].token, newest.token)

    def test_a_decided_approval_stops_waiting(self):
        store = self.store()
        pending = store.request(user_id="u", tool_name="t", arguments={})
        store.decide(user_id="u", token=pending.token, approve=True)

        self.assertEqual(store.waiting_for("u"), [])

    def test_a_rewrite_may_not_turn_a_cancellation_into_a_success(self):
        # The rewriter once answered "deny" with "OK, I added it".
        self.assertFalse(ConversationAgent._same_outcome("Alright, I've dropped it.", "Sheri, njan athu add cheythu"))
        self.assertTrue(ConversationAgent._same_outcome("Alright, I've dropped it.", "Sheri, athu cancel cheythu"))
        self.assertTrue(ConversationAgent._same_outcome("Done. Event added.", "Sheri, event add cheythu"))


if __name__ == "__main__":
    unittest.main()
