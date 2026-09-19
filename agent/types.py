from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    # Data URLs for anything the person sent as a picture rather than words.
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ProviderResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class Tool:
    """A locally-executed function the conversational model may request."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]
    requires_approval: bool = False
    # True for a stand-in that exists only until the person finishes setting
    # something up, so callers know not to hold on to it.
    provisional: bool = False
    # A router carries the real action in its arguments rather than its name,
    # so some tools can only say whether approval is needed once the call is
    # in hand.
    approval_check: Callable[[dict[str, Any]], bool] | None = None

    def needs_approval(self, arguments: dict[str, Any]) -> bool:
        return self.approval_check(arguments) if self.approval_check else self.requires_approval

    def response_schema(self) -> dict[str, Any]:
        """The native Responses API schema; no Anthropic conversion layer."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": False,
        }

