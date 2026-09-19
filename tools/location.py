from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.request import Request, urlopen

from agent.types import Tool


logger = logging.getLogger(__name__)


class NetworkLocation:
    """Where this machine's internet connection says it is.

    Derived from the public IP, so it describes the network rather than the
    person: city-level at best, and on mobile data it can land on the carrier's
    registered city a long way from whoever is holding the phone. Good enough
    to save someone typing their city, not good enough to state as fact.
    """

    ENDPOINT = "https://ipinfo.io/json"
    TTL = 900.0

    def __init__(
        self,
        *,
        endpoint: str = ENDPOINT,
        ttl: float = TTL,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.endpoint = endpoint
        self.ttl = ttl
        self._opener = opener
        self._cached: tuple[float, str] | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "NetworkLocation":
        return cls(
            endpoint=os.getenv("BLOOM_LOCATION_URL", cls.ENDPOINT),
            ttl=float(os.getenv("BLOOM_LOCATION_TTL", str(cls.TTL))),
        )

    def _open(self, request: Request):
        """Use certifi on Python.org macOS builds, whose root store is empty."""
        if self._opener is not urlopen:
            return self._opener(request, timeout=10)
        import certifi

        return urlopen(request, timeout=10, context=ssl.create_default_context(cafile=certifi.where()))

    def describe(self) -> str:
        with self._lock:
            cached = self._cached
        if cached and time.time() - cached[0] < self.ttl:
            return cached[1]
        request = Request(self.endpoint, headers={"Accept": "application/json", "User-Agent": "bloom"})
        with self._open(request) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, dict):
            raise RuntimeError("The location service returned something unreadable.")
        place = ", ".join(str(payload[key]) for key in ("city", "region", "country") if payload.get(key))
        if not place:
            raise RuntimeError("The location service returned no place.")
        described = (
            f"The internet connection places this machine near {place}"
            + (f" ({payload['loc']})" if payload.get("loc") else "")
            + (f", timezone {payload['timezone']}" if payload.get("timezone") else "")
            + ". This comes from the network, not from a phone, so it is city-level and on mobile "
            "data it is often the carrier's city rather than theirs. Name the place you are assuming "
            "and let them correct it."
        )
        with self._lock:
            self._cached = (time.time(), described)
        return described

    def tool(self) -> Tool:
        def handler(_arguments: dict) -> str:
            try:
                return self.describe()
            except Exception as exc:
                logger.warning("Could not read the network location: %s", exc)
                return "The location lookup failed. Ask the user which place they mean."

        return Tool(
            name="current_location",
            description=(
                "Roughly where the user is, worked out from this machine's internet connection. "
                "Use it when an answer depends on place: weather, what is nearby, local time. "
                "It is approximate and can be a long way off on mobile data, so always prefer a "
                "location the user has mentioned themselves, and say which place you used."
            ),
            parameters={"type": "object", "properties": {}},
            handler=handler,
        )
