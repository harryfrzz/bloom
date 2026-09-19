import unittest
from types import SimpleNamespace

from tools.composio import ComposioConnector


class FakeTools:
    def get(self, **_kwargs):
        return [{"type": "function", "function": {"name": "GMAIL_SEND_EMAIL", "description": "Send an email", "parameters": {"type": "object"}}}]

    def execute(self, *args, **kwargs):
        self.executed = (args, kwargs)
        return {"successful": True}


class ComposioConnectorTests(unittest.TestCase):
    def test_a_dropped_connection_is_retried_before_giving_up(self):
        attempts = []

        class Flaky:
            def get(self, **_kwargs):
                return [{"type": "function", "function": {"name": "GMAIL_FETCH_EMAILS", "description": "Read", "parameters": {"type": "object"}}}]

            def execute(self, *_args, **_kwargs):
                attempts.append(1)
                if len(attempts) < 3:
                    raise RuntimeError("Connection error.")
                return {"mail": "found"}

        client = SimpleNamespace(
            tools=Flaky(),
            toolkits=SimpleNamespace(get=lambda slug: SimpleNamespace(meta=SimpleNamespace(version=f"{slug}-v1"))),
            auth_configs=SimpleNamespace(list=lambda: [{"id": "ac_gmail", "toolkit": {"slug": "gmail"}}]),
            connected_accounts=SimpleNamespace(list=lambda user_ids: [{"toolkit": {"slug": "gmail"}, "status": "ACTIVE"}]),
        )
        connector = ComposioConnector(client)

        self.assertIn("found", connector.for_user("imessage:+1555")[0].handler({}))
        self.assertEqual(len(attempts), 3)

    def test_an_oversized_result_is_trimmed_before_it_reaches_the_conversation(self):
        class Huge:
            def get(self, **_kwargs):
                return [{"type": "function", "function": {"name": "GMAIL_FETCH_EMAILS", "description": "Read", "parameters": {"type": "object"}}}]

            def execute(self, *_args, **_kwargs):
                return "x" * 90_000

        client = SimpleNamespace(
            tools=Huge(),
            toolkits=SimpleNamespace(get=lambda slug: SimpleNamespace(meta=SimpleNamespace(version=f"{slug}-v1"))),
            auth_configs=SimpleNamespace(list=lambda: [{"id": "ac_gmail", "toolkit": {"slug": "gmail"}}]),
            connected_accounts=SimpleNamespace(list=lambda user_ids: [{"toolkit": {"slug": "gmail"}, "status": "ACTIVE"}]),
        )
        connector = ComposioConnector(client, result_limit=6000)

        answer = connector.for_user("imessage:+1555")[0].handler({})

        self.assertLess(len(answer), 6400)
        self.assertIn("Cut off here", answer)

    def test_apps_are_discovered_and_an_unconnected_one_offers_only_a_way_in(self):
        client = SimpleNamespace(
            tools=FakeTools(),
            toolkits=SimpleNamespace(get=lambda slug: SimpleNamespace(meta=SimpleNamespace(version=f"{slug}-v1"))),
            auth_configs=SimpleNamespace(list=lambda: [
                {"id": "ac_notion", "toolkit": {"slug": "notion"}},
                {"id": "ac_gmail", "toolkit": {"slug": "gmail"}},
            ]),
            connected_accounts=SimpleNamespace(
                list=lambda user_ids: [{"toolkit": {"slug": "gmail"}, "status": "ACTIVE"}],
                link=lambda *, user_id, auth_config_id: SimpleNamespace(
                    redirect_url="https://connect.composio.dev/link/lk_notion"
                ),
            ),
        )
        connector = ComposioConnector(client)

        self.assertEqual(set(connector.toolkits), {"notion", "gmail"})
        tools = {tool.name: tool for tool in connector.for_user("imessage:+1555")}
        # Gmail is connected, so its real actions show up; Notion is not, so the
        # only thing on offer is the sign-in.
        self.assertIn("GMAIL_SEND_EMAIL", tools)
        self.assertNotIn("NOTION_SEND_EMAIL", tools)
        self.assertIn("CONNECT_NOTION", tools)
        self.assertFalse(tools["CONNECT_NOTION"].requires_approval)
        self.assertEqual(tools["CONNECT_NOTION"].parameters, {"type": "object", "properties": {}})
        self.assertIn("https://connect.composio.dev/link/lk_notion", tools["CONNECT_NOTION"].handler({}))

    def test_a_disconnected_app_returns_a_sign_in_link_instead_of_an_error(self):
        class Disconnected:
            def get(self, **_kwargs):
                return [{"type": "function", "function": {"name": "GMAIL_FETCH_EMAILS", "description": "Read", "parameters": {"type": "object"}}}]

            def execute(self, *_args, **_kwargs):
                raise RuntimeError("No connected account found for user ID imessage:+1555 for toolkit gmail")

        linked = []

        def link(*, user_id, auth_config_id):
            linked.append((user_id, auth_config_id))
            return SimpleNamespace(redirect_url="https://connect.composio.dev/link/lk_test")

        client = SimpleNamespace(
            tools=Disconnected(),
            toolkits=SimpleNamespace(get=lambda slug: SimpleNamespace(meta=SimpleNamespace(version=f"{slug}-v1"))),
            auth_configs=SimpleNamespace(list=lambda: [{"id": "ac_gmail", "toolkit": {"slug": "gmail"}}]),
            connected_accounts=SimpleNamespace(link=link),
        )
        connector = ComposioConnector(client, toolkits=["gmail"], allowed=["GMAIL_FETCH_EMAILS"])

        answer = connector.for_user("imessage:+1555")[0].handler({})

        self.assertIn("https://connect.composio.dev/link/lk_test", answer)
        self.assertIn("not connected", answer)
        connector.for_user("imessage:+1555")[0].handler({})
        self.assertEqual(len(linked), 1)  # the pending sign-in is reused

    def test_native_openai_schema_becomes_approved_local_tool(self):
        tools = FakeTools()
        client = SimpleNamespace(
            tools=tools,
            toolkits=SimpleNamespace(get=lambda slug: SimpleNamespace(meta=SimpleNamespace(version=f"{slug}-v1"))),
        )
        connector = ComposioConnector(client, toolkits=["gmail"])
        tool = connector.for_user("imessage:+1555")[0]

        self.assertEqual(tool.name, "GMAIL_SEND_EMAIL")
        self.assertTrue(tool.requires_approval)
        self.assertEqual(tool.response_schema()["parameters"], {"type": "object"})
        self.assertEqual(tool.handler({"to": "a@example.com"}), "{'successful': True}")
        self.assertEqual(tools.executed[1]["version"], "gmail-v1")
