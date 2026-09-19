from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
from pathlib import Path
from collections.abc import Callable, Sequence
from typing import Any

from agent.converse import ConversationAgent
from agent.types import Message, Tool
from channels.base import InboundMessage
from memory.threads import ThreadStore
from memory.approvals import ApprovalStore
from memory.identities import IdentityStore
from memory.watches import WatchStore
from proactive import DailyInterruptLimit
from tasks.runner import LocalTaskRunner, TaskResult
from tasks.watcher import Watcher
from tools.location import NetworkLocation


# Words that mean a request belongs to a connected app rather than the browser.
# Deliberately narrow: a false match sends real research down the wrong path.
APP_HINTS = {
    "GMAIL": ("gmail", "email", "e-mail", "inbox", "mail"),
    "GOOGLECALENDAR": ("calendar", "calender"),
    "NOTION": ("notion",),
    "SLACK": ("slack",),
}

TaskReport = Callable[[str, TaskResult], None]
ToolFactory = Callable[[str], Sequence[Tool]]
logger = logging.getLogger(__name__)


class BloomApp:
    """Wires channels, the conversational agent, local task work, and SQLite."""

    HISTORY_MESSAGES = 20
    MINIMUM_TASK_UPDATE = 40

    def __init__(
        self,
        agent: ConversationAgent,
        *,
        db_path: str | Path,
        task_report: TaskReport | None = None,
        tool_factory: ToolFactory | None = None,
        browser_tasks: bool = False,
        location: NetworkLocation | None = None,
        session_for: Callable[[str], Any] | None = None,
        notify: Callable[[str, str], bool] | None = None,
        speak: Callable[[str, str, str], None] | None = None,
        awaiting: Callable[..., list[dict]] | None = None,
    ) -> None:
        self.agent = agent
        self.db_path = Path(db_path)
        self.threads = ThreadStore(self.db_path)
        self.approvals = ApprovalStore(self.db_path)
        self.identities = IdentityStore(self.db_path)
        self.interrupts = DailyInterruptLimit(self.db_path)
        self.task_runner = LocalTaskRunner(db_path=self.db_path, threads=self.threads, model=os.getenv("BLOOM_MODEL"))
        self.histories: dict[str, list[Message]] = {}
        self.task_report = task_report
        self.tool_factory = tool_factory
        self._tools_by_user: dict[str, Sequence[Tool]] = {}
        self._active_inbound: contextvars.ContextVar[InboundMessage | None] = contextvars.ContextVar("active_inbound", default=None)
        self._task_started: contextvars.ContextVar[bool] = contextvars.ContextVar("task_started", default=False)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        # The research browser is signed in to nothing, and the model reached
        # for it over the connected apps often enough to be worth switching off
        # until it earns its place back.  The machinery below stays ready.
        if browser_tasks:
            self._offer_browser_tasks()
        self.agent.tools["current_location"] = (location or NetworkLocation.from_environment()).tool()
        if speak is not None:
            self.agent.tools["speak"] = self._voice_tool(speak)
        if awaiting is not None:
            self.agent.tools["waiting_on_you"] = self._waiting_tool(awaiting)
        self.watches = WatchStore(self.db_path)
        self.watcher: Watcher | None = None
        # Watching needs somewhere to read from and someone to tell; without
        # either, offering to watch would be a promise bloom could not keep.
        if session_for is not None and notify is not None:
            self.watcher = Watcher(
                store=self.watches,
                session_for=session_for,
                allow=self.interrupts.allow,
                deliver=notify,
                describe=self.agent.report_watch,
            )
            for tool in self.watcher.tools(self._current_user):
                self.agent.tools[tool.name] = tool
            self.watcher.start()
        self.agent.request_approval = self._request_approval

    def _offer_browser_tasks(self) -> None:
        self.agent.tools["start_task"] = Tool(
            name="start_task",
            description=(
                "Start longer research on the public web, using a local browser that is signed in to "
                "nothing. It cannot read the user's mail, calendar, notes or messages, and it never "
                "commits an external action. For anything in a connected app, use that app's own tools."
            ),
            parameters={"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
            handler=self._start_task_from_cli,
        )

    def _voice_tool(self, speak: Callable[[str, str, str], None]) -> Tool:
        def handler(arguments: dict) -> str:
            incoming = self._active_inbound.get()
            if incoming is None:
                return "There is no conversation to speak into right now."
            said = str(arguments.get("text", "")).strip()
            if not said:
                return "Nothing to say."
            language = str(arguments.get("language") or "en-IN").strip()
            try:
                speak(incoming.thread_id, said, language)
            except Exception as exc:
                logger.warning("Could not send a voice reply: %s", exc)
                return f"The voice reply could not be sent ({type(exc).__name__}). Answer in text instead."
            return "Voice note sent. Keep any written reply very short or skip it."

        return Tool(
            name="speak",
            description=(
                "Send this reply as a voice note instead of writing it out. Use it when the user sent "
                "a voice note themselves, or asked to be spoken to, and not otherwise. Give the text to "
                "say and one of the language codes en-IN, hi-IN or ml-IN. Keep it to a few sentences: "
                "nobody wants to listen to a list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What to say aloud."},
                    "language": {"type": "string", "description": "Language code, e.g. en-IN, hi-IN, ml-IN."},
                },
                "required": ["text"],
            },
            handler=handler,
        )

    def _waiting_tool(self, awaiting: Callable[..., list[dict]]) -> Tool:
        def handler(arguments: dict) -> str:
            try:
                days = max(int(arguments.get("days") or 14), 1)
            except (TypeError, ValueError):
                days = 14
            try:
                rows = awaiting(days=days)
            except Exception as exc:
                logger.warning("Could not read who is waiting: %s", exc)
                return f"Could not read the conversations ({type(exc).__name__})."
            if not rows:
                return f"Nobody is waiting on a reply from the last {days} days."
            return "\n".join(
                f"{row['who']} ({row['when']}, {row['hours_ago']}h ago): {row['said']}" for row in rows
            )

        return Tool(
            name="waiting_on_you",
            description=(
                "Conversations where someone messaged the user and got no reply. Short codes, OTPs and "
                "automated senders are already filtered out, so what comes back is real people. Use it "
                "when they ask who is waiting, what they have missed, or what needs replying to."
            ),
            parameters={
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "How far back to look. Defaults to 14."}},
            },
            handler=handler,
        )

    def _current_user(self) -> str:
        incoming = self._active_inbound.get()
        return incoming.user_id if incoming else "cli"

    def _request_approval(self, tool_name: str, arguments: dict) -> str:
        incoming = self._active_inbound.get()
        user_id = incoming.user_id if incoming else "cli"
        pending = self.approvals.request(user_id=user_id, tool_name=tool_name, arguments=arguments)
        details = ", ".join(f"{key}: {self._short(value)}" for key, value in arguments.items())
        action = self._describe(tool_name, arguments)
        return f"Before I {action} — {details}. Reply approve {pending.token} to go ahead, or deny {pending.token} to drop it."

    @staticmethod
    def _describe(tool_name: str, arguments: dict) -> str:
        """Name what is about to happen, not the machinery carrying it out.

        A router executes every app action under one tool name, so that name
        would tell someone nothing about what they are approving.
        """
        items = arguments.get("tools")
        slugs = [str(item.get("tool_slug", "")) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        readable = [slug.replace("_", " ").lower() for slug in slugs if slug]
        return " and ".join(readable) if readable else tool_name.replace("_", " ").lower()

    @staticmethod
    def _short(value: object, limit: int = 120) -> str:
        """Keep an approval prompt readable in a message bubble."""
        text = str(value)
        return text if len(text) <= limit else f"{text[:limit]}…"

    def _task_loop(self) -> asyncio.AbstractEventLoop:
        """One long-lived loop that every background task runs on.

        A fresh ``asyncio.run`` per task closes its loop when it finishes, but
        the task SDK caches async HTTP clients bound to whichever loop it first
        saw.  The second task onwards then died with "Event loop is closed".
        """
        with self._loop_lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="bloom-tasks", daemon=True).start()
            return self._loop

    def _connected_app_for(self, task: str, user_id: str) -> str | None:
        """Name the connected app this request belongs to, if it belongs to one.

        The person's own words count as much as the task the model writes: the
        model can phrase its way around a word list, they cannot, so what they
        actually asked for is the more reliable half of this.
        """
        incoming = self._active_inbound.get()
        lowered = f"{task} {incoming.text if incoming else ''}".lower()
        available = self._tools_for(user_id)
        for prefix, hints in APP_HINTS.items():
            if any(hint in lowered for hint in hints) and any(name.startswith(f"{prefix}_") for name in available):
                return prefix.lower()
        return None

    def _start_task_from_cli(self, arguments: dict) -> str:
        task = arguments["task"]
        inbound = self._active_inbound.get()
        user_id = inbound.user_id if inbound else "cli"
        # The browser is signed in to nothing, so sending it after someone's
        # mail buys a sign-in wall several minutes later instead of an answer.
        app = self._connected_app_for(task, user_id)
        if app is not None:
            return (
                f"Not started: this is {app} work and the research browser cannot sign in to it. "
                f"Use the {app} tools directly instead."
            )
        if self._task_started.get():
            # One question deserves one piece of background work, not a task per
            # attempt at phrasing it.
            return "Not started: a task for this message is already running. Wait for its result."
        self._task_started.set(True)
        logger.info("Starting a background task for %s: %r", user_id, task)

        async def report(result: TaskResult) -> None:
            if not inbound or not self.task_report:
                return
            if not result.needs_approval and len(result.text.strip()) < self.MINIMUM_TASK_UPDATE:
                # A task that came back with nothing to say should not interrupt
                # someone to say it.
                logger.info("Dropping an empty task update for %s: %r", user_id, result.text)
                return
            self.task_report(inbound.thread_id, result)

        def finished(completed) -> None:
            error = None if completed.cancelled() else completed.exception()
            if error is None:
                return
            logger.error("Background task failed for %s", user_id, exc_info=error)
            if inbound and self.task_report:
                self.task_report(inbound.thread_id, TaskResult(text=f"I couldn't finish that task: {error}", thread_id=-1))

        future = asyncio.run_coroutine_threadsafe(
            self.task_runner.run(user_id=user_id, task=task, report=report), self._task_loop()
        )
        future.add_done_callback(finished)
        return "Task started locally. I will report back when it is ready."

    def handle(self, incoming: InboundMessage) -> str:
        self.identities.remember(channel=incoming.channel, sender=incoming.sender, thread_id=incoming.thread_id)
        decision = self._parse_decision(incoming.text)
        if decision:
            token, approved = decision
            pending = self.approvals.decide(user_id=incoming.user_id, token=token, approve=approved)
            if pending is None:
                return "I couldn't find a pending approval with that code."
            if not approved:
                return "Okay, I cancelled that action."
            tool = self._tools_for(incoming.user_id).get(pending.tool_name)
            if tool is None or not tool.needs_approval(pending.arguments):
                return "That approval is no longer safe to execute; I cancelled it."
            try:
                return f"Done: {tool.handler(pending.arguments)}"
            except Exception as exc:
                return f"I couldn't complete the approved action: {type(exc).__name__}: {exc}"
        history = self.histories.setdefault(incoming.user_id, [])
        token = self._active_inbound.set(incoming)
        started = self._task_started.set(False)
        try:
            tools = tuple(self._tools_for(incoming.user_id).values())
            answer = self.agent.reply(history, incoming.text, images=incoming.images, extra_tools=tools)
        finally:
            self._active_inbound.reset(token)
            self._task_started.reset(started)
        history.extend((Message("user", incoming.text), Message("assistant", answer)))
        # Remembering the last several exchanges is what makes a conversation
        # feel continuous; remembering all of them eventually breaks it.
        del history[: -self.HISTORY_MESSAGES]
        return answer

    def _tools_for(self, user_id: str) -> dict[str, Tool]:
        external = self._tools_by_user.get(user_id)
        if external is None:
            try:
                external = self.tool_factory(user_id) if self.tool_factory else ()
            except Exception as exc:
                # A user may not have connected Gmail/Calendar yet.  Their
                # regular iMessage conversation must remain available.
                logger.warning("External tools unavailable for %s: %s", user_id, exc)
                external = ()
            if external and not any(tool.provisional for tool in external):
                # Only a settled set is worth keeping.  An empty one usually
                # means the lookup failed, and a provisional one means the
                # person still has an app to connect: cache either and they
                # would be told to sign in again after they already had.
                self._tools_by_user[user_id] = external
        return {**self.agent.tools, **{tool.name: tool for tool in external}}

    @staticmethod
    def _parse_decision(text: str) -> tuple[str, bool] | None:
        words = text.strip().split()
        if len(words) != 2 or words[0].lower() not in {"approve", "deny"}:
            return None
        return words[1], words[0].lower() == "approve"

    def proactive_text(self, *, user_id: str, text: str) -> str | None:
        """A final local hard limit before an event may interrupt a person."""
        return text if self.interrupts.allow(user_id) else None
