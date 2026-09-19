from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InboundMessage:
    """Normalized input; core code never depends on a channel's payload shape."""

    channel: str
    sender: str
    thread_id: str
    text: str
    images: tuple[str, ...] = ()

    @property
    def user_id(self) -> str:
        return f"{self.channel}:{self.sender}"


InboundHandler = Callable[[InboundMessage], str | None]


class ChannelAdapter(Protocol):
    def send(self, *, thread_id: str, text: str) -> None: ...

    def run(self, handler: InboundHandler) -> None: ...

