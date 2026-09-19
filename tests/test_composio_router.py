import unittest
from types import SimpleNamespace

from tools.composio_router import ComposioRouter


class FakeSession:
    def __init__(self):
        self.executed = []

    def tools(self):
        return [
            {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object"}}}
            for name in (
                "COMPOSIO_SEARCH_TOOLS",
                "COMPOSIO_MULTI_EXECUTE_TOOL",
                "COMPOSIO_MANAGE_CONNECTIONS",
                "COMPOSIO_REMOTE_BASH_TOOL",
                "COMPOSIO_REMOTE_WORKBENCH",
            )
        ]

    def execute(self, slug, arguments=None):
        self.executed.append((slug, arguments))
        return {"ok": True}


def router_with(session):
    client = SimpleNamespace(sessions=SimpleNamespace(create=lambda **_kwargs: session))
    return ComposioRouter(client)


class ComposioRouterTests(unittest.TestCase):
    def test_remote_shell_and_workbench_are_never_offered(self):
        tools = {tool.name for tool in router_with(FakeSession()).for_user("imessage:+1555")}

        self.assertIn("COMPOSIO_MULTI_EXECUTE_TOOL", tools)
        self.assertIn("COMPOSIO_MANAGE_CONNECTIONS", tools)
        self.assertNotIn("COMPOSIO_REMOTE_BASH_TOOL", tools)
        self.assertNotIn("COMPOSIO_REMOTE_WORKBENCH", tools)

    def test_approval_follows_the_action_inside_the_call(self):
        tools = {tool.name: tool for tool in router_with(FakeSession()).for_user("imessage:+1555")}
        execute = tools["COMPOSIO_MULTI_EXECUTE_TOOL"]

        reading = {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": {}}]}
        sending = {"tools": [{"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}}]}
        both = {"tools": [{"tool_slug": "GMAIL_FETCH_EMAILS"}, {"tool_slug": "NOTION_ADD_PAGE_CONTENT"}]}

        self.assertFalse(execute.needs_approval(reading))
        self.assertTrue(execute.needs_approval(sending))
        self.assertTrue(execute.needs_approval(both))
        # Searching for tools changes nothing, so it is never gated.
        self.assertFalse(tools["COMPOSIO_SEARCH_TOOLS"].needs_approval({"queries": ["mail"]}))

    def test_an_unreadable_payload_is_gated_rather_than_waved_through(self):
        for payload in ({}, {"tools": "???"}, {"tools": []}, {"tools": ["GMAIL_SEND_EMAIL"]}):
            self.assertTrue(ComposioRouter.writes_something(payload), payload)

    def test_one_session_is_reused_for_a_person(self):
        created = []

        def create(**kwargs):
            created.append(kwargs)
            return FakeSession()

        router = ComposioRouter(SimpleNamespace(sessions=SimpleNamespace(create=create)))
        router.for_user("imessage:+1555")
        router.for_user("imessage:+1555")

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0]["manage_connections"])

    def test_a_dropped_session_request_is_retried(self):
        attempts = []

        def create(**_kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("Connection error.")
            return FakeSession()

        router = ComposioRouter(SimpleNamespace(sessions=SimpleNamespace(create=create)))

        self.assertTrue(router.for_user("imessage:+1555"))
        self.assertEqual(len(attempts), 3)


if __name__ == "__main__":
    unittest.main()
