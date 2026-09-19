import tempfile
import unittest
from pathlib import Path

from agent.converse import ConversationAgent
from agent.types import ProviderResponse
from app import BloomApp
from channels.base import InboundMessage
from channels.bluebubbles import BlueBubblesAdapter


def voice_note(guid="V-1", *, mime="audio/x-caf", name="Audio Message.caf", text=""):
    return {
        "event": "new-message",
        "data": {
            "guid": guid,
            "text": text,
            "isFromMe": False,
            "handle": {"address": "+15551234567"},
            "chats": [{"guid": "any;-;+15551234567"}],
            "attachments": [{"guid": f"{guid}-att", "mimeType": mime, "transferName": name}],
        },
    }


class VoiceNoteTests(unittest.TestCase):
    def adapter(self, transcribe):
        made = BlueBubblesAdapter(
            base_url="https://server.example", password="not-a-real-password", ack_after=0, transcribe=transcribe
        )
        made.download_attachment = lambda guid: b"fake-audio-bytes"
        return made

    def test_iMessage_audio_formats_are_recognised(self):
        for attachment in (
            {"mimeType": "audio/x-caf"},
            {"uti": "com.apple.coreaudio-format"},
            {"transferName": "Audio Message.m4a"},
            {"mimeType": "audio/amr"},
        ):
            self.assertTrue(BlueBubblesAdapter._is_audio(attachment), attachment)
        for attachment in ({"mimeType": "image/png"}, {"transferName": "budget.pdf"}, "not a dict"):
            self.assertFalse(BlueBubblesAdapter._is_audio(attachment), attachment)

    def test_a_voice_note_reaches_the_agent_as_words(self):
        heard = []
        adapter = self.adapter(lambda audio, filename: "kal ka plan kya hai")
        adapter.send = lambda **_kwargs: None

        adapter._answer(lambda message: heard.append(message.text) or "ok", voice_note())

        self.assertEqual(heard, ["kal ka plan kya hai"])

    def test_a_written_message_is_never_sent_for_transcription(self):
        calls = []
        adapter = self.adapter(lambda audio, filename: calls.append(1) or "should not happen")
        adapter.send = lambda **_kwargs: None
        heard = []

        adapter._answer(lambda message: heard.append(message.text) or "ok", voice_note(text="already written"))

        self.assertEqual(heard, ["already written"])
        self.assertEqual(calls, [])

    def test_a_failed_transcription_still_reaches_the_agent(self):
        def broken(_audio, _filename):
            raise RuntimeError("Sarvam is down")

        adapter = self.adapter(broken)
        adapter.send = lambda **_kwargs: None
        heard = []

        adapter._answer(lambda message: heard.append(message.text) or "ok", voice_note())

        self.assertIn("could not be transcribed", heard[0])

    def test_speaking_sends_audio_into_the_conversation(self):
        spoken = []

        class Speaks:
            def complete(self, **_kwargs):
                return ProviderResponse(text="said it")

        with tempfile.TemporaryDirectory() as directory:
            app = BloomApp(
                ConversationAgent(Speaks()),
                db_path=Path(directory) / "state.sqlite3",
                speak=lambda thread_id, text, language: spoken.append((thread_id, text, language)),
            )
            self.assertIn("speak", app.agent.tools)
            token = app._active_inbound.set(InboundMessage("imessage", "+1555", "any;-;+1555", "say it out loud"))
            try:
                answer = app.agent.tools["speak"].handler({"text": "here you go", "language": "ml-IN"})
            finally:
                app._active_inbound.reset(token)

        self.assertEqual(spoken, [("any;-;+1555", "here you go", "ml-IN")])
        self.assertIn("Voice note sent", answer)

    def test_speaking_is_not_offered_without_a_way_to_speak(self):
        with tempfile.TemporaryDirectory() as directory:
            app = BloomApp(ConversationAgent(object()), db_path=Path(directory) / "state.sqlite3")
            self.assertNotIn("speak", app.agent.tools)


if __name__ == "__main__":
    unittest.main()
