from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import replace
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .base import InboundHandler, InboundMessage


logger = logging.getLogger(__name__)


class BlueBubblesAdapter:
    """Small local adapter for BlueBubbles REST and webhook events.

    BlueBubbles is the bridge to Messages; bloom itself listens only on the
    configured local interface and stores no remote webhook state.
    """

    def __init__(
        self,
        *,
        base_url: str,
        password: str,
        webhook_token: str = "",
        host: str = "127.0.0.1",
        port: int = 8787,
        poll_interval: float = 3.0,
        max_age: float = 300.0,
        ack_after: float = 4.0,
        ack_text: str = "One sec…",
        ack_writer: Callable[[InboundMessage], str | None] | None = None,
        transcribe: Callable[[bytes, str], str | None] | None = None,
        failure_text: str = "Something broke on my side, sorry. Try me again in a moment.",
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.webhook_token = webhook_token
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.max_age = max_age
        self.ack_after = ack_after
        self.ack_text = ack_text
        self.ack_writer = ack_writer
        self.transcribe = transcribe
        self.failure_text = failure_text
        self._opener = opener
        self._method: str | None = None
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._seen_lock = threading.Lock()

    def _open(self, request: Request):
        """Use certifi on Python.org macOS builds, whose system root store is empty."""
        if self._opener is not urlopen:
            return self._opener(request, timeout=20)
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        return urlopen(request, timeout=20, context=context)

    @classmethod
    def from_local_server_config(cls, **options: Any) -> "BlueBubblesAdapter":
        """Load the server's local connection details without duplicating its password.

        Environment variables remain an explicit override for a remote server;
        the default supports the hackathon's one-Mac deployment.
        """
        database = os.getenv(
            "BLUEBUBBLES_CONFIG_DB",
            os.path.expanduser("~/Library/Application Support/bluebubbles-server/config.db"),
        )
        try:
            with sqlite3.connect(database) as connection:
                values = dict(connection.execute("SELECT name, value FROM config WHERE name IN ('socket_port', 'password')"))
        except sqlite3.Error as exc:
            raise RuntimeError("Could not read local BlueBubbles server configuration.") from exc
        if not values.get("password"):
            raise RuntimeError("BlueBubbles setup is incomplete: the server password is missing.")
        # Reach the server directly rather than through `server_address`: that
        # is the public proxy URL, and a Cloudflare quick tunnel rotates its
        # hostname on every restart, so a cached one starts answering 530.
        return cls(
            base_url=f"http://localhost:{values.get('socket_port') or '1234'}",
            password=values["password"],
            **options,
        )

    def ping(self) -> bool:
        query = urlencode({"guid": self.password})
        request = Request(f"{self.base_url}/api/v1/ping?{query}", method="GET")
        with self._open(request) as response:
            return 200 <= response.status < 300

    def _send_method(self) -> str:
        """Use the Private API when the server offers it, AppleScript otherwise.

        The Private API needs SIP disabled, so a server without it must keep
        driving Messages through AppleScript.  Resolved once per process: the
        answer only changes when the BlueBubbles helper is installed, which
        means restarting the server anyway.
        """
        if self._method is None:
            self._method = "apple-script"
            try:
                query = urlencode({"guid": self.password})
                with self._open(Request(f"{self.base_url}/api/v1/server/info?{query}", method="GET")) as response:
                    info = json.loads(response.read()).get("data") or {}
                if info.get("private_api") and info.get("helper_connected"):
                    self._method = "private-api"
            except Exception as exc:
                logger.warning("Could not read BlueBubbles capabilities, assuming AppleScript: %s", exc)
            logger.info("Sending iMessage replies via %s", self._method)
        return self._method

    def send(self, *, thread_id: str, text: str) -> None:
        if not text.strip():
            return
        query = urlencode({"guid": self.password})
        # BlueBubbles validates the reply body under "message"; a "text" key is
        # rejected with HTTP 400, so the answer never leaves the Mac.  Without
        # the Private API the server falls back to AppleScript, which also
        # requires a caller-supplied tempGuid to match the sent message.
        body = json.dumps(
            {"chatGuid": thread_id, "message": text, "method": self._send_method(), "tempGuid": str(uuid.uuid4())}
        ).encode()
        request = Request(
            f"{self.base_url}/api/v1/message/text?{query}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._open(request) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"BlueBubbles send failed with HTTP {response.status}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"BlueBubbles send failed with HTTP {exc.code}: {detail}") from exc

    def download_attachment(self, guid: str) -> bytes:
        query = urlencode({"guid": self.password})
        request = Request(f"{self.base_url}/api/v1/attachment/{quote(guid)}/download?{query}", method="GET")
        with self._open(request) as response:
            return response.read()

    def send_attachment(
        self,
        *,
        thread_id: str,
        filename: str,
        data: bytes,
        content_type: str = "audio/wav",
        is_audio: bool = True,
    ) -> None:
        """Send a file to a chat, optionally as a voice message."""
        boundary = f"----bloom{uuid.uuid4().hex}"
        fields = {
            "chatGuid": thread_id,
            "name": filename,
            "tempGuid": str(uuid.uuid4()),
            "method": self._send_method(),
            "isAudioMessage": "true" if is_audio else "false",
        }
        body = bytearray()
        for key, value in fields.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"attachment\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        body += data
        body += f"\r\n--{boundary}--\r\n".encode()
        query = urlencode({"guid": self.password})
        request = Request(
            f"{self.base_url}/api/v1/message/attachment?{query}",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._open(request) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"BlueBubbles attachment send failed with HTTP {response.status}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"BlueBubbles attachment send failed with HTTP {exc.code}: {detail}") from exc

    # What a voice note looks like across iMessage's audio formats.
    AUDIO_HINTS = ("audio", "caf", "m4a", "mpeg-4-audio", "amr", "wav", "opus", "ogg")
    # Pictures the model can actually read; HEIC is left out because it cannot.
    IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp")
    # A phone photo is far larger than a model needs in order to read it, and
    # its size is paid again in tokens on every turn, so it is shrunk first.
    IMAGE_EDGE = 1024
    IMAGE_LIMIT = 6
    IMAGE_BYTES = 12_000_000

    @classmethod
    def _is_audio(cls, attachment: Any) -> bool:
        if not isinstance(attachment, dict):
            return False
        marks = " ".join(str(attachment.get(key) or "") for key in ("mimeType", "uti", "transferName")).lower()
        return any(hint in marks for hint in cls.AUDIO_HINTS)

    @classmethod
    def _image_type(cls, attachment: Any) -> str | None:
        """The media type of a picture bloom can show the model, if it is one."""
        if not isinstance(attachment, dict):
            return None
        mime = str(attachment.get("mimeType") or "").lower()
        if mime in cls.IMAGE_TYPES:
            return "image/jpeg" if mime == "image/jpg" else mime
        name = str(attachment.get("transferName") or "").lower()
        for suffix, mime in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".gif", "image/gif"), (".webp", "image/webp")):
            if name.endswith(suffix):
                return mime
        return None

    def _shrink(self, raw: bytes, mime: str) -> bytes:
        """Scale a picture down to something a model can read cheaply.

        Uses macOS's own sips rather than an image library, and keeps the
        original whenever that does not work out.
        """
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".png")
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                handle.write(raw)
                path = handle.name
            subprocess.run(
                ["sips", "-Z", str(self.IMAGE_EDGE), path],
                check=True, capture_output=True, timeout=20,
            )
            with open(path, "rb") as handle:
                smaller = handle.read()
        except Exception as exc:
            logger.warning("Could not resize an image, using it as it came: %s", exc)
            return raw
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass
        if not smaller:
            return raw
        logger.info("Resized an image from %d to %d bytes", len(raw), len(smaller))
        return smaller if len(smaller) < len(raw) else raw

    def _pictures(self, data: dict[str, Any]) -> tuple[str, ...]:
        """Whatever was sent as a picture, ready to hand to the model."""
        found: list[str] = []
        for attachment in (data.get("attachments") or [])[: self.IMAGE_LIMIT * 3]:
            mime = self._image_type(attachment)
            if mime is None or not isinstance(attachment, dict) or not attachment.get("guid"):
                continue
            size = attachment.get("totalBytes")
            if isinstance(size, int) and size > self.IMAGE_BYTES:
                logger.warning("Skipping an image of %d bytes, over the %d limit", size, self.IMAGE_BYTES)
                continue
            try:
                raw = self.download_attachment(str(attachment["guid"]))
            except Exception as exc:
                logger.warning("Could not download an image: %s", exc)
                continue
            if not raw or len(raw) > self.IMAGE_BYTES:
                continue
            raw = self._shrink(raw, mime)
            found.append(f"data:{mime};base64,{base64.b64encode(raw).decode()}")
            if len(found) >= self.IMAGE_LIMIT:
                break
        return tuple(found)

    def _with_transcript(self, data: dict[str, Any]) -> dict[str, Any]:
        """Turn a voice note into words before anything else reads the message."""
        if self.transcribe is None or str(data.get("text") or "").strip():
            return data
        voice = next((item for item in (data.get("attachments") or []) if self._is_audio(item)), None)
        if not isinstance(voice, dict) or not voice.get("guid"):
            return data
        try:
            audio = self.download_attachment(str(voice["guid"]))
            spoken = self.transcribe(audio, str(voice.get("transferName") or "voice.caf"))
        except Exception as exc:
            logger.warning("Could not transcribe a voice note: %s", exc)
            return {**data, "text": "[a voice note arrived but could not be transcribed]"}
        if not spoken:
            return {**data, "text": "[a voice note arrived but nothing could be made out]"}
        logger.warning("Transcribed a voice note into %d characters", len(spoken))
        return {**data, "text": spoken}

    @staticmethod
    def _event_data(payload: dict[str, Any]) -> dict[str, Any] | None:
        data = payload.get("data", payload)
        # BlueBubbles normally sends an object, but some server/webhook paths
        # serialize the event data once more before the outer JSON envelope.
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _is_inbound(payload: dict[str, Any], data: dict[str, Any]) -> bool:
        """Something a person sent us, rather than a receipt or our own echo."""
        event = payload.get("event") or payload.get("eventType") or payload.get("type")
        if event and str(event).lower().replace("_", "-") not in {"new-message", "newmessage"}:
            return False
        if data.get("isFromMe") or data.get("fromMe"):
            return False
        return not data.get("associatedMessageGuid")  # a tapback is not something to answer

    @staticmethod
    def parse_webhook(payload: dict[str, Any]) -> InboundMessage | None:
        """Parse only inbound text messages and ignore receipts/outbound echoes."""
        data = BlueBubblesAdapter._event_data(payload)
        if data is None or not BlueBubblesAdapter._is_inbound(payload, data):
            return None
        text = data.get("text") or data.get("message") or ""
        chats = data.get("chats") or []
        chat = chats[0] if isinstance(chats, list) and chats and isinstance(chats[0], dict) else {}
        chat_guid = data.get("chatGuid") or data.get("chat_guid") or chat.get("guid")
        sender = data.get("handle", data.get("sender", data.get("from")))
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("id")
        if not sender:
            sender = data.get("participants", [None])[0] if isinstance(data.get("participants"), list) else None
        if not isinstance(text, str) or not text.strip() or not chat_guid or not sender:
            return None
        return InboundMessage(channel="imessage", sender=str(sender), thread_id=str(chat_guid), text=text.strip())

    def _claim(self, guid: str | None) -> bool:
        """Answer each message once, whichever of webhook or polling sees it first."""
        if not guid:
            return True
        with self._seen_lock:
            if guid in self._seen:
                return False
            self._seen.add(guid)
            self._seen_order.append(guid)
            while len(self._seen_order) > 500:
                self._seen.discard(self._seen_order.popleft())
        return True

    def _answer(self, handler: InboundHandler, payload: dict[str, Any]) -> None:
        data = self._event_data(payload)
        if data is None or not self._is_inbound(payload, data) or not self._claim(data.get("guid")):
            return
        data = self._with_transcript(data)
        pictures = self._pictures(data)
        if pictures and not str(data.get("text") or "").strip():
            data = {**data, "text": "[sent a picture with no caption]"}
        message = self.parse_webhook({**payload, "data": data})
        if message is not None and pictures:
            message = replace(message, images=pictures)
            logger.warning("Received %d image(s) from %s", len(pictures), message.user_id)
        if message is None:
            # It got past the inbound checks, so something a person sent is
            # being dropped. Never let that happen quietly again.
            logger.warning(
                "Dropped an inbound message with nothing readable (guid=%s, attachments=%d)",
                data.get("guid"),
                len(data.get("attachments") or []),
            )
            return
        # The server's listener stalls and then floods the backlog through, so a
        # message can surface long after it was sent.  Answering one then reads
        # as a reply out of nowhere, landing under whatever is being discussed.
        age = (time.time() * 1000 - data["dateCreated"]) / 1000 if data.get("dateCreated") else 0
        if age > self.max_age:
            logger.warning("Ignoring a %.0fs-old iMessage from %s", age, message.user_id)
            return
        logger.warning("Received iMessage (user=%s, characters=%d)", message.user_id, len(message.text))
        try:
            answer = self._answer_with_a_holding_reply(handler, message)
        except Exception:
            # Silence is the one thing a person cannot act on, and after a
            # holding reply it also breaks a promise to come back to them.
            logger.exception("Could not work out a reply for %s", message.user_id)
            answer = self.failure_text
        if not answer:
            return
        self.send(thread_id=message.thread_id, text=answer)
        logger.info("Sent iMessage reply for user %s", message.user_id)

    def _answer_with_a_holding_reply(self, handler: InboundHandler, message: InboundMessage) -> str | None:
        """Say something if the real answer is taking a while.

        Looking up mail or a calendar runs to tens of seconds, and silence for
        that long reads as nothing happening at all.  A quick answer still
        arrives on its own, with nothing said in front of it.
        """
        if self.ack_after <= 0:
            return handler(message)
        finished = False
        lock = threading.Lock()

        def hold() -> None:
            with lock:
                if finished:
                    return  # the answer beat the timer; do not talk over it
                try:
                    self.send(thread_id=message.thread_id, text=self._holding_line(message))
                except Exception as exc:
                    logger.warning("Could not send a holding reply to %s: %s", message.user_id, exc)

        waiting = threading.Timer(self.ack_after, hold)
        waiting.daemon = True
        waiting.start()
        try:
            return handler(message)
        finally:
            with lock:
                finished = True
            waiting.cancel()

    def _holding_line(self, message: InboundMessage) -> str:
        """Something written for this request, or a plain stand-in."""
        if self.ack_writer is None:
            return self.ack_text
        try:
            return self.ack_writer(message) or self.ack_text
        except Exception as exc:
            logger.warning("Could not write a holding line for %s: %s", message.user_id, exc)
            return self.ack_text

    def _recent_messages(self, after_ms: int) -> list[dict[str, Any]]:
        query = urlencode({"guid": self.password})
        # Attachments included, or a polled voice note arrives with no audio to
        # find and is dropped for having nothing to say.
        body = json.dumps(
            {"limit": 50, "sort": "DESC", "after": max(after_ms, 0), "with": ["handle", "chats", "attachment"]}
        ).encode()
        request = Request(
            f"{self.base_url}/api/v1/message/query?{query}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._open(request) as response:
            data = json.loads(response.read()).get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def poll(self, handler: InboundHandler) -> None:
        """Read new messages directly, because the server's own listener stalls.

        BlueBubbles stops emitting new-message events after a while and only
        recovers on a service restart, so the webhook alone drops replies.  The
        REST query never misses one; `_claim` keeps the two paths from both
        answering the same message.
        """
        cursor = int(time.time() * 1000)
        priming = True
        while True:
            time.sleep(self.poll_interval)
            try:
                messages = self._recent_messages(cursor - 10_000)
            except Exception as exc:
                logger.warning("BlueBubbles poll failed: %s", exc)
                continue
            for data in sorted(messages, key=lambda item: item.get("dateCreated") or 0):
                cursor = max(cursor, data.get("dateCreated") or 0)
                if priming:
                    self._claim(data.get("guid"))  # never answer a backlog on startup
                    continue
                try:
                    self._answer(handler, {"event": "new-message", "data": data})
                except Exception:
                    logger.exception("Could not answer a polled iMessage")
            priming = False

    def run(self, handler: InboundHandler) -> None:
        adapter = self
        threading.Thread(target=self.poll, args=(handler,), name="bluebubbles-poll", daemon=True).start()

        class WebhookHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BlueBubbles validates webhook reachability with GET
                if self.path.split("?", 1)[0] != "/bluebubbles/webhook":
                    self.send_error(404)
                    return
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - HTTP standard method name
                if self.path.split("?", 1)[0] != "/bluebubbles/webhook":
                    self.send_error(404)
                    return
                if adapter.webhook_token:
                    supplied = self.headers.get("X-Bloom-Webhook-Token", "")
                    if not hmac.compare_digest(supplied, adapter.webhook_token):
                        self.send_error(401)
                        return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                    adapter._answer(handler, payload)
                except (ValueError, json.JSONDecodeError) as exc:
                    logger.warning("Rejected malformed BlueBubbles webhook: %s", exc)
                    self.send_error(400, str(exc))
                    return
                except Exception:
                    logger.exception("BlueBubbles webhook handling failed")
                    self.send_error(500)
                    return
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return  # webhook traffic is not application logging

        ThreadingHTTPServer((self.host, self.port), WebhookHandler).serve_forever()
