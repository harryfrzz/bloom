from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime

from memory.commitments import CommitmentStore


logger = logging.getLogger(__name__)


class Briefing:
    """The once-a-day catch-up, and the quiet nudge about what is overdue.

    Both are bloom speaking without being spoken to, which is only welcome
    when it is rare and worth reading: the briefing goes once each morning,
    and an unfinished thing is raised a few times at most, hours apart.
    """

    SWEEP_SECONDS = 300.0

    def __init__(
        self,
        *,
        commitments: CommitmentStore,
        sources: dict[str, Callable[[], str]],
        compose: Callable[[str, str], str | None],
        ask_about: Callable[[str, str], str | None],
        deliver: Callable[[str, str], bool],
        who: Callable[[], list[str]],
        allow: Callable[[str], bool] | None = None,
        scan: Callable[[], int] | None = None,
        at_hour: int = 8,
        at_minute: int = 0,
        sweep_seconds: float = SWEEP_SECONDS,
    ) -> None:
        self.commitments = commitments
        self.sources = sources
        self.compose = compose
        self.ask_about = ask_about
        self.deliver = deliver
        self.who = who
        self.allow = allow or (lambda _user: True)
        self.scan = scan
        self.at_hour = at_hour
        self.at_minute = at_minute
        self.sweep_seconds = sweep_seconds
        self._briefed_on: dict[str, date] = {}

    def start(self) -> None:
        threading.Thread(target=self._forever, name="bloom-briefing", daemon=True).start()

    def _forever(self) -> None:
        while True:
            time.sleep(self.sweep_seconds)
            try:
                self.sweep()
            except Exception:
                logger.exception("The briefing sweep failed")

    def sweep(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now().astimezone()
        sent = 0
        if self._is_time(moment):
            if self.scan is not None and self._briefed_on.get("#scan") != moment.date():
                self._briefed_on["#scan"] = moment.date()
                try:
                    found = self.scan()
                    logger.info("Found %d new commitment(s) in what they wrote", found)
                except Exception:
                    logger.exception("Could not scan for promises")
            for user_id in self.who():
                if self._briefed_on.get(user_id) == moment.date():
                    continue
                self._briefed_on[user_id] = moment.date()
                sent += int(self.brief(user_id))
        sent += self.chase(now=moment.astimezone(UTC))
        return sent

    def _is_time(self, moment: datetime) -> bool:
        """True from the appointed minute onwards, so a late wake still sends."""
        return (moment.hour, moment.minute) >= (self.at_hour, self.at_minute)

    def brief(self, user_id: str) -> bool:
        gathered = []
        for name, read in self.sources.items():
            try:
                found = read()
            except Exception as exc:
                logger.warning("Could not read %s for the briefing: %s", name, exc)
                continue
            if found and found.strip():
                gathered.append(f"{name}:\n{found.strip()}")
        if not gathered:
            logger.info("Nothing worth briefing %s about", user_id)
            return False
        written = self.compose("\n\n".join(gathered), user_id)
        if not written:
            return False
        return bool(self.deliver(user_id, written))

    def chase(self, *, now: datetime | None = None) -> int:
        asked = 0
        for item in self.commitments.worth_asking_about(now=now):
            # A check-in is bloom speaking uninvited, so it comes out of the
            # same daily allowance as everything else unasked for. The morning
            # briefing does not: that is a standing appointment they set.
            if not self.allow(item.user_id):
                logger.info("Not asking about %d today; the daily limit is reached", item.id)
                continue
            question = self.ask_about(item.what, item.user_id)
            if not question:
                continue
            # Recorded before sending: a delivery that half-fails should not
            # turn into the same question every five minutes.
            self.commitments.record_nudge(item.id)
            asked += int(bool(self.deliver(item.user_id, question)))
        return asked
