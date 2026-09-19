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
    # A sample of their own words, so anything bloom sends unprompted is in
    # the language they actually write in rather than defaulting to English.
    writes_like: str = ""


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
            columns = {row[1] for row in connection.execute("PRAGMA table_info(channel_identities)")}
            if "writes_like" not in columns:
                connection.execute("ALTER TABLE channel_identities ADD COLUMN writes_like TEXT NOT NULL DEFAULT ''")

    def remember(self, *, channel: str, sender: str, thread_id: str, writes_like: str = "") -> ChannelIdentity:
        user_id = f"{channel}:{sender}"
        now = datetime.now(UTC).isoformat()
        sample = " ".join(writes_like.split())[:300]
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO channel_identities VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET thread_id = excluded.thread_id,
                    updated_at = excluded.updated_at,
                    writes_like = CASE WHEN excluded.writes_like = '' THEN channel_identities.writes_like
                                       ELSE excluded.writes_like END""",
                (user_id, channel, sender, thread_id, now, sample),
            )
            row = connection.execute(
                "SELECT user_id, channel, sender, thread_id, updated_at, writes_like FROM channel_identities WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return ChannelIdentity(*row)

    def everyone(self) -> list[ChannelIdentity]:
        """Everyone bloom has a way of reaching, for anything unprompted."""
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT user_id, channel, sender, thread_id, updated_at, writes_like FROM channel_identities"
                " ORDER BY updated_at DESC"
            ).fetchall()
        return [ChannelIdentity(*row) for row in rows]

    def get(self, user_id: str) -> ChannelIdentity | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT user_id, channel, sender, thread_id, updated_at, writes_like FROM channel_identities WHERE user_id = ?", (user_id,)).fetchone()
        return ChannelIdentity(*row) if row else None
