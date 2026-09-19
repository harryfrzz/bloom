from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ChannelIdentity:
    user_id: str
    channel: str
    sender: str
    thread_id: str
    updated_at: str


class IdentityStore:
    """Maps a stable bloom user ID back to the channel route needed to reply."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS channel_identities (
                user_id TEXT PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL,
                thread_id TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )

    def remember(self, *, channel: str, sender: str, thread_id: str) -> ChannelIdentity:
        user_id = f"{channel}:{sender}"
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO channel_identities VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET thread_id = excluded.thread_id, updated_at = excluded.updated_at""",
                (user_id, channel, sender, thread_id, now),
            )
        return ChannelIdentity(user_id, channel, sender, thread_id, now)

    def get(self, user_id: str) -> ChannelIdentity | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT user_id, channel, sender, thread_id, updated_at FROM channel_identities WHERE user_id = ?", (user_id,)).fetchone()
        return ChannelIdentity(*row) if row else None
