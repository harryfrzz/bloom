import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.converse import ConversationAgent
from agent.types import Message, ProviderResponse
from app import BloomApp
from channels.base import InboundMessage
from channels.bluebubbles import BlueBubblesAdapter
from providers.openai import OpenAIProvider


def picture(guid="P-1", *, mime="image/png", name="IMG_0001.png", text="", size=1200):
    return {
        "event": "new-message",
        "data": {
            "guid": guid,
            "text": text,
            "isFromMe": False,
            "handle": {"address": "+15551234567"},
            "chats": [{"guid": "any;-;+15551234567"}],
            "attachments": [{"guid": f"{guid}-att", "mimeType": mime, "transferName": name, "totalBytes": size}],
        },
    }


class ImageTests(unittest.TestCase):
    def adapter(self, data=b"fake-image-bytes"):
        made = BlueBubblesAdapter(base_url="https://server.example", password="not-a-real-password", ack_after=0)
        made.download_attachment = lambda guid: data
        made.send = lambda **_kwargs: None
        return made

    def test_the_formats_a_model_can_read_are_recognised(self):
        for attachment, expected in (
            ({"mimeType": "image/png"}, "image/png"),
            ({"mimeType": "image/JPG"}, "image/jpeg"),
            ({"transferName": "photo.JPEG"}, "image/jpeg"),
            ({"mimeType": "image/webp"}, "image/webp"),
        ):
            self.assertEqual(BlueBubblesAdapter._image_type(attachment), expected, attachment)
        for attachment in ({"mimeType": "image/heic"}, {"mimeType": "audio/x-caf"}, {"transferName": "notes.pdf"}, None):
            self.assertIsNone(BlueBubblesAdapter._image_type(attachment), attachment)

    def test_a_picture_reaches_the_agent_alongside_the_message(self):
        seen = []
        self.adapter()._answer(lambda message: seen.append(message) or "ok", picture(text="what is this?"))

        self.assertEqual(seen[0].text, "what is this?")
        self.assertEqual(len(seen[0].images), 1)
        self.assertTrue(seen[0].images[0].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(seen[0].images[0].split(",", 1)[1]), b"fake-image-bytes")

    def test_a_picture_with_no_caption_still_gets_through(self):
        seen = []
        self.adapter()._answer(lambda message: seen.append(message) or "ok", picture())

        self.assertIn("picture", seen[0].text)
        self.assertEqual(len(seen[0].images), 1)

    def test_an_enormous_picture_is_left_behind(self):
        seen = []
        self.adapter()._answer(lambda message: seen.append(message) or "ok", picture(text="look", size=99_000_000))

        self.assertEqual(seen[0].images, ())

    def test_pictures_are_sent_to_the_model_as_image_parts(self):
        class Responses:
            def __init__(self):
                self.request = None

            def create(self, **kwargs):
                self.request = kwargs
                return SimpleNamespace(output=(), output_text="a red square")

        responses = Responses()
        provider = OpenAIProvider(client=SimpleNamespace(responses=responses), model="test-model")

        provider.complete(system="s", messages=[Message("user", "what is this?", images=("data:image/png;base64,AAA",))])

        content = responses.request["input"][0]["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "what is this?"})
        self.assertEqual(content[1], {"type": "input_image", "image_url": "data:image/png;base64,AAA"})

    def test_a_picture_is_uploaded_once_however_many_turns_follow(self):
        uploads = []

        class Files:
            def create(self, *, file, purpose):
                uploads.append(purpose)
                return SimpleNamespace(id="file-abc")

        class Responses:
            def __init__(self):
                self.requests = []

            def create(self, **kwargs):
                self.requests.append(kwargs)
                return SimpleNamespace(output=(), output_text="ok")

        responses = Responses()
        provider = OpenAIProvider(client=SimpleNamespace(responses=responses, files=Files()), model="m")
        url = "data:image/png;base64," + base64.b64encode(b"x" * 20_000).decode()
        conversation = [Message("user", "what is this?", images=(url,))]

        for _turn in range(4):
            provider.complete(system="s", messages=conversation)

        self.assertEqual(uploads, ["vision"])
        # The bytes go once; every turn after that carries only the reference.
        for request in responses.requests:
            self.assertEqual(request["input"][0]["content"][1], {"type": "input_image", "file_id": "file-abc"})
            self.assertLess(len(json.dumps(request["input"])), 500)

    def test_a_failed_upload_falls_back_to_sending_the_bytes(self):
        class Files:
            def create(self, **_kwargs):
                raise RuntimeError("upload is down")

        class Responses:
            def __init__(self):
                self.request = None

            def create(self, **kwargs):
                self.request = kwargs
                return SimpleNamespace(output=(), output_text="ok")

        responses = Responses()
        provider = OpenAIProvider(client=SimpleNamespace(responses=responses, files=Files()), model="m")

        provider.complete(system="s", messages=[Message("user", "look", images=("data:image/png;base64,AAA",))])

        self.assertEqual(
            responses.request["input"][0]["content"][1],
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
        )

    def test_pictures_are_visible_to_the_context_budget(self):
        plain = Message("user", "hello")
        with_picture = Message("user", "hello", images=("data:image/png;base64,AAA",))

        self.assertGreater(ConversationAgent._weight(with_picture), ConversationAgent._weight(plain) + 1000)
        # Before this they weighed nothing, so the overflow retry could not act.
        self.assertEqual(len(ConversationAgent._fit([with_picture, with_picture, with_picture], budget=5000)), 1)

    def test_an_image_that_will_not_resize_is_used_as_it_came(self):
        adapter = BlueBubblesAdapter(base_url="x", password="y")

        self.assertEqual(adapter._shrink(b"not really a png", "image/png"), b"not really a png")

    def test_a_message_without_pictures_keeps_its_plain_shape(self):
        class Responses:
            def __init__(self):
                self.request = None

            def create(self, **kwargs):
                self.request = kwargs
                return SimpleNamespace(output=(), output_text="fine")

        responses = Responses()
        OpenAIProvider(client=SimpleNamespace(responses=responses), model="test-model").complete(
            system="s", messages=[Message("user", "hello")]
        )

        self.assertEqual(responses.request["input"][0], {"role": "user", "content": "hello"})

    def test_the_app_hands_pictures_to_the_agent(self):
        got = {}

        class Provider:
            def complete(self, **kwargs):
                got["messages"] = kwargs["messages"]
                return ProviderResponse(text="seen it")

        with tempfile.TemporaryDirectory() as directory:
            app = BloomApp(ConversationAgent(Provider()), db_path=Path(directory) / "state.sqlite3")
            app.handle(InboundMessage("imessage", "+1555", "any;-;+1555", "look", images=("data:image/png;base64,AAA",)))

        self.assertEqual(got["messages"][-1].images, ("data:image/png;base64,AAA",))


if __name__ == "__main__":
    unittest.main()
