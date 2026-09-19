from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Watch:
    id: int
    user_id: str
    what: str
    tool_slug: str
    arguments: dict
    interval_seconds: int
    seen: list[str]
    active: bool
    last_checked_at: str | None
    created_at: str


class WatchStore:
    """Standing requests to be told when something appears.

    A watch is a read someone asked bloom to keep running — new mail from a
    person, a page that changed — with the identifiers it has already reported
    so the same thing is never announced twice.
    """

    # Enough history to recognise a repeat without the row growing without end.
    SEEN_LIMIT = 200

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                what TEXT NOT NULL,
                tool_slug TEXT NOT NULL,
                arguments TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                seen TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT,
                created_at TEXT NOT NULL
                )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS watches_user_active ON watches(user_id, active)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, *, user_id: str, what: str, tool_slug: str, arguments: dict, interval_seconds: int) -> Watch:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO watches (user_id, what, tool_slug, arguments, interval_seconds, seen, active, last_checked_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, '[]', 1, NULL, ?)",
                (user_id, what, tool_slug, json.dumps(arguments), max(int(interval_seconds), 60), now),
            )
            row = connection.execute("SELECT * FROM watches WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row(row)

    def active_for(self, user_id: str) -> list[Watch]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM watches WHERE user_id = ? AND active = 1 ORDER BY id", (user_id,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def due(self, *, now: float) -> list[Watch]:
        """Every active watch whose interval has elapsed."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM watches WHERE active = 1").fetchall()
        due: list[Watch] = []
        for row in rows:
            watch = self._row(row)
            if watch.last_checked_at is None:
                due.append(watch)
                continue
            last = datetime.fromisoformat(watch.last_checked_at).timestamp()
            if now - last >= watch.interval_seconds:
                due.append(watch)
        return due

    def record_check(self, watch_id: int, *, seen: list[str]) -> None:
        # One thing often carries the same id under several keys, and a
        # duplicate would eat into how far back the watch can remember.
        unique = list(dict.fromkeys(seen))
        with self._connect() as connection:
            connection.execute(
                "UPDATE watches SET seen = ?, last_checked_at = ? WHERE id = ?",
                (json.dumps(unique[-self.SEEN_LIMIT :]), _now(), watch_id),
            )

    def stop(self, watch_id: int, *, user_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE watches SET active = 0 WHERE id = ? AND user_id = ? AND active = 1", (watch_id, user_id)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _row(row: sqlite3.Row) -> Watch:
        values = dict(row)
        return Watch(
            id=values["id"],
            user_id=values["user_id"],
            what=values["what"],
            tool_slug=values["tool_slug"],
            arguments=json.loads(values["arguments"]),
            interval_seconds=values["interval_seconds"],
            seen=json.loads(values["seen"]),
            active=bool(values["active"]),
            last_checked_at=values["last_checked_at"],
            created_at=values["created_at"],
        )
