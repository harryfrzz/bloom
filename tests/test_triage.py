import unittest

from channels.bluebubbles import BlueBubblesAdapter


class WaitingOnYouTests(unittest.TestCase):
    def test_short_codes_and_gateways_are_not_people(self):
        for identifier in ("JioMart", "Kotak811", "+9157575782(smsft)", "instamart@botplatform.sarcs.jio.com"):
            self.assertFalse(BlueBubblesAdapter._is_a_person(identifier, "hello"), identifier)

    def test_a_real_number_or_personal_email_is_a_person(self):
        self.assertTrue(BlueBubblesAdapter._is_a_person("+16507096528", "call me back"))
        self.assertTrue(BlueBubblesAdapter._is_a_person("friend@gmail.com", "you free?"))

    def test_automated_texts_are_not_someone_waiting(self):
        for text in (
            "023269 is your OTP. Do not share it with anyone",
            "Dear Customer, You have a missed call from +91...",
            "Your order has been delivered",
            "Get 50% cashback, T&C apply",
        ):
            self.assertFalse(BlueBubblesAdapter._is_a_person("+919995646211", text), text)

    def test_a_person_saying_something_real_survives_the_filter(self):
        self.assertTrue(
            BlueBubblesAdapter._is_a_person("+16507096528", "No rush. Send me something annoying you'd rather not deal with")
        )

    def test_only_unanswered_and_recent_conversations_come_back(self):
        import json
        import time

        now = time.time() * 1000

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_exception):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        chats = {
            "data": [
                {"guid": "a", "chatIdentifier": "+16507096528", "lastMessage": {"text": "you free?", "isFromMe": False, "dateCreated": now - 3_600_000}},
                {"guid": "b", "chatIdentifier": "+919995646212", "lastMessage": {"text": "already answered", "isFromMe": True, "dateCreated": now}},
                {"guid": "c", "chatIdentifier": "JioMart", "lastMessage": {"text": "offer inside", "isFromMe": False, "dateCreated": now}},
                {"guid": "d", "chatIdentifier": "+919995646213", "lastMessage": {"text": "old question", "isFromMe": False, "dateCreated": now - 60 * 86_400_000}},
            ]
        }

        def opener(request, timeout=None):
            return Response(chats if request.data else {"data": []})

        waiting = BlueBubblesAdapter(base_url="https://server.example", password="y", opener=opener).awaiting_reply(days=14)

        self.assertEqual([row["who"] for row in waiting], ["+16507096528"])


if __name__ == "__main__":
    unittest.main()
