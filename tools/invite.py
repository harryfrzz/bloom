from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from agent.types import Tool


logger = logging.getLogger(__name__)


def _escape(value: str) -> str:
    """Calendar text has its own escaping, and a stray comma breaks a field."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Calendar lines wrap at 75 octets, continued with a leading space."""
    if len(line) <= 75:
        return line
    head, rest = line[:75], line[75:]
    parts = [head] + [rest[index : index + 74] for index in range(0, len(rest), 74)]
    return "\r\n ".join(parts)


def calendar_invite(
    *,
    title: str,
    start: str,
    end: str | None = None,
    location: str = "",
    notes: str = "",
    timezone: str | None = None,
) -> bytes:
    """An .ics for one event, ready to send to whoever should have it."""
    begins = datetime.fromisoformat(start)
    finishes = datetime.fromisoformat(end) if end else begins.replace(hour=min(begins.hour + 1, 23))
    zone = timezone or (datetime.now().astimezone().tzname() and str(datetime.now().astimezone().tzinfo)) or "UTC"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//bloom//iMessage assistant//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4().hex}@bloom",
        f"DTSTAMP:{stamp}",
        # Carry the zone rather than converting: a time written as 09:55 should
        # arrive as 09:55, whatever the reading device thinks its offset is.
        f"DTSTART;TZID={zone}:{begins:%Y%m%dT%H%M%S}",
        f"DTEND;TZID={zone}:{finishes:%Y%m%dT%H%M%S}",
        f"SUMMARY:{_escape(title)}",
    ]
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if notes:
        lines.append(f"DESCRIPTION:{_escape(notes)}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(_fold(line) for line in lines).encode()


def invite_tool(*, send_file: Callable[[str, str, bytes, str], None], thread_id: Callable[[], str | None]) -> Tool:
    """Send an event to the person as a calendar file they can tap to add.

    Nothing is written anywhere by doing this, so there is nothing to approve:
    the tap is the decision, and it lands on whichever device they are holding
    rather than only on the Mac bloom happens to run on.
    """

    def handler(arguments: dict) -> str:
        thread = thread_id()
        if thread is None:
            return "There is no conversation to send an invite into."
        title = str(arguments.get("title", "")).strip()
        start = str(arguments.get("start", "")).strip()
        if not title or not start:
            return "An invite needs a title and a start time."
        try:
            data = calendar_invite(
                title=title,
                start=start,
                end=str(arguments.get("end") or "") or None,
                location=str(arguments.get("location") or ""),
                notes=str(arguments.get("notes") or ""),
            )
        except ValueError as exc:
            return f"That start or end time did not make sense ({exc}). Give it as 2026-09-21T09:55."
        try:
            send_file(thread, "invite.ics", data, "text/calendar")
        except Exception as exc:
            logger.warning("Could not send a calendar invite: %s", exc)
            return f"The invite would not send ({type(exc).__name__})."
        return f"Invite sent for {title} at {start}. Tell them to tap it to add it to their calendar."

    return Tool(
        name="send_calendar_invite",
        description=(
            "Send the user a calendar invite they can tap to add: a meeting, a class, anything "
            "happening at a time. It arrives as a file in the chat and lands on whichever device they "
            "tap it on, so nothing is written until they choose to. Use this for events rather than "
            "writing to a calendar directly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO time, e.g. 2026-09-21T09:55"},
                "end": {"type": "string", "description": "ISO time. Defaults to an hour later."},
                "location": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["title", "start"],
        },
        handler=handler,
    )
