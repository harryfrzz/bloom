import json
import unittest
from types import SimpleNamespace

from agent.types import Message
from providers.openai import OpenAIProvider


class FakeResponses:
    def __init__(self, response):
        self.response = response
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return self.response


class OpenAIProviderTests(unittest.TestCase):
    def test_tool_traffic_is_sent_as_responses_items_not_a_tool_role(self):
        responses = FakeResponses(SimpleNamespace(output=(), output_text="done"))
        provider = OpenAIProvider(client=SimpleNamespace(responses=responses), model="test-model")

        provider.complete(
            system="s",
            messages=[
                Message("user", "check my email"),
                Message("tool_call", json.dumps({"call_id": "c1", "name": "GMAIL_FETCH_EMAILS", "arguments": {"max": 1}})),
                Message("tool", json.dumps({"call_id": "c1", "name": "GMAIL_FETCH_EMAILS", "output": "not connected"})),
            ],
        )

        sent = responses.request["input"]
        self.assertEqual(sent[0], {"role": "user", "content": "check my email"})
        self.assertEqual(sent[1], {"type": "function_call", "call_id": "c1", "name": "GMAIL_FETCH_EMAILS", "arguments": '{"max": 1}'})
        self.assertEqual(sent[2], {"type": "function_call_output", "call_id": "c1", "output": "not connected"})
        self.assertNotIn("tool", [item.get("role") for item in sent])

    def test_uses_responses_api_and_unwraps_composio_schema(self):
        responses = FakeResponses(
            SimpleNamespace(
                output_text="done",
                output=[SimpleNamespace(type="function_call", call_id="call_1", name="calendar", arguments='{"day":"Friday"}')],
            )
        )
        provider = OpenAIProvider(client=SimpleNamespace(responses=responses), model="account-confirmed-model")

        result = provider.complete(
            system="stable",
            messages=[Message("user", "what about Friday")],
            tools=[{"type": "function", "function": {"name": "calendar", "description": "Read calendar", "parameters": {"type": "object"}}}],
        )

        self.assertEqual(result.text, "done")
        self.assertEqual(result.tool_calls[0].arguments, {"day": "Friday"})
        self.assertEqual(responses.request["instructions"], "stable")
        self.assertEqual(responses.request["tools"][0]["name"], "calendar")
        self.assertNotIn("function", responses.request["tools"][0])
        self.assertEqual(responses.request["reasoning"], {"effort": "low"})
        self.assertFalse(responses.request["store"])

    def test_requires_explicit_model(self):
        with self.assertRaisesRegex(ValueError, "BLOOM_MODEL"):
            OpenAIProvider(client=object(), model="")

