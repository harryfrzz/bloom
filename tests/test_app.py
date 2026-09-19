import tempfile
import unittest
from pathlib import Path

from app import BloomApp
from agent.types import ProviderResponse
from agent.types import Tool, ToolCall
from channels.base import InboundMessage


class TextProvider:
    def complete(self, **_kwargs):
        return ProviderResponse(text="Haan, Friday khaali hai.")


class AppTests(unittest.TestCase):
    def test_channel_identity_scopes_history_and_reply(self):
        from agent.converse import ConversationAgent

        with tempfile.TemporaryDirectory() as directory:
            app = BloomApp(ConversationAgent(TextProvider()), db_path=Path(directory) / "state.sqlite3")
            answer = app.handle(InboundMessage("imessage", "+1555", "iMessage;+;chat", "Friday ka kya?"))
            self.assertEqual(answer, "Haan, Friday khaali hai.")
            self.assertEqual(len(app.histories["imessage:+1555"]), 2)

    def test_the_browser_agent_is_not_offered_unless_it_is_switched_on(self):
        from agent.converse import ConversationAgent

        with tempfile.TemporaryDirectory() as directory:
            off = BloomApp(ConversationAgent(TextProvider()), db_path=Path(directory) / "off.sqlite3")
            self.assertNotIn("start_task", off.agent.tools)

            on = BloomApp(
                ConversationAgent(TextProvider()), db_path=Path(directory) / "on.sqlite3", browser_tasks=True
            )
            self.assertIn("start_task", on.agent.tools)

    def test_mail_work_is_refused_by_the_browser_agent_when_gmail_is_connected(self):
        from agent.converse import ConversationAgent

        gmail = Tool("GMAIL_FETCH_EMAILS", "Read mail", {"type": "object"}, lambda _a: "[]")
        with tempfile.TemporaryDirectory() as directory:
            app = BloomApp(
                ConversationAgent(TextProvider()),
                db_path=Path(directory) / "state.sqlite3",
                tool_factory=lambda _user: [gmail],
                browser_tasks=True,
            )
            refusal = app._start_task_from_cli({"task": "check the inbox for mail from Lossfunk"})
            self.assertIn("gmail", refusal)
            self.assertIn("Not started", refusal)

            # The model can phrase its way around the word list, so what the
            # person actually asked for is checked too.
            incoming = InboundMessage("imessage", "+1555", "iMessage;+;chat", "ente mailil lossfunk ninnu vannitundo")
            token = app._active_inbound.set(incoming)
            try:
                reworded = app._start_task_from_cli({"task": "find out if Lossfunk sent anything recently"})
            finally:
                app._active_inbound.reset(token)
            self.assertIn("Not started", reworded)

            # Real web research is untouched by the guard.
            started = app._start_task_from_cli({"task": "compare train fares from Kochi to Bangalore"})
            self.assertIn("Task started", started)

            # And one message never starts a second task.
            again = app._start_task_from_cli({"task": "compare bus fares from Kochi to Bangalore"})
            self.assertIn("already running", again)

    def test_approval_round_trip_executes_only_after_user_code(self):
        class ToolProvider:
            def complete(self, **_kwargs):
                return ProviderResponse(tool_calls=(ToolCall("call_1", "send_email", {"to": "a@example.com"}),))

        sent: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            from agent.converse import ConversationAgent

            tool = Tool("send_email", "Send email", {"type": "object"}, lambda args: sent.append(args["to"]) or "sent", requires_approval=True)
            app = BloomApp(ConversationAgent(ToolProvider(), tools=[tool]), db_path=Path(directory) / "state.sqlite3")
            incoming = InboundMessage("imessage", "+1555", "iMessage;+;chat", "send it")
            prompt = app.handle(incoming)
            token = prompt.split("approve ")[1].split(" ")[0]
            self.assertEqual(sent, [])
            self.assertEqual(app.handle(InboundMessage("imessage", "+1555", "iMessage;+;chat", f"approve {token}")), "Done. sent")
            self.assertEqual(sent, ["a@example.com"])
