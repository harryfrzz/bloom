from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from datetime import datetime

from agent.types import Tool


logger = logging.getLogger(__name__)


def _quote(value: str) -> str:
    """Put a Python string safely inside an AppleScript literal."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _applescript(script: str, *, runner: Callable[..., object] | None = None) -> str:
    run = runner or subprocess.run
    finished = run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if getattr(finished, "returncode", 1) != 0:
        raise RuntimeError((getattr(finished, "stderr", "") or "AppleScript failed").strip()[:300])
    return (getattr(finished, "stdout", "") or "").strip()


class AppleApps:
    """Reminders, Notes and Calendar on this Mac, driven through AppleScript.

    These are the apps the person already keeps things in, so bloom writes
    into them rather than inventing a store of its own. Each of these creates
    something, which is why they all go through the approval gate.
    """

    def __init__(self, *, runner: Callable[..., object] | None = None) -> None:
        self._runner = runner

    def _run(self, script: str) -> str:
        return _applescript(script, runner=self._runner)

    # --- reading, so the model can offer real choices --------------------

    def reminder_lists(self) -> list[str]:
        return [name.strip() for name in self._run('tell application "Reminders" to return name of every list').split(",") if name.strip()]

    def note_folders(self) -> list[str]:
        return [name.strip() for name in self._run('tell application "Notes" to return name of every folder').split(",") if name.strip()]

    def calendars(self) -> list[str]:
        script = 'tell application "Calendar" to return name of every calendar whose writable is true'
        return [name.strip() for name in self._run(script).split(",") if name.strip()]

    # --- writing ---------------------------------------------------------

    def add_reminder(self, *, title: str, due: str | None = None, list_name: str | None = None, note: str = "") -> str:
        target = f'list {_quote(list_name)}' if list_name else "default list"
        properties = [f"name:{_quote(title)}"]
        if note:
            properties.append(f"body:{_quote(note)}")
        script = 'tell application "Reminders"\n'
        if due:
            moment = datetime.fromisoformat(due)
            script += (
                f"set theDate to (current date)\n"
                f"set year of theDate to {moment.year}\nset month of theDate to {moment.month}\n"
                f"set day of theDate to {moment.day}\nset hours of theDate to {moment.hour}\n"
                f"set minutes of theDate to {moment.minute}\nset seconds of theDate to 0\n"
            )
            properties.append("due date:theDate")
        script += f"make new reminder at end of {target} with properties {{{', '.join(properties)}}}\nend tell"
        self._run(script)
        return f"Reminder added{f' to {list_name}' if list_name else ''}: {title}" + (f" (due {due})" if due else "")

    def add_note(self, *, title: str, body: str = "", folder: str | None = None) -> str:
        html = f"<div><b>{title}</b></div><div>{body}</div>" if body else f"<div><b>{title}</b></div>"
        properties = f"{{name:{_quote(title)}, body:{_quote(html)}}}"
        # Notes has no "default folder"; without one named, let it choose.
        script = (
            f'tell application "Notes" to tell account 1 to make new note at end of folder {_quote(folder)} '
            f"with properties {properties}"
            if folder
            else f'tell application "Notes" to make new note with properties {properties}'
        )
        self._run(script)
        return f"Note saved{f' in {folder}' if folder else ''}: {title}"

    def add_event(self, *, title: str, start: str, end: str | None = None, calendar: str | None = None, location: str = "") -> str:
        begins = datetime.fromisoformat(start)
        finishes = datetime.fromisoformat(end) if end else begins.replace(hour=min(begins.hour + 1, 23))
        target = f'calendar {_quote(calendar)}' if calendar else "calendar 1"
        properties = [f"summary:{_quote(title)}", "start date:startDate", "end date:endDate"]
        if location:
            properties.append(f"location:{_quote(location)}")
        script = (
            'tell application "Calendar"\n'
            "set startDate to (current date)\n"
            f"set year of startDate to {begins.year}\nset month of startDate to {begins.month}\n"
            f"set day of startDate to {begins.day}\nset hours of startDate to {begins.hour}\n"
            f"set minutes of startDate to {begins.minute}\nset seconds of startDate to 0\n"
            "set endDate to (current date)\n"
            f"set year of endDate to {finishes.year}\nset month of endDate to {finishes.month}\n"
            f"set day of endDate to {finishes.day}\nset hours of endDate to {finishes.hour}\n"
            f"set minutes of endDate to {finishes.minute}\nset seconds of endDate to 0\n"
            f"tell {target} to make new event with properties {{{', '.join(properties)}}}\n"
            "end tell"
        )
        self._run(script)
        return f"Event added{f' to {calendar}' if calendar else ''}: {title} at {start}"

    # --- what the model is offered ---------------------------------------

    def tools(self) -> list[Tool]:
        def guard(work: Callable[[dict], str]) -> Callable[[dict], str]:
            def run(arguments: dict) -> str:
                try:
                    return work(arguments)
                except Exception as exc:
                    logger.warning("An Apple app refused the request: %s", exc)
                    return f"That did not work: {exc}"

            return run

        return [
            Tool(
                name="add_apple_reminder",
                description=(
                    "Add something to the Reminders app on this Mac: a task, an errand, something to "
                    "chase. Use it for things that get ticked off, not for things that happen at a time "
                    "with other people, which belong in the calendar."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "due": {"type": "string", "description": "ISO time, e.g. 2026-09-21T09:00"},
                        "list_name": {"type": "string", "description": "Which Reminders list. Omit for the default."},
                        "note": {"type": "string"},
                    },
                    "required": ["title"],
                },
                handler=guard(lambda a: self.add_reminder(
                    title=a["title"], due=a.get("due"), list_name=a.get("list_name"), note=a.get("note", "")
                )),
                requires_approval=True,
            ),
            Tool(
                name="add_apple_note",
                description=(
                    "Write something into the Notes app on this Mac: an idea, a list, something to keep "
                    "and read later. Use it for things to remember, not things to do."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "folder": {"type": "string", "description": "Which Notes folder. Omit for the default."},
                    },
                    "required": ["title"],
                },
                handler=guard(lambda a: self.add_note(title=a["title"], body=a.get("body", ""), folder=a.get("folder"))),
                requires_approval=True,
            ),
            Tool(
                name="apple_places",
                description=(
                    "The Reminders lists, Notes folders and calendars that exist on this Mac. Read it "
                    "before asking the user where something should go, so the choice you offer is real."
                ),
                parameters={"type": "object", "properties": {}},
                handler=guard(lambda _a: (
                    f"Reminders lists: {', '.join(self.reminder_lists()) or 'none'}\\n"
                    f"Notes folders: {', '.join(self.note_folders()) or 'none'}\\n"
                    f"Calendars: {', '.join(self.calendars()) or 'none'}"
                )),
            ),
        ]
