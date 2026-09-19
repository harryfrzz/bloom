import json
import unittest

from tools.location import NetworkLocation


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class NetworkLocationTests(unittest.TestCase):
    def test_a_place_is_reported_with_its_uncertainty(self):
        place = NetworkLocation(
            opener=lambda request, timeout=None: Response(
                {"city": "Kanayannur", "region": "Kerala", "country": "IN", "loc": "9.9,76.2", "timezone": "Asia/Kolkata"}
            )
        )

        described = place.describe()

        self.assertIn("Kanayannur, Kerala, IN", described)
        self.assertIn("Asia/Kolkata", described)
        # The caller must never present this as where the person actually is.
        self.assertIn("city-level", described)

    def test_the_answer_is_cached_rather_than_looked_up_each_time(self):
        calls = []

        def opener(request, timeout=None):
            calls.append(1)
            return Response({"city": "Kochi", "region": "Kerala", "country": "IN"})

        place = NetworkLocation(opener=opener)
        place.describe()
        place.describe()

        self.assertEqual(len(calls), 1)

    def test_a_failed_lookup_asks_rather_than_raising(self):
        def broken(request, timeout=None):
            raise RuntimeError("Connection error.")

        answer = NetworkLocation(opener=broken).tool().handler({})

        self.assertIn("Ask the user", answer)


if __name__ == "__main__":
    unittest.main()
