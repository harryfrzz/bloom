from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from memory.threads import ThreadStore
from .browser import LocalBrowser


@dataclass(frozen=True)
class TaskResult:
    text: str
    thread_id: int
    needs_approval: bool = False


TaskReporter = Callable[[TaskResult], Awaitable[None] | None]


class LocalTaskRunner:
    """Runs layer two in this process using the OpenAI Agents SDK.

    No Agents API, hosted sandbox, or Anthropic component is used. The SDK's
    SQLiteSession persists conversational task state locally while ThreadStore
    records the durable status bloom needs for future surfacing.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        threads: ThreadStore,
        browser: LocalBrowser | None = None,
        model: str | None = None,
    ) -> None:
        database = Path(db_path)
        database.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(database)
        self.threads = threads
        self.browser = browser or LocalBrowser()
        self.model = model

    async def run(self, *, user_id: str, task: str, report: TaskReporter | None = None) -> TaskResult:
        thread = self.threads.create(user_id=user_id, why_it_matters=task)
        try:
            from agents import Agent, Runner, SQLiteSession, function_tool
        except ImportError as exc:
            self.threads.update_status(thread.id, "dropped")
            raise RuntimeError("Task runner requires `pip install -e .[tasks]`.") from exc

        @function_tool
        async def inspect_webpage(url: str) -> str:
            """Open a public webpage locally and return readable source text and a screenshot path."""
            details = await self.browser.research(url)
            return f"Source: {details['url']}\nScreenshot: {details['screenshot']}\n\n{details['text']}"

        agent = Agent(
            name="Bloom task agent",
            model=self.model,
            instructions=(
                "You are bloom's local task agent. Research and prepare useful results with the local browser. "
                "You may never submit a form, buy, book, send, delete, or otherwise commit an external action. "
                "Instead describe the exact proposed action and end with NEEDS_APPROVAL when user confirmation is required. "
                "Include source URLs and summarize enough that the user can decide."
            ),
            tools=[inspect_webpage],
        )
        session = SQLiteSession(f"task:{user_id}:{thread.id}", self.db_path)
        try:
            result = await Runner.run(agent, task, session=session)
            text = str(result.final_output)
            needs_approval = "NEEDS_APPROVAL" in text
            self.threads.update_status(thread.id, "waiting_approval" if needs_approval else "done")
            task_result = TaskResult(text=text, thread_id=thread.id, needs_approval=needs_approval)
            if report:
                possible_awaitable = report(task_result)
                if possible_awaitable is not None:
                    await possible_awaitable
            return task_result
        except Exception:
            self.threads.update_status(thread.id, "dropped")
            raise

    def start(self, **kwargs: object) -> asyncio.Task[TaskResult]:
        """Schedule work without blocking the inbound-channel webhook thread."""
        return asyncio.create_task(self.run(**kwargs))  # type: ignore[arg-type]
