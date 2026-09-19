import unittest
from types import SimpleNamespace

from tools.apple import AppleApps, _quote


class Runner:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.scripts = []
        self.result = SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)

    def __call__(self, command, **_kwargs):
        self.scripts.append(command[-1])
        return self.result


class AppleAppsTests(unittest.TestCase):
    def test_quotes_cannot_break_out_of_a_script(self):
        self.assertEqual(_quote('say "hi"'), '"say \\"hi\\""')
        self.assertEqual(_quote("back\\slash"), '"back\\\\slash"')

    def test_a_reminder_carries_its_title_list_and_due_date(self):
        runner = Runner()
        AppleApps(runner=runner).add_reminder(title="call the landlord", due="2026-09-21T10:00", list_name="Errands")

        script = runner.scripts[0]
        self.assertIn('"call the landlord"', script)
        self.assertIn('list "Errands"', script)
        self.assertIn("set hours of theDate to 10", script)
        self.assertIn("due date:theDate", script)

    def test_a_note_without_a_folder_lets_notes_choose(self):
        runner = Runner()
        AppleApps(runner=runner).add_note(title="wifi password", body="bloom1234")

        # Notes has no "default folder", so naming one would fail outright.
        self.assertNotIn("default folder", runner.scripts[0])
        self.assertIn("bloom1234", runner.scripts[0])

    def test_creating_things_always_waits_for_approval(self):
        tools = {tool.name: tool for tool in AppleApps(runner=Runner()).tools()}

        for name in ("add_apple_reminder", "add_apple_note"):
            self.assertTrue(tools[name].requires_approval, name)
        # Reading where things could go changes nothing, so it is not gated.
        self.assertFalse(tools["apple_places"].requires_approval)

    def test_a_refusing_app_is_reported_rather_than_raised(self):
        runner = Runner(returncode=1, stderr="Notes got an error: not authorised")
        tools = {tool.name: tool for tool in AppleApps(runner=runner).tools()}

        answer = tools["add_apple_note"].handler({"title": "anything"})

        self.assertIn("did not work", answer)
        self.assertIn("not authorised", answer)

    def test_the_places_on_offer_are_read_from_the_mac(self):
        runner = Runner(stdout="Home, Work")
        tools = {tool.name: tool for tool in AppleApps(runner=runner).tools()}

        self.assertIn("Home", tools["apple_places"].handler({}))


if __name__ == "__main__":
    unittest.main()
