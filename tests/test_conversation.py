import unittest

from agent.converse import ConversationAgent
from agent.types import Message, ProviderResponse, Tool, ToolCall


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)


class ConversationTests(unittest.TestCase):
    def test_tool_result_is_returned_to_model(self):
        provider = ScriptedProvider([
            ProviderResponse(tool_calls=(ToolCall("call_1", "calendar", {"day": "Friday"}),)),
            ProviderResponse(text="Friday is free."),
        ])
        tool = Tool("calendar", "Read calendar", {"type": "object"}, lambda args: f"{args['day']} has no events")

        answer = ConversationAgent(provider, tools=[tool]).reply([], "what about Friday")

        self.assertEqual(answer, "Friday is free.")
        second_turn = provider.requests[1]["messages"]
        self.assertEqual(second_turn[-1].role, "tool")
        self.assertIn("Friday has no events", second_turn[-1].content)

    def test_roman_script_question_gets_a_roman_script_answer(self):
        provider = ScriptedProvider([
            ProviderResponse(text="Aah, മനസ്സിലായി — naale oru meeting undallo."),
            ProviderResponse(text="Aah, manassilaayi — naale oru meeting undallo."),
        ])

        answer = ConversationAgent(provider).reply([], "enikku naale oru meeting undu")

        self.assertEqual(answer, "Aah, manassilaayi — naale oru meeting undallo.")
        self.assertIn("Roman script only", provider.requests[1]["system"])

    def test_native_script_question_keeps_its_native_script_answer(self):
        provider = ScriptedProvider([ProviderResponse(text="സുഖമാണ്.")])

        answer = ConversationAgent(provider).reply([], "സുഖമാണോ?")

        self.assertEqual(answer, "സുഖമാണ്.")
        self.assertEqual(len(provider.requests), 1)  # no rewrite pass

    def test_an_overflowing_context_is_retried_smaller_not_surfaced(self):
        class Overflowing:
            def __init__(self):
                self.sizes = []

            def complete(self, **kwargs):
                total = sum(len(message.content) for message in kwargs["messages"])
                self.sizes.append(total)
                if total > 12_000:
                    raise RuntimeError("Your input exceeds the context window of this model.")
                return ProviderResponse(text="ok")

        provider = Overflowing()
        agent = ConversationAgent(provider)
        history = [Message("user", "x" * 9_000), Message("assistant", "y" * 9_000)]

        self.assertEqual(agent.reply(history, "and now?"), "ok")
        self.assertGreater(len(provider.sizes), 1)      # it retried
        self.assertLessEqual(provider.sizes[-1], 12_000)  # and the retry fitted

    def test_a_tool_result_is_never_left_without_the_call_it_answers(self):
        conversation = [
            Message("user", "older question"),
            Message("tool_call", '{"call_id": "c1"}'),
            Message("tool", '{"call_id": "c1", "output": "result"}'),
            Message("user", "newest question"),
        ]

        kept = ConversationAgent._fit(conversation, budget=40)

        self.assertNotEqual(kept[0].role, "tool")
        self.assertEqual(kept[-1].content, "newest question")

    def test_external_action_is_blocked_without_approval(self):
        called = False

        def send_email(_args):
            nonlocal called
            called = True
            return "sent"

        provider = ScriptedProvider([
            ProviderResponse(tool_calls=(ToolCall("call_1", "send_email", {"to": "a@example.com"}),)),
            ProviderResponse(text="I did not send it."),
        ])
        tool = Tool("send_email", "Send email", {"type": "object"}, send_email, requires_approval=True)

        answer = ConversationAgent(provider, tools=[tool]).reply([], "send it")

        self.assertFalse(called)
        self.assertEqual(answer, "I did not send it.")
        self.assertIn("not approved", provider.requests[1]["messages"][-1].content)

