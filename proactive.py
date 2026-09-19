from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class DailyInterruptLimit:
    """Deliberately simple hackathon policy: at most N unsolicited pings/day."""

    def __init__(self, path: str | Path, *, maximum: int = 3) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.maximum = maximum
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS interrupts (user_id TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL)")

    def allow(self, user_id: str) -> bool:
        day = datetime.now(UTC).date().isoformat()
        with sqlite3.connect(self.path) as connection:
            count = connection.execute("SELECT count(*) FROM interrupts WHERE user_id = ? AND day = ?", (user_id, day)).fetchone()[0]
            if count >= self.maximum:
                return False
            connection.execute("INSERT INTO interrupts VALUES (?, ?, ?)", (user_id, day, datetime.now(UTC).isoformat()))
        return True


class LocalEventListener:
    """Accept locally-forwarded candidates and deliver only rate-limited pings."""

    def __init__(self, *, limit: DailyInterruptLimit, deliver: Callable[[str, str], bool], token: str = "", host: str = "127.0.0.1", port: int = 8788) -> None:
        self.limit = limit
        self.deliver = deliver
        self.token = token
        self.host = host
        self.port = port

    def dispatch(self, payload: dict) -> bool:
        user_id, text = payload.get("user_id"), payload.get("text")
        if not isinstance(user_id, str) or not isinstance(text, str) or not text.strip():
            raise ValueError("event payload requires non-empty string user_id and text")
        if not self.limit.allow(user_id):
            return False
        return self.deliver(user_id, text.strip())

    def run(self) -> None:
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path.split("?", 1)[0] != "/events":
                    self.send_error(404)
                    return
                if listener.token and not hmac.compare_digest(self.headers.get("X-Bloom-Event-Token", ""), listener.token):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    delivered = listener.dispatch(json.loads(self.rfile.read(length)))
                except (ValueError, json.JSONDecodeError) as exc:
                    self.send_error(400, str(exc))
                    return
                self.send_response(202 if delivered else 204)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        ThreadingHTTPServer((self.host, self.port), Handler).serve_forever()
