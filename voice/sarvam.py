from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


STT_URL = "https://api.sarvam.ai/speech-to-text"
TTS_URL = "https://api.sarvam.ai/text-to-speech"


class VoiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    confidence: float | None


@dataclass(frozen=True)
class AudioReply:
    audio: bytes
    mime_type: str
    language: str


class SarvamVoice:
    """Thin Sarvam boundary: audio/text transport, no agent policy inside it."""

    def __init__(self, api_key: str, *, session: requests.Session | Any | None = None) -> None:
        if not api_key:
            raise VoiceError("SARVAM_API_KEY is required for voice features.")
        self.api_key = api_key
        self.session = session or requests.Session()

    @classmethod
    def from_environment(cls) -> "SarvamVoice":
        return cls(os.getenv("SARVAM_API_KEY", ""))

    def transcribe(self, audio: bytes, *, filename: str = "voice.ogg", language: str = "unknown") -> Transcript:
        if not audio:
            raise VoiceError("Cannot transcribe an empty audio message.")
        response = self.session.post(
            STT_URL,
            headers={"api-subscription-key": self.api_key},
            files={"file": (filename, io.BytesIO(audio), "audio/ogg")},
            data={"model": "saaras:v3", "language_code": language, "mode": "codemix"},
            timeout=120,
        )
        payload = self._payload(response, "transcription")
        probability = payload.get("language_probability")
        return Transcript(
            text=str(payload.get("transcript") or "").strip(),
            language=payload.get("language_code"),
            confidence=float(probability) if isinstance(probability, (int, float)) else None,
        )

    # bulbul:v3 rejects the older voices, so the default has to be one it knows.
    SPEAKER = "anand"

    def synthesize(self, text: str, *, language: str, speaker: str | None = None) -> AudioReply:
        """Create a Bulbul v3 audio reply; caller owns when speech is useful."""
        if not text.strip():
            raise VoiceError("Cannot synthesize empty text.")
        response = self.session.post(
            TTS_URL,
            headers={"api-subscription-key": self.api_key, "Content-Type": "application/json"},
            json={
                "inputs": [text],
                "target_language_code": language,
                "speaker": speaker or os.getenv("SARVAM_SPEAKER") or self.SPEAKER,
                "model": "bulbul:v3",
                "pace": 1.0,
                "speech_sample_rate": 22050,
            },
            timeout=120,
        )
        payload = self._payload(response, "speech synthesis")
        audios = payload.get("audios") or []
        if not audios or not isinstance(audios[0], str):
            raise VoiceError("Sarvam returned no audio.")
        try:
            audio = base64.b64decode(audios[0])
        except ValueError as exc:
            raise VoiceError("Sarvam returned invalid base64 audio.") from exc
        return AudioReply(audio=audio, mime_type="audio/wav", language=language)

    @staticmethod
    def _payload(response: Any, operation: str) -> dict:
        if not 200 <= response.status_code < 300:
            raise VoiceError(f"Sarvam {operation} failed ({response.status_code}): {str(response.text)[:300]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise VoiceError(f"Sarvam {operation} returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise VoiceError(f"Sarvam {operation} returned an invalid payload.")
        return payload
