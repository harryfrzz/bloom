from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from agent.converse import ConversationAgent
from agent.types import Message
from app import BloomApp
from channels.bluebubbles import BlueBubblesAdapter
from memory.identities import IdentityStore
from proactive import DailyInterruptLimit, LocalEventListener
from providers.base import load
from tools.composio import ComposioConnector
from tools.composio_router import ComposioRouter


def _load_env() -> None:
    load_dotenv(Path(__file__).with_name(".env"))


def models() -> int:
    """Print account-visible model IDs; no model name is guessed in code."""
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    for model in sorted(client.models.list().data, key=lambda item: item.id):
        print(model.id)
    return 0


def chat() -> int:
    agent = ConversationAgent(load())
    history: list[Message] = []
    print("bloom CLI — type /quit to exit")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text in {"/quit", "/exit"}:
            return 0
        if not text:
            continue
        answer = agent.reply(history, text)
        print(f"bloom> {answer}")
        history.extend((Message("user", text), Message("assistant", answer)))


def serve_imessage() -> int:
    agent = ConversationAgent(load())
    voice = _voice()
    adapter = _bluebubbles_adapter()
    if voice is not None:
        # A voice note is just a message that arrived as sound; turn it into
        # words at the edge so nothing downstream has to care.
        adapter.transcribe = lambda audio, filename: voice.transcribe(audio, filename=filename).text
    # Written per request rather than a fixed phrase, so the wait says what is
    # being looked up and does it in the language they are speaking.
    adapter.ack_writer = lambda incoming: agent.acknowledge(incoming.text)
    db_path = Path(os.getenv("BLOOM_DB_PATH", "data/bloom.sqlite3"))

    def report(thread_id: str, task_result) -> None:
        prefix = "I need your approval before I continue:\n" if task_result.needs_approval else "Task update:\n"
        adapter.send(thread_id=thread_id, text=prefix + task_result.text)

    connector = _composio_connector()
    identities = IdentityStore(db_path)

    def notify(user_id: str, text: str) -> bool:
        """Say something nobody asked for right now, in their own thread."""
        identity = identities.get(user_id)
        if identity is None or identity.channel != "imessage":
            return False
        adapter.send(thread_id=identity.thread_id, text=text)
        return True

    def speak(thread_id: str, text: str, language: str) -> None:
        reply = voice.synthesize(text, language=language)
        adapter.send_attachment(
            thread_id=thread_id,
            filename="bloom.wav",
            data=reply.audio,
            content_type=reply.mime_type,
            is_audio=True,
        )

    app = BloomApp(
        agent,
        db_path=db_path,
        task_report=report,
        tool_factory=connector.for_user if connector else None,
        browser_tasks=os.getenv("BLOOM_BROWSER_TASKS", "").strip().lower() in {"1", "true", "yes", "on"},
        session_for=getattr(connector, "session_for", None),
        notify=notify,
        speak=speak if voice is not None else None,
    )
    print(f"Listening for BlueBubbles webhooks on http://{adapter.host}:{adapter.port}/bluebubbles/webhook")
    print(f"Also polling {adapter.base_url} every {adapter.poll_interval:g}s in case the server stops emitting events")
    adapter.run(app.handle)
    return 0


def serve_events() -> int:
    db_path = Path(os.getenv("BLOOM_DB_PATH", "data/bloom.sqlite3"))
    adapter = _bluebubbles_adapter()
    identities = IdentityStore(db_path)

    def deliver(user_id: str, text: str) -> bool:
        identity = identities.get(user_id)
        if identity is None or identity.channel != "imessage":
            return False
        adapter.send(thread_id=identity.thread_id, text=text)
        return True

    listener = LocalEventListener(
        limit=DailyInterruptLimit(db_path),
        deliver=deliver,
        token=os.getenv("BLOOM_EVENT_TOKEN", ""),
        host=os.getenv("BLOOM_EVENT_HOST", "127.0.0.1"),
        port=int(os.getenv("BLOOM_EVENT_PORT", "8788")),
    )
    print(f"Listening for local proactive events on http://{listener.host}:{listener.port}/events")
    listener.run()
    return 0


def _voice():
    """Sarvam handles speech, when a key for it is configured."""
    if not os.getenv("SARVAM_API_KEY"):
        return None
    try:
        from voice.sarvam import SarvamVoice

        return SarvamVoice.from_environment()
    except Exception as exc:
        logging.getLogger(__name__).warning("Voice features are unavailable: %s", exc)
        return None


def _composio_connector():
    """Composio's router by default; the per-action connector on request.

    The router keeps the tool surface small and fixed however many apps are
    connected, which is why it is the default. COMPOSIO_ROUTER=0 falls back to
    offering each app action directly.
    """
    if not os.getenv("COMPOSIO_API_KEY"):
        return None
    if os.getenv("COMPOSIO_ROUTER", "1").strip().lower() in {"0", "false", "no", "off"}:
        return ComposioConnector.from_environment()
    return ComposioRouter.from_environment()


def _bluebubbles_adapter() -> BlueBubblesAdapter:
    """Prefer explicit deployment config, otherwise use this Mac's server app."""
    common = {
        "webhook_token": os.getenv("BLUEBUBBLES_WEBHOOK_TOKEN", ""),
        "host": os.getenv("BLOOM_WEBHOOK_HOST", "127.0.0.1"),
        "port": int(os.getenv("BLOOM_WEBHOOK_PORT", "8787")),
        "poll_interval": float(os.getenv("BLOOM_POLL_INTERVAL", "3")),
        "max_age": float(os.getenv("BLOOM_MAX_MESSAGE_AGE", "300")),
        # Writing the line costs a second or two, so the timer starts earlier
        # than it would for a fixed phrase.
        "ack_after": float(os.getenv("BLOOM_ACK_AFTER", "4")),
        "ack_text": os.getenv("BLOOM_ACK_TEXT", "One sec…"),
        "failure_text": os.getenv("BLOOM_FAILURE_TEXT", "Something broke on my side, sorry. Try me again in a moment."),
    }
    url, password = os.getenv("BLUEBUBBLES_URL"), os.getenv("BLUEBUBBLES_PASSWORD")
    if url and password:
        return BlueBubblesAdapter(base_url=url, password=password, **common)
    return BlueBubblesAdapter.from_local_server_config(**common)


def main() -> int:
    _load_env()
    logging.basicConfig(
        level=os.getenv("BLOOM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="bloom")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("chat", help="start a terminal conversation")
    subcommands.add_parser("models", help="list model IDs available to this API key")
    subcommands.add_parser("serve-imessage", help="receive BlueBubbles webhooks and reply through iMessage")
    subcommands.add_parser("serve-events", help="receive locally-forwarded proactive event candidates")
    args = parser.parse_args()
    if args.command == "models":
        return models()
    if args.command == "serve-imessage":
        return serve_imessage()
    if args.command == "serve-events":
        return serve_events()
    return chat()


if __name__ == "__main__":
    raise SystemExit(main())
