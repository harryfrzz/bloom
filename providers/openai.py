from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
from collections.abc import Sequence
from typing import Any

from agent.types import Message, ProviderResponse, ToolCall


logger = logging.getLogger(__name__)


class OpenAIProvider:
    """Layer-one provider implemented with OpenAI's Responses API.

    Reasoning and tool-schema details are deliberately owned here. Keeping this
    adapter narrow is what allows the conversation agent to remain provider
    agnostic.
    """

    def __init__(self, *, client: Any, model: str, reasoning_effort: str = "low") -> None:
        if not model:
            raise ValueError("BLOOM_MODEL is required; run `bloom models` to choose one.")
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort
        # A picture uploaded once and referred to by id afterwards, so the tool
        # loop does not re-send its bytes on every turn of the conversation.
        self._uploads: dict[str, str] = {}

    @classmethod
    def from_environment(cls) -> "OpenAIProvider":
        from openai import OpenAI

        return cls(
            # The network here drops requests often enough that the default two
            # attempts leave a person staring at an unanswered message.
            client=OpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
                max_retries=int(os.getenv("BLOOM_API_RETRIES", "5")),
            ),
            model=os.getenv("BLOOM_MODEL", ""),
            reasoning_effort=os.getenv("BLOOM_REASONING_EFFORT", "low"),
        )

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[dict] = (),
    ) -> ProviderResponse:
        response = self.client.responses.create(
            model=self.model,
            instructions=system,
            input=[self._as_input_item(message) for message in messages],
            tools=[self._responses_tool_schema(tool) for tool in tools],
            reasoning={"effort": self.reasoning_effort},
            store=False,
        )
        calls: list[ToolCall] = []
        for item in getattr(response, "output", ()):
            if getattr(item, "type", None) != "function_call":
                continue
            try:
                arguments = json.loads(item.arguments)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"OpenAI returned invalid JSON for tool {item.name!r}") from exc
            if not isinstance(arguments, dict):
                raise ValueError(f"OpenAI tool arguments for {item.name!r} must be an object")
            calls.append(ToolCall(call_id=item.call_id, name=item.name, arguments=arguments))
        return ProviderResponse(text=getattr(response, "output_text", "") or "", tool_calls=tuple(calls))

    SUFFIXES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

    def _image_part(self, url: str) -> dict[str, Any]:
        """Refer to a picture by id, uploading it the first time it is seen."""
        if not url.startswith("data:"):
            return {"type": "input_image", "image_url": url}
        key = hashlib.sha256(url.encode()).hexdigest()
        known = self._uploads.get(key)
        if known is None:
            try:
                header, _, encoded = url.partition(",")
                mime = header[5:].split(";")[0] or "image/png"
                raw = base64.b64decode(encoded)
                uploaded = self.client.files.create(
                    file=(f"image.{self.SUFFIXES.get(mime, 'png')}", io.BytesIO(raw), mime), purpose="vision"
                )
                known = uploaded.id
            except Exception as exc:
                # Inline bytes still work; they just cost more on every turn.
                logger.warning("Could not upload an image, sending it inline: %s", exc)
                return {"type": "input_image", "image_url": url}
            if len(self._uploads) > 64:
                self._uploads.clear()
            self._uploads[key] = known
        return {"type": "input_image", "file_id": known}

    def _as_input_item(self, message: Message) -> dict[str, Any]:
        """Carry tool traffic as Responses items; it has no "tool" chat role.

        The agent stays provider-agnostic by describing a call and its result as
        ordinary messages, and this boundary turns them into the typed items the
        Responses API expects.
        """
        if message.role not in {"tool_call", "tool"}:
            if not message.images:
                return {"role": message.role, "content": message.content}
            return {
                "role": message.role,
                "content": [
                    {"type": "input_text", "text": message.content},
                    *(self._image_part(url) for url in message.images),
                ],
            }
        payload = json.loads(message.content)
        if message.role == "tool_call":
            return {
                "type": "function_call",
                "call_id": payload["call_id"],
                "name": payload["name"],
                "arguments": json.dumps(payload.get("arguments", {})),
            }
        return {"type": "function_call_output", "call_id": payload["call_id"], "output": payload["output"]}

    @staticmethod
    def _responses_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
        """Accept native Responses tools and Composio's Chat-Completions shape.

        The latter is unwrapped once at the provider boundary. The agent never
        sees or emits Anthropic's ``input_schema`` representation.
        """
        if tool.get("type") != "function":
            return tool
        if "function" in tool:
            function = tool["function"]
            return {
                "type": "function",
                "name": function["name"],
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object", "properties": {}}),
                "strict": function.get("strict", False),
            }
        return tool

