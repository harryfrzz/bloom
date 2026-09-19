from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyFormat:
    text: bool
    speech: bool
    language: str


def choose_reply_format(text: str, *, detected_language: str | None = None) -> ReplyFormat:
    """Speak short conversational replies; lists and long answers stay skimmable."""
    language = detected_language or ("hi-IN" if any("\u0900" <= character <= "\u097f" for character in text) else "en-IN")
    is_list = "\n-" in text or "\n1." in text or text.count("\n") >= 2
    is_short = len(text) <= 280 and not is_list
    return ReplyFormat(text=True, speech=is_short, language=language)
