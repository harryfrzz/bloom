from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from agent.types import Tool
from memory.watches import Watch, WatchStore


logger = logging.getLogger(__name__)

# Keys apps use for the identity of a thing, so the same arrival is recognised
# rather than announced again on the next check.
ID_KEYS = ("messageId", "message_id", "threadId", "thread_id", "eventId", "event_id", "guid", "uid", "id")


def identifiers(payload: Any) -> list[str]:
    """Every stable item id inside whatever an app action returned."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_KEYS and isinstance(value, (str, int)) and str(value):
                    found.append(str(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


class Watcher:
    """Keeps standing watches running and speaks up when one finds something.

    A watch is an ordinary app read replayed on a schedule. What makes it worth
    interrupting someone is not that it ran, but that something new came back,
    so a fired watch is written up by the model rather than relayed raw.
    """

    # At most this many new things described in one message; the rest are counted.
    DETAIL_LIMIT = 5
    SWEEP_SECONDS = 60.0

    def __init__(
        self,
        *,
        store: WatchStore,
        session_for: Callable[[str], Any],
        allow: Callable[[str], bool],
        deliver: Callable[[str, str], bool],
        describe: Callable[[str, str], str | None],
        sweep_seconds: float = SWEEP_SECONDS,
    ) -> None:
        self.store = store
        self.session_for = session_for
        self.allow = allow
        self.deliver = deliver
        self.describe = describe
        self.sweep_seconds = sweep_seconds

    def start(self) -> None:
        threading.Thread(target=self._sweep_forever, name="bloom-watches", daemon=True).start()

    def _sweep_forever(self) -> None:
        while True:
            time.sleep(self.sweep_seconds)
            try:
                self.sweep()
            except Exception:
                logger.exception("A watch sweep failed")

    def sweep(self) -> int:
        fired = 0
        for watch in self.store.due(now=time.time()):
            try:
                fired += int(self.check(watch))
            except Exception:
                logger.exception("Could not check watch %d (%s)", watch.id, watch.tool_slug)
        return fired

    def check(self, watch: Watch) -> bool:
        session = self.session_for(watch.user_id)
        response = session.execute(watch.tool_slug, arguments=watch.arguments)
        payload = getattr(response, "data", None)
        if payload is None:
            payload = response if isinstance(response, (dict, list)) else {}
        found = identifiers(payload)
        fresh = [item for item in found if item not in watch.seen]

        first_run = watch.last_checked_at is None
        self.store.record_check(watch.id, seen=[*watch.seen, *fresh])
        if first_run:
            # What is already there is not news; only what shows up next is.
            logger.info("Watch %d primed with %d existing items", watch.id, len(found))
            return False
        if not fresh:
            return False
        if not self.allow(watch.user_id):
            logger.info("Watch %d found %d new items but the daily limit is reached", watch.id, len(fresh))
            return False

        extract = json.dumps(payload)[:4000]
        summary = self.describe(watch.what, extract)
        if not summary:
            summary = f"Something new turned up for: {watch.what} ({len(fresh)} new)."
        return bool(self.deliver(watch.user_id, summary))

    # --- what the model calls --------------------------------------------

    def tools(self, user_id: Callable[[], str]) -> list[Tool]:
        def start_watch(arguments: dict) -> str:
            what = str(arguments.get("what", "")).strip()
            slug = str(arguments.get("tool_slug", "")).strip().upper()
            if not what or not slug:
                return "A watch needs both a description and the tool slug that finds the thing."
            payload = arguments.get("arguments") or {}
            if not isinstance(payload, dict):
                return "The watch arguments must be an object."
            minutes = max(int(arguments.get("minutes") or 15), 1)
            watch = self.store.create(
                user_id=user_id(),
                what=what,
                tool_slug=slug,
                arguments=payload,
                interval_seconds=minutes * 60,
            )
            return (
                f"Watching for {what} (#{watch.id}), checking every {minutes} minute(s). "
                f"Anything already there is treated as old; only new arrivals are reported."
            )

        def list_watches(_arguments: dict) -> str:
            watches = self.store.active_for(user_id())
            if not watches:
                return "Nothing is being watched right now."
            return "\n".join(
                f"#{watch.id}: {watch.what} (every {watch.interval_seconds // 60} min, via {watch.tool_slug})"
                for watch in watches
            )

        def stop_watch(arguments: dict) -> str:
            try:
                watch_id = int(arguments.get("id"))
            except (TypeError, ValueError):
                return "Which watch should stop? Give its number."
            return (
                f"Stopped watch #{watch_id}."
                if self.store.stop(watch_id, user_id=user_id())
                else f"There is no active watch #{watch_id}."
            )

        return [
            Tool(
                name="watch_for",
                description=(
                    "Keep checking for something and tell the user when it turns up: new mail from a "
                    "person, a calendar invite, a page that changed. Give the tool slug that would find "
                    "it now and the arguments to run it with, the same ones you would use to look it up "
                    "once. Search for the right tool first if you do not know its slug. Only what "
                    "appears after the watch is created is reported."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "what": {"type": "string", "description": "What the user is waiting for, in their words."},
                        "tool_slug": {"type": "string", "description": "The app action that finds it, e.g. GMAIL_FETCH_EMAILS."},
                        "arguments": {"type": "object", "description": "Arguments for that action, e.g. a search query."},
                        "minutes": {"type": "integer", "description": "How often to check. Defaults to 15."},
                    },
                    "required": ["what", "tool_slug", "arguments"],
                },
                handler=start_watch,
            ),
            Tool(
                name="list_watches",
                description="List what the user has asked bloom to watch for.",
                parameters={"type": "object", "properties": {}},
                handler=list_watches,
            ),
            Tool(
                name="stop_watch",
                description="Stop one of the user's watches by its number.",
                parameters={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
                handler=stop_watch,
            ),
        ]
