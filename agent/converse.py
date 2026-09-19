from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence

from agent.types import Message, Tool
from providers.base import Provider, load


logger = logging.getLogger(__name__)


Approval = Callable[[str, dict], bool]
ApprovalRequest = Callable[[str, dict], str]


class ConversationAgent:
    """Small, locally-owned layer-one tool loop.

    The system prompt remains a stable block and turn data is appended after it,
    preserving the request shape needed for OpenAI prefix caching.
    """

    system = """You are bloom, a personal assistant who lives in someone's
messaging app. Write the way a person texts: warm, direct, usually a line or
two. Plain text only — no markdown, no bold, no headings, no bullet points and
no numbered lists, because asterisks and dashes arrive as literal characters in
a message bubble. Ask for one thing at a time instead of listing everything you
need, skip openers like "Sure!" or "Certainly", and never narrate what you are
about to do. Say what you found or what you need, and stop.
You answer in English, Hindi and Malayalam only, each in either its native
script or its Roman form, and in the code-mixed Hinglish and Manglish people
actually write. Reply in the same language and the same script the user used:
Roman in means Roman out, never transliterated into Devanagari or Malayalam
script, and a native script in means that same native script out. Never let a
native-script character into a Roman-script reply. When the user code-mixes,
mirror the mix rather than tidying it into one language.
Work out which language it is and answer in it whenever your best guess is one
of those three. Roman-script Malayalam is Malayalam and Roman-script Hindi is
Hindi; answering Manglish or Hinglish in English is as wrong as answering it in
the wrong language. Only when your best guess is some other language — Tamil,
Telugu, Kannada, Tulu, Bengali, Marathi or anything else — reply entirely in
English, name that language, and ask them to continue in English, Hindi or
Malayalam. Never tell someone to switch to a language you have just said they
are already writing in, and never answer in a neighbouring language instead of
the one they used. Never fake fluency.
When you turn something the user wrote into a search term, use the plain root
form of a name. Malayalam and Hindi attach endings to nouns, so Lossfunkil,
Lossfunkinte and Lossfunkine all mean Lossfunk, and searching for the inflected
form finds nothing. If a search comes back empty, try the shorter root before
telling anyone there is nothing there.
A voice note arrives already turned into words, so answer it like any other
message. Reply out loud with speak only when they spoke to you or asked you to,
and keep spoken replies to a few sentences; anything with a list or a link is
better read than heard. When you do speak, say so briefly in text too rather
than sending a voice note into silence.
A reminder, a note and a calendar entry are three different things in three
different apps: something to tick off belongs in Reminders, something to keep
and read later in Notes, something happening at a time in the Calendar. Two
calendars exist as well, this Mac's and Google's. When a request could
reasonably mean more than one of these, ask which they meant and name the apps
rather than guessing. When it is obvious, just do it and say where it went.
When someone asks to be told when something happens — mail from a person, a
reply they are waiting on, an invite — set it up with watch_for there and then.
You will not be thinking about it later: an intention you did not record is a
promise already broken, so never say you will let them know unless a watch
exists. Find the tool that would answer the question right now, and watch with
that same tool and arguments.
Use tools only when they materially improve the answer. Never claim a tool has
run when it has not. The tools you are given are live connections to the user's
own accounts: if one exists for what they are asking about, use it instead of
saying you cannot reach it. Only say an app is not connected when a CONNECT
tool is the only tool you hold for it. For any action that changes an external system, explain the
action and wait for explicit user approval through the approval gate."""

    def __init__(
        self,
        provider: Provider | None = None,
        *,
        tools: Sequence[Tool] = (),
        approve: Approval | None = None,
        request_approval: ApprovalRequest | None = None,
    ) -> None:
        self.provider = provider or load()
        self.tools = {tool.name: tool for tool in tools}
        self.approve = approve or (lambda _name, _arguments: False)
        self.request_approval = request_approval

    # Roughly four characters to a token. A long-running chat and a few tool
    # results will outgrow any window eventually, so the conversation is kept to
    # a budget; the exact figure matters less than the retry below, which finds
    # a workable one against whichever model is configured.
    conversation_budget = 40_000
    minimum_budget = 4_000

    @staticmethod
    def _is_context_overflow(error: Exception) -> bool:
        text = str(error).lower()
        return "context" in text and ("exceed" in text or "too long" in text or "maximum" in text)

    # What a picture costs the context each turn, in characters of budget.
    # Its bytes are uploaded once, but the model re-reads it every turn, and
    # counting only the words beside it would hide that entirely.
    IMAGE_WEIGHT = 4_000

    @classmethod
    def _weight(cls, message: Message) -> int:
        return len(message.content) + cls.IMAGE_WEIGHT * len(message.images)

    @classmethod
    def _fit(cls, conversation: list[Message], budget: int) -> list[Message]:
        """Keep the newest turns that fit, oldest dropped first.

        A tool result means nothing without the call it answers, so a
        conversation is never left starting on an orphaned one.
        """
        kept: list[Message] = []
        used = 0
        for message in reversed(conversation):
            used += cls._weight(message)
            if used > budget and kept:
                break
            kept.append(message)
        kept.reverse()
        while kept and kept[0].role == "tool":
            kept.pop(0)
        return kept

    def _complete(self, conversation: list[Message], schemas: list[dict], budget: int):
        """Ask the model, shrinking the conversation until it fits its window."""
        while True:
            try:
                return self.provider.complete(
                    system=self.system, messages=self._fit(conversation, budget), tools=schemas
                ), budget
            except Exception as error:
                if not self._is_context_overflow(error) or budget <= self.minimum_budget:
                    raise
                budget //= 2
                logger.warning("Context window exceeded; retrying within %d characters", budget)

    # Devanagari and Malayalam: the two native scripts bloom writes.
    _native_ranges = ((0x0900, 0x097F), (0x0D00, 0x0D7F))

    @classmethod
    def _in_native_script(cls, text: str) -> bool:
        return any(low <= ord(character) <= high for character in text for low, high in cls._native_ranges)

    acknowledgement = """Someone just asked bloom for something that takes a few
seconds. Say the throwaway thing a friend says while they go and look: casual,
a handful of words, the way people actually text. Same language and script they
used. Eight words is plenty and fewer is better — "checking your mail now",
"onnu nokkatte", "ek sec, dekh raha hoon" are the register, not phrases to
copy. No markdown, nothing formal or wordy. Do not answer the request, do not
guess what you will find, and do not ask them anything."""

    watch_report = """Something a person asked bloom to keep an eye out for has
just turned up. Tell them what arrived and why it matters to them, in one or two
short lines, the way you would text it. Lead with the thing itself, never with
"your watch fired". Use the same language and script they used when they asked.
Plain text, no markdown. If several things arrived, lead with the most important
and count the rest."""

    def report_watch(self, what: str, found: str) -> str | None:
        """Write up what a watch found, or None to let the caller fall back."""
        try:
            result = self.provider.complete(
                system=self.watch_report,
                messages=[Message("user", f"They asked to be told about: {what}\n\nWhat came back:\n{found}")],
            )
        except Exception as exc:
            logger.warning("Could not write up a watch: %s", exc)
            return None
        line = (result.text or "").strip()
        return line or None

    def acknowledge(self, user_text: str) -> str | None:
        """A line to send while the real answer is still being worked out.

        Returns None rather than anything doubtful: the caller has a plain
        fallback, and a wrong-language holding line is worse than a dull one.
        """
        try:
            result = self.provider.complete(system=self.acknowledgement, messages=[Message("user", user_text)])
        except Exception as exc:
            logger.warning("Could not write a holding line: %s", exc)
            return None
        line = " ".join((result.text or "").split())
        if not line or len(line) > 160:
            return None
        if self._in_native_script(line) and not self._in_native_script(user_text):
            return None
        return line

    def _romanise(self, text: str) -> str:
        """Keep a Roman-script conversation in Roman script.

        The model reliably answers in the right language but still slips the
        occasional Devanagari or Malayalam word into a Hinglish or Manglish
        reply, which the prompt alone does not prevent.
        """
        try:
            result = self.provider.complete(
                system=(
                    "Rewrite the message using Roman script only, keeping the same language, "
                    "meaning, tone and formatting. Transliterate any Devanagari or Malayalam "
                    "words rather than translating them. Reply with the rewritten message alone."
                ),
                messages=[Message("user", text)],
            )
        except Exception:
            return text
        return result.text.strip() if result.text and not self._in_native_script(result.text) else text

    def reply(
        self,
        history: list[Message],
        user_text: str,
        *,
        images: Sequence[str] = (),
        extra_tools: Sequence[Tool] = (),
    ) -> str:
        conversation = [*history, Message("user", user_text, images=tuple(images))]
        available_tools = {**self.tools, **{tool.name: tool for tool in extra_tools}}
        schemas = [tool.response_schema() for tool in available_tools.values()]
        budget = self.conversation_budget
        for _ in range(8):
            result, budget = self._complete(conversation, schemas, budget)
            if not result.tool_calls:
                answer = result.text or "I couldn't produce a response. Please try again."
                if self._in_native_script(answer) and not self._in_native_script(user_text):
                    answer = self._romanise(answer)
                return answer
            for call in result.tool_calls:
                # The model's own call has to come back with its result, or the
                # call_id the result refers to resolves to nothing.
                conversation.append(
                    Message("tool_call", json.dumps({"call_id": call.call_id, "name": call.name, "arguments": call.arguments}))
                )
            for call in result.tool_calls:
                tool = available_tools.get(call.name)
                if tool is None:
                    output = f"Unknown tool: {call.name}"
                elif tool.needs_approval(call.arguments) and not self.approve(call.name, call.arguments):
                    if self.request_approval:
                        return self.request_approval(call.name, call.arguments)
                    output = "Action not approved by the user. Do not perform it."
                else:
                    try:
                        output = tool.handler(call.arguments)
                    except Exception as exc:  # keep tool failures observable to the model/user
                        output = f"Tool failed: {type(exc).__name__}: {exc}"
                conversation.append(
                    Message("tool", json.dumps({"call_id": call.call_id, "name": call.name, "output": output}))
                )
        return "I stopped because the request required too many tool steps."
