from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Thread:
    id: int
    user_id: str
    status: str
    next_check_at: str | None
    why_it_matters: str
    last_surfaced_at: str | None
    created_at: str
    updated_at: str


class ThreadStore:
    """Persistent task facts that a conversational session cannot represent."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'waiting_approval', 'done', 'dropped')),
                next_check_at TEXT,
                why_it_matters TEXT NOT NULL,
                last_surfaced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
                )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS threads_user_status ON threads(user_id, status)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, *, user_id: str, why_it_matters: str, status: str = "active", next_check_at: str | None = None) -> Thread:
        if status not in {"active", "waiting_approval", "done", "dropped"}:
            raise ValueError(f"Invalid thread status: {status}")
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO threads (user_id, status, next_check_at, why_it_matters, last_surfaced_at, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (user_id, status, next_check_at, why_it_matters, now, now),
            )
            row = connection.execute("SELECT * FROM threads WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row(row)

    def update_status(self, thread_id: int, status: str) -> Thread:
        if status not in {"active", "waiting_approval", "done", "dropped"}:
            raise ValueError(f"Invalid thread status: {status}")
        with self._connect() as connection:
            connection.execute("UPDATE threads SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), thread_id))
            row = connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if row is None:
            raise KeyError(thread_id)
        return self._row(row)

    def pending_for(self, user_id: str) -> list[Thread]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM threads WHERE user_id = ? AND status IN ('active', 'waiting_approval') ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> Thread:
        return Thread(**dict(row))

