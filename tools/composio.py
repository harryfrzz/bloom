from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from typing import Any

from agent.types import Tool


logger = logging.getLogger(__name__)

# Words that mark an action as changing something rather than just reading it.
WRITE_WORDS = (
    "CREATE", "SEND", "DELETE", "UPDATE", "MODIFY", "ARCHIVE", "MOVE", "CANCEL", "INVITE",
    "ADD", "REPLY", "FORWARD", "TRASH", "REMOVE", "PATCH", "INSERT", "IMPORT", "CLEAR",
)

# This network drops requests often enough that a single attempt turns a working
# connection into "I couldn't reach your mail".
TRANSIENT = ("connection", "timeout", "timed out", "temporarily", "502", "503", "504", "rate limit")
EXECUTE_ATTEMPTS = 3


def is_transient(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(sign in text for sign in TRANSIENT)


def trim(text: str, limit: int) -> str:
    """Keep a tool result small enough for a conversation to carry."""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return (
        f"{text[:limit]}\n\n[Cut off here: {dropped} more characters. Ask for fewer "
        f"results, or search for what you actually need, rather than reading everything.]"
    )


class ComposioConnector:
    """Expose connected Gmail/Calendar actions as bloom's local tool contract.

    Composio already produces OpenAI function schemas. This adapter preserves
    those schemas (rather than translating them into Anthropic input_schema)
    and routes execution through the same local approval gate as every other
    side effect.
    """

    WRITE_WORDS = WRITE_WORDS

    # A result is re-sent with every later turn of the tool loop, and one mail
    # fetch alone runs to tens of thousands of tokens, so what comes back is
    # trimmed to something a conversation can carry.
    RESULT_LIMIT = 6000

    EXECUTE_ATTEMPTS = EXECUTE_ATTEMPTS

    # How many of an app's actions to offer when nobody has narrowed it. Every
    # schema is re-sent each message, so an uncapped catalogue would not fit;
    # raise it with COMPOSIO_TOOLKIT_LIMIT or name exact tools in COMPOSIO_TOOLS.
    TOOLKIT_LIMIT = 20

    def __init__(
        self,
        client: Any,
        *,
        toolkits: Iterable[str] | None = None,
        allowed: Iterable[str] | None = None,
        identity: str | None = None,
        toolkit_limit: int = TOOLKIT_LIMIT,
        result_limit: int = RESULT_LIMIT,
    ) -> None:
        self.client = client
        self.allowed = tuple(allowed) if allowed else ()
        # One person's account can serve every channel they talk from; without
        # this each channel identity needs its own Composio connection.
        self.identity = identity
        self.toolkit_limit = toolkit_limit
        self.result_limit = result_limit
        self.auth_configs = self._discover_auth_configs()
        # Whichever apps are set up in Composio, discovered rather than listed
        # here, so connecting a new one is all it takes to make it usable.
        self.toolkits = tuple(toolkits) if toolkits else tuple(self.auth_configs)
        self.versions = self._resolve_versions()
        self._links: dict[tuple[str, str], str] = {}

    @classmethod
    def from_environment(cls) -> "ComposioConnector":
        try:
            from composio import Composio
        except ImportError as exc:
            raise RuntimeError("Composio tools require `pip install -e .[connectors]`.") from exc
        api_key = os.getenv("COMPOSIO_API_KEY")
        if not api_key:
            raise RuntimeError("COMPOSIO_API_KEY is required to use connected tools.")
        chosen = os.getenv("COMPOSIO_TOOLS", "")
        apps = os.getenv("COMPOSIO_TOOLKITS", "")
        return cls(
            Composio(api_key=api_key),
            toolkits=tuple(app.strip().lower() for app in apps.split(",") if app.strip()) or None,
            allowed=tuple(name.strip().upper() for name in chosen.split(",") if name.strip()) or None,
            identity=os.getenv("COMPOSIO_USER_ID") or None,
            toolkit_limit=int(os.getenv("COMPOSIO_TOOLKIT_LIMIT", str(cls.TOOLKIT_LIMIT))),
            result_limit=int(os.getenv("COMPOSIO_RESULT_LIMIT", str(cls.RESULT_LIMIT))),
        )

    def _resolve_versions(self) -> dict[str, str]:
        """Read, do not hard-code, the current version once at application start."""
        versions: dict[str, str] = {}
        for toolkit in self.toolkits:
            descriptor = self.client.toolkits.get(slug=toolkit)
            versions[toolkit] = descriptor.meta.version
        return versions

    def _discover_auth_configs(self) -> dict[str, str]:
        """Every app set up in this Composio project, and how to sign in to it."""
        resolved = {
            pair.split("=", 1)[0].strip().lower(): pair.split("=", 1)[1].strip()
            for pair in os.getenv("COMPOSIO_AUTH_CONFIGS", "").split(",")
            if "=" in pair
        }
        try:
            listing = self.client.auth_configs.list()
        except Exception as exc:
            logger.warning("Could not read Composio auth configs: %s", exc)
            return resolved
        entries = [self._as_dict(item) for item in getattr(listing, "items", listing)]
        # Newest first, so an app set up twice signs in through its latest config.
        entries.sort(key=lambda item: str(item.get("created_at") or item.get("name") or ""), reverse=True)
        for entry in entries:
            slug = (entry.get("toolkit") or {}).get("slug")
            if slug and slug not in resolved:
                resolved[slug] = entry["id"]
        return resolved

    def connect_link(self, user_id: str, toolkit: str) -> str | None:
        """A one-off sign-in URL the person can open to connect this toolkit.

        Cached per person and toolkit: each call opens a pending connection on
        Composio, and one unfinished sign-in is enough.
        """
        cached = self._links.get((user_id, toolkit))
        if cached:
            return cached
        auth_config = self.auth_configs.get(toolkit)
        if not auth_config:
            return None
        try:
            request = self.client.connected_accounts.link(user_id=user_id, auth_config_id=auth_config)
        except Exception as exc:
            logger.warning("Could not create a %s sign-in link for %s: %s", toolkit, user_id, exc)
            return None
        link = getattr(request, "redirect_url", None)
        if link:
            self._links[(user_id, toolkit)] = link
        return link

    def _connected_apps(self, user_id: str) -> set[str] | None:
        """Which apps this person has actually signed in to, or None if unknown."""
        try:
            listing = self.client.connected_accounts.list(user_ids=[user_id])
        except Exception as exc:
            logger.warning("Could not read connected accounts for %s: %s", user_id, exc)
            return None
        connected = set()
        for item in getattr(listing, "items", listing):
            entry = self._as_dict(item)
            slug = (entry.get("toolkit") or {}).get("slug")
            if slug and str(entry.get("status", "")).upper() == "ACTIVE":
                connected.add(slug)
        return connected

    def _connect_message(self, user_id: str, toolkit: str) -> str | None:
        link = self.connect_link(user_id, toolkit)
        if link is None:
            return None
        return (
            f"{toolkit} is not connected yet, so nothing was read or changed. "
            f"Give the user this sign-in link exactly as written: {link} — tell them to open it, "
            f"finish connecting their account, and reply once they are done, and say that you will "
            f"then carry on with what they asked."
        )

    def _connect_tool(self, user_id: str, toolkit: str) -> Tool:
        """The only action an unconnected app can offer: a way to connect it."""

        def execute(_arguments: dict) -> str:
            message = self._connect_message(user_id, toolkit)
            if message is None:
                raise RuntimeError(f"{toolkit} is not connected and no sign-in link is available.")
            return message

        return Tool(
            name=f"CONNECT_{toolkit.upper()}",
            description=(
                f"Get a sign-in link for {toolkit}. The user has not connected {toolkit} yet, so nothing in "
                f"{toolkit} can be read or changed. Call this whenever they ask for anything needing {toolkit}, "
                f"then give them the link it returns and ask them to reply once they have connected."
            ),
            parameters={"type": "object", "properties": {}},
            handler=execute,
            requires_approval=False,
            provisional=True,
        )

    def _trim(self, text: str) -> str:
        return trim(text, self.result_limit)

    @staticmethod
    def _is_transient(error: Exception) -> bool:
        return is_transient(error)

    @staticmethod
    def _is_not_connected(error: Exception) -> bool:
        text = str(error)
        return "ConnectedAccountNotFound" in text or "No connected account" in text

    def for_user(self, user_id: str) -> list[Tool]:
        identity = self.identity or user_id
        if not self.toolkits:
            # Discovery can fail against a blip in Composio; retry rather than
            # leave the assistant permanently tool-less.
            self.auth_configs = self._discover_auth_configs()
            self.toolkits = tuple(self.auth_configs)
            self.versions = self._resolve_versions()
        connected = self._connected_apps(identity)
        result: list[Tool] = []
        for toolkit in self.toolkits:
            if connected is not None and toolkit not in connected:
                # An app nobody has signed in to has no usable actions, only a
                # way in.  Offering one argument-free tool is what lets the model
                # hand over a sign-in link instead of interrogating the person
                # for arguments no action could accept yet.
                result.append(self._connect_tool(identity, toolkit))
                continue
            # A connected app stays fully available: the model picks the action,
            # rather than a list in here deciding for it.
            wanted = [name for name in self.allowed if name.startswith(toolkit.upper())]
            if self.allowed and not wanted:
                continue
            raw_tools = (
                self.client.tools.get(user_id=identity, tools=wanted)
                if wanted
                else self.client.tools.get(user_id=identity, toolkits=[toolkit], limit=self.toolkit_limit)
            )
            for raw_tool in raw_tools:
                schema = self._as_dict(raw_tool)
                function = schema.get("function", schema)
                name = function["name"]
                result.append(
                    Tool(
                        name=name,
                        description=function.get("description", ""),
                        parameters=function.get("parameters", {"type": "object", "properties": {}}),
                        handler=self._handler(
                            name=name, user_id=identity, version=self.versions[toolkit], toolkit=toolkit
                        ),
                        requires_approval=any(word in name.upper() for word in self.WRITE_WORDS),
                    )
                )
        return result

    def _handler(self, *, name: str, user_id: str, version: str, toolkit: str):
        def execute(arguments: dict) -> str:
            for attempt in range(1, self.EXECUTE_ATTEMPTS + 1):
                try:
                    response = self.client.tools.execute(name, arguments=arguments, user_id=user_id, version=version)
                except Exception as error:
                    # A connection can also lapse mid-conversation, after the
                    # tool was built as a connected one.
                    message = self._connect_message(user_id, toolkit) if self._is_not_connected(error) else None
                    if message is not None:
                        return message
                    if attempt == self.EXECUTE_ATTEMPTS or not self._is_transient(error):
                        raise
                    logger.warning("Retrying %s after %s (attempt %d)", name, type(error).__name__, attempt)
                    time.sleep(0.4 * attempt)
                    continue
                return self._trim(str(response))
            raise RuntimeError(f"{name} could not be run.")  # unreachable, keeps the type honest

        return execute

    @staticmethod
    def _as_dict(raw_tool: Any) -> dict[str, Any]:
        if isinstance(raw_tool, dict):
            return raw_tool
        if hasattr(raw_tool, "model_dump"):
            return raw_tool.model_dump()
        raise TypeError(f"Unsupported Composio tool schema: {type(raw_tool).__name__}")
