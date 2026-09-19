from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from agent.types import Tool

from .composio import EXECUTE_ATTEMPTS, WRITE_WORDS, is_transient, trim


logger = logging.getLogger(__name__)


class ComposioRouter:
    """Offer Composio's own router rather than every app action separately.

    The router answers with a handful of meta-tools — search, schemas, execute,
    connections — so the prompt carries a small fixed surface however many apps
    someone connects, and Composio decides which action actually fits. It also
    owns the sign-in flow, which the direct connector had to build by hand.
    """

    EXECUTE = "COMPOSIO_MULTI_EXECUTE_TOOL"
    # Remote shell and arbitrary code execution in Composio's sandbox. Far more
    # than a messaging assistant needs, and not something to leave reachable
    # from a text message, so bloom never offers them.
    WITHHELD = ("COMPOSIO_REMOTE_BASH_TOOL", "COMPOSIO_REMOTE_WORKBENCH")
    SESSION_ATTEMPTS = 5
    RESULT_LIMIT = 6000

    def __init__(self, client: Any, *, result_limit: int = RESULT_LIMIT) -> None:
        self.client = client
        self.result_limit = result_limit
        self._sessions: dict[str, Any] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "ComposioRouter":
        try:
            from composio import Composio
        except ImportError as exc:
            raise RuntimeError("Composio tools require `pip install -e .[connectors]`.") from exc
        api_key = os.getenv("COMPOSIO_API_KEY")
        if not api_key:
            raise RuntimeError("COMPOSIO_API_KEY is required to use connected tools.")
        return cls(
            Composio(api_key=api_key),
            result_limit=int(os.getenv("COMPOSIO_RESULT_LIMIT", str(cls.RESULT_LIMIT))),
        )

    def session_for(self, user_id: str) -> Any:
        """The router session for this person, for work outside a conversation."""
        return self._session(user_id)

    def _session(self, user_id: str) -> Any:
        """One router session per person, reused for the life of the process."""
        with self._lock:
            existing = self._sessions.get(user_id)
        if existing is not None:
            return existing
        failure: Exception | None = None
        for attempt in range(1, self.SESSION_ATTEMPTS + 1):
            try:
                session = self.client.sessions.create(user_id=user_id, manage_connections=True)
            except Exception as error:
                failure = error
                if not is_transient(error):
                    raise
                time.sleep(0.4 * attempt)
                continue
            with self._lock:
                self._sessions[user_id] = session
            return session
        raise RuntimeError(f"Could not open a Composio session: {failure}")

    @staticmethod
    def writes_something(arguments: dict[str, Any]) -> bool:
        """Decide approval from the actions inside the call, not its name.

        Every app action arrives through one execute tool, so the tool's name
        says nothing about whether it sends an email or merely reads one.
        """
        items = arguments.get("tools")
        if not isinstance(items, list) or not items:
            return True  # an unreadable payload is not something to wave through
        slugs = [str(item.get("tool_slug", "")) for item in items if isinstance(item, dict)]
        if len(slugs) != len(items):
            return True
        return any(word in slug.upper() for slug in slugs for word in WRITE_WORDS)

    def for_user(self, user_id: str) -> list[Tool]:
        session = self._session(user_id)
        offered: list[Tool] = []
        for raw in session.tools():
            schema = raw.get("function", raw) if isinstance(raw, dict) else raw
            name = schema["name"]
            if name in self.WITHHELD:
                continue
            offered.append(
                Tool(
                    name=name,
                    description=schema.get("description", ""),
                    parameters=schema.get("parameters", {"type": "object", "properties": {}}),
                    handler=self._handler(session, name),
                    approval_check=self.writes_something if name == self.EXECUTE else None,
                )
            )
        return offered

    def _handler(self, session: Any, name: str):
        def execute(arguments: dict) -> str:
            for attempt in range(1, EXECUTE_ATTEMPTS + 1):
                try:
                    response = session.execute(name, arguments=arguments)
                except Exception as error:
                    if attempt == EXECUTE_ATTEMPTS or not is_transient(error):
                        raise
                    logger.warning("Retrying %s after %s (attempt %d)", name, type(error).__name__, attempt)
                    time.sleep(0.4 * attempt)
                    continue
                return trim(str(response), self.result_limit)
            raise RuntimeError(f"{name} could not be run.")

        return execute
