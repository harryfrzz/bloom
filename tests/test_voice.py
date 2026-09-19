import base64
import unittest

from voice.policy import choose_reply_format
from voice.sarvam import SarvamVoice


class Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if "speech-to-text" in args[0]:
            return Response({"transcript": "kal ka schedule kya hai", "language_code": "hi-IN", "language_probability": 0.93})
        return Response({"audios": [base64.b64encode(b"wav").decode()]})


class VoiceTests(unittest.TestCase):
    def test_sarvam_boundary_transliterates_asr_and_uses_bulbul(self):
        session = Session()
        voice = SarvamVoice("key", session=session)
        transcript = voice.transcribe(b"ogg", filename="note.ogg")
        audio = voice.synthesize("नमस्ते", language="hi-IN")
        self.assertEqual(transcript.text, "kal ka schedule kya hai")
        # Speech comes back in Roman script, the way the same person types.
        self.assertEqual(session.calls[0][1]["data"]["mode"], "translit")
        self.assertEqual(session.calls[1][1]["json"]["model"], "bulbul:v3")
        self.assertEqual(audio.audio, b"wav")

    def test_an_unspeakable_language_is_read_out_in_english_instead(self):
        session = Session()
        voice = SarvamVoice("key", session=session)

        voice.synthesize("vanakkam", language="ta-IN")

        self.assertEqual(session.calls[0][1]["json"]["target_language_code"], "en-IN")

    def test_lists_stay_text_first(self):
        self.assertFalse(choose_reply_format("- one\n- two\n- three").speech)
        self.assertTrue(choose_reply_format("Haan, kal 3 baje free ho.", detected_language="hi-IN").speech)
