from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Commitment:
    id: int
    user_id: str
    what: str
    due_at: str | None
    status: str
    nudges: int
    last_nudged_at: str | None
    created_at: str


class CommitmentStore:
    """Things someone said they would do, so bloom can ask how it went.

    Asking once is helping; asking every hour is nagging, so how often each
    has been raised is part of the record rather than something worked out
    afterwards.
    """

    MAX_NUDGES = 3
    QUIET_HOURS = 6

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS commitments (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                what TEXT NOT NULL,
                due_at TEXT,
                status TEXT NOT NULL CHECK(status IN ('open', 'done', 'dropped')),
                nudges INTEGER NOT NULL DEFAULT 0,
                last_nudged_at TEXT,
                created_at TEXT NOT NULL
                )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS commitments_user ON commitments(user_id, status)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def note(self, *, user_id: str, what: str, due_at: str | None = None) -> Commitment:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO commitments (user_id, what, due_at, status, nudges, last_nudged_at, created_at)"
                " VALUES (?, ?, ?, 'open', 0, NULL, ?)",
                (user_id, what, due_at, _now()),
            )
            row = connection.execute("SELECT * FROM commitments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row(row)

    def open_for(self, user_id: str) -> list[Commitment]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM commitments WHERE user_id = ? AND status = 'open' ORDER BY due_at IS NULL, due_at",
                (user_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def worth_asking_about(self, *, now: datetime | None = None) -> list[Commitment]:
        """Open, past due, and not raised too recently or too often."""
        moment = now or datetime.now(UTC)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM commitments WHERE status = 'open' AND due_at IS NOT NULL AND nudges < ?",
                (self.MAX_NUDGES,),
            ).fetchall()
        ready = []
        for row in rows:
            item = self._row(row)
            try:
                due = datetime.fromisoformat(item.due_at)
            except (TypeError, ValueError):
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=UTC)
            if due > moment:
                continue
            if item.last_nudged_at:
                last = datetime.fromisoformat(item.last_nudged_at)
                if (moment - last).total_seconds() < self.QUIET_HOURS * 3600:
                    continue
            ready.append(item)
        return ready

    def record_nudge(self, commitment_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE commitments SET nudges = nudges + 1, last_nudged_at = ? WHERE id = ?", (_now(), commitment_id)
            )

    def settle(self, commitment_id: int, *, user_id: str, status: str = "done") -> bool:
        if status not in {"done", "dropped"}:
            raise ValueError(f"Unknown status: {status}")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE commitments SET status = ? WHERE id = ? AND user_id = ? AND status = 'open'",
                (status, commitment_id, user_id),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _row(row: sqlite3.Row) -> Commitment:
        return Commitment(**dict(row))
