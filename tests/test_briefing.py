import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memory.commitments import CommitmentStore
from tasks.briefing import Briefing


def store():
    return CommitmentStore(Path(tempfile.mkdtemp()) / "state.sqlite3")


def briefing(commitments, sources=None, compose=None, ask_about=None, sent=None, at=(8, 0)):
    return Briefing(
        commitments=commitments,
        sources=sources if sources is not None else {"Today": lambda: "one meeting"},
        compose=compose or (lambda gathered, who: f"briefing for {who}"),
        ask_about=ask_about or (lambda what, who: f"how's {what} going?"),
        deliver=lambda user, text: (sent if sent is not None else []).append((user, text)) or True,
        who=lambda: ["imessage:+1555"],
        at_hour=at[0],
        at_minute=at[1],
    )


class CommitmentTests(unittest.TestCase):
    def test_something_not_yet_due_is_left_alone(self):
        keeper = store()
        keeper.note(user_id="u", what="send the deck", due_at=(datetime.now(UTC) + timedelta(hours=3)).isoformat())

        self.assertEqual(keeper.worth_asking_about(), [])

    def test_something_overdue_is_worth_asking_about(self):
        keeper = store()
        keeper.note(user_id="u", what="send the deck", due_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())

        self.assertEqual([item.what for item in keeper.worth_asking_about()], ["send the deck"])

    def test_something_with_no_deadline_is_never_chased(self):
        keeper = store()
        keeper.note(user_id="u", what="learn the guitar")

        self.assertEqual(keeper.worth_asking_about(), [])

    def test_asking_once_buys_hours_of_quiet(self):
        keeper = store()
        item = keeper.note(user_id="u", what="send it", due_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
        keeper.record_nudge(item.id)

        self.assertEqual(keeper.worth_asking_about(), [])
        later = datetime.now(UTC) + timedelta(hours=CommitmentStore.QUIET_HOURS + 1)
        self.assertEqual(len(keeper.worth_asking_about(now=later)), 1)

    def test_bloom_gives_up_rather_than_nagging(self):
        keeper = store()
        item = keeper.note(user_id="u", what="send it", due_at=(datetime.now(UTC) - timedelta(days=5)).isoformat())
        for _ in range(CommitmentStore.MAX_NUDGES):
            keeper.record_nudge(item.id)

        self.assertEqual(keeper.worth_asking_about(now=datetime.now(UTC) + timedelta(days=9)), [])

    def test_finishing_something_stops_the_asking(self):
        keeper = store()
        item = keeper.note(user_id="u", what="send it", due_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())

        self.assertTrue(keeper.settle(item.id, user_id="u"))
        self.assertEqual(keeper.worth_asking_about(), [])
        self.assertEqual(keeper.open_for("u"), [])
        self.assertFalse(keeper.settle(item.id, user_id="u"))

    def test_one_person_cannot_settle_another_person_s_commitment(self):
        keeper = store()
        item = keeper.note(user_id="u", what="send it")

        self.assertFalse(keeper.settle(item.id, user_id="someone-else"))


class PromiseTests(unittest.TestCase):
    def test_the_same_promise_is_never_recorded_twice(self):
        keeper = store()
        first = keeper.note(user_id="u", what="send the deck", source="MSG-1")
        again = keeper.note(user_id="u", what="send the deck", source="MSG-1")

        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(len(keeper.open_for("u")), 1)

    def test_a_promise_remembers_who_it_was_made_to(self):
        keeper = store()
        item = keeper.note(user_id="u", what="send the deck", owed_to="Arun", source="MSG-2")

        self.assertEqual(item.owed_to, "Arun")

    def test_promises_noted_in_conversation_need_no_source(self):
        keeper = store()

        self.assertIsNotNone(keeper.note(user_id="u", what="one thing"))
        self.assertIsNotNone(keeper.note(user_id="u", what="another thing"))


class BriefingTests(unittest.TestCase):
    def test_a_briefing_gathers_what_it_can_and_sends_it(self):
        sent = []
        briefing(store(), sent=sent).brief("imessage:+1555")

        self.assertEqual(sent, [("imessage:+1555", "briefing for imessage:+1555")])

    def test_a_source_that_fails_does_not_stop_the_rest(self):
        sent = []
        def broken():
            raise RuntimeError("calendar is asleep")
        seen = {}
        made = briefing(
            store(),
            sources={"Broken": broken, "Today": lambda: "one meeting"},
            compose=lambda gathered, who: seen.setdefault("gathered", gathered) and None or "ok",
            sent=sent,
        )
        made.brief("imessage:+1555")

        self.assertIn("one meeting", seen["gathered"])
        self.assertEqual(len(sent), 1)

    def test_nothing_to_say_means_nothing_is_sent(self):
        sent = []
        made = briefing(store(), sources={"Today": lambda: ""}, sent=sent)

        self.assertFalse(made.brief("imessage:+1555"))
        self.assertEqual(sent, [])

    def test_only_one_briefing_a_day(self):
        sent = []
        made = briefing(store(), sent=sent, at=(8, 0))
        morning = datetime(2026, 9, 21, 8, 5).astimezone()

        made.sweep(now=morning)
        made.sweep(now=morning.replace(hour=11))
        self.assertEqual(len(sent), 1)
        # A new day earns another one.
        made.sweep(now=morning.replace(day=22))
        self.assertEqual(len(sent), 2)

    def test_nothing_is_sent_before_the_appointed_hour(self):
        sent = []
        made = briefing(store(), sent=sent, at=(8, 0))

        made.sweep(now=datetime(2026, 9, 21, 6, 30).astimezone())
        self.assertEqual(sent, [])

    def test_check_ins_come_out_of_the_daily_allowance(self):
        keeper = store()
        keeper.note(user_id="u", what="send it", due_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
        sent = []
        made = Briefing(
            commitments=keeper,
            sources={},
            compose=lambda gathered, who: "x",
            ask_about=lambda what, who: "how's it going?",
            deliver=lambda user, text: sent.append(text) or True,
            who=lambda: [],
            allow=lambda _user: False,
        )

        self.assertEqual(made.chase(), 0)
        self.assertEqual(sent, [])
        # Refused for today, not used up: it can still be asked tomorrow.
        self.assertEqual(len(keeper.worth_asking_about()), 1)

    def test_a_check_in_is_recorded_even_if_delivery_half_fails(self):
        keeper = store()
        keeper.note(user_id="u", what="send it", due_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
        made = Briefing(
            commitments=keeper,
            sources={},
            compose=lambda gathered, who: "x",
            ask_about=lambda what, who: "how's it going?",
            deliver=lambda user, text: False,
            who=lambda: [],
        )

        made.chase()
        # Otherwise the same question goes out again on the next sweep.
        self.assertEqual(keeper.worth_asking_about(), [])


if __name__ == "__main__":
    unittest.main()
