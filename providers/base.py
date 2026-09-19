from __future__ import annotations

from typing import Protocol, Sequence

from agent.types import Message, ProviderResponse


class Provider(Protocol):
    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[dict] = (),
    ) -> ProviderResponse:
        """Return text and/or local function calls for one model turn."""


def load(name: str | None = None) -> Provider:
    """Load the configured layer-one provider without leaking it into the agent."""
    import os

    selected = (name or os.getenv("BLOOM_PROVIDER", "openai")).lower()
    if selected == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider.from_environment()
    raise ValueError(f"Unsupported BLOOM_PROVIDER: {selected!r}")

