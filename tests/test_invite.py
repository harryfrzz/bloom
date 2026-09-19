import unittest

from tools.invite import calendar_invite, invite_tool


class InviteTests(unittest.TestCase):
    def test_an_invite_carries_the_time_as_written(self):
        ics = calendar_invite(title="standup", start="2026-09-21T09:55", end="2026-09-21T10:55").decode()

        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("SUMMARY:standup", ics)
        # The zone travels with the time, so 09:55 stays 09:55 on the phone.
        self.assertIn("DTSTART;TZID=", ics)
        self.assertIn(":20260921T095500", ics)
        self.assertIn(":20260921T105500", ics)
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))

    def test_an_invite_without_an_end_still_has_one(self):
        self.assertIn(":20260921T105500", calendar_invite(title="call", start="2026-09-21T09:55").decode())

    def test_commas_and_semicolons_cannot_break_a_field(self):
        ics = calendar_invite(title="lunch, then class; bring laptop", start="2026-09-21T13:00").decode()

        self.assertIn("SUMMARY:lunch\\, then class\; bring laptop", ics)

    def test_a_long_line_is_folded_rather_than_truncated(self):
        ics = calendar_invite(title="x" * 200, start="2026-09-21T09:00").decode()

        self.assertTrue(all(len(line) <= 75 for line in ics.split("\r\n")))
        self.assertIn("x" * 50, ics.replace("\r\n ", ""))

    def test_sending_an_invite_needs_no_approval(self):
        sent = []
        tool = invite_tool(
            send_file=lambda thread, name, data, mime: sent.append((thread, name, mime, data)),
            thread_id=lambda: "any;-;+1555",
        )

        # Tapping the invite is the decision, so there is nothing to gate.
        self.assertFalse(tool.requires_approval)
        answer = tool.handler({"title": "standup", "start": "2026-09-21T09:55"})

        thread, name, mime, data = sent[0]
        self.assertEqual((thread, name, mime), ("any;-;+1555", "invite.ics", "text/calendar"))
        self.assertIn(b"SUMMARY:standup", data)
        self.assertIn("tap it", answer)

    def test_a_nonsense_time_is_reported_not_raised(self):
        tool = invite_tool(send_file=lambda *_a: None, thread_id=lambda: "chat")

        self.assertIn("did not make sense", tool.handler({"title": "x", "start": "next tuesday-ish"}))

    def test_nothing_is_sent_without_a_conversation(self):
        tool = invite_tool(send_file=lambda *_a: None, thread_id=lambda: None)

        self.assertIn("no conversation", tool.handler({"title": "x", "start": "2026-09-21T09:00"}))


if __name__ == "__main__":
    unittest.main()
