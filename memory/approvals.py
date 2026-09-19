from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class PendingApproval:
    token: str
    user_id: str
    tool_name: str
    arguments: dict
    status: str


class ApprovalStore:
    """Local, restart-safe record of user confirmations for side effects."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS approvals (
                token TEXT PRIMARY KEY, user_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
                created_at TEXT NOT NULL, decided_at TEXT
                )"""
            )

    def request(self, *, user_id: str, tool_name: str, arguments: dict) -> PendingApproval:
        payload = json.dumps(arguments, sort_keys=True)
        with sqlite3.connect(self.path) as connection:
            # The same action asked about twice is one decision, not two. A
            # fresh code each time means an answer to the first no longer
            # matches, and the conversation goes round in circles.
            existing = connection.execute(
                "SELECT token FROM approvals WHERE user_id = ? AND tool_name = ? AND arguments = ? AND status = 'pending'"
                " ORDER BY created_at DESC LIMIT 1",
                (user_id, tool_name, payload),
            ).fetchone()
            if existing is not None:
                return PendingApproval(existing[0], user_id, tool_name, arguments, "pending")
            token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, 'pending', ?, NULL)",
                (token, user_id, tool_name, payload, _now()),
            )
        return PendingApproval(token, user_id, tool_name, arguments, "pending")

    def waiting_for(self, user_id: str) -> list[PendingApproval]:
        """Everything this person has been asked about and not yet answered."""
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT token, user_id, tool_name, arguments, status FROM approvals"
                " WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [PendingApproval(row[0], row[1], row[2], json.loads(row[3]), row[4]) for row in rows]

    def decide(self, *, user_id: str, token: str, approve: bool) -> PendingApproval | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT token, user_id, tool_name, arguments, status FROM approvals WHERE token = ? AND user_id = ?",
                (token.upper(), user_id),
            ).fetchone()
            if row is None or row[4] != "pending":
                return None
            status = "approved" if approve else "denied"
            connection.execute("UPDATE approvals SET status = ?, decided_at = ? WHERE token = ?", (status, _now(), token.upper()))
        return PendingApproval(row[0], row[1], row[2], json.loads(row[3]), status)
