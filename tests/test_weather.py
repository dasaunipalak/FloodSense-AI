"""Synthetic unit-test inputs only: these are not real weather observations."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from http.client import IncompleteRead
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from services.weather import WeatherError, fetch_weather, parse_forecast, validate_coordinates

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fixture():
    return {
        "latitude": 28.6, "longitude": 77.2, "timezone": "GMT",
        "timezone_abbreviation": "GMT", "utc_offset_seconds": 0,
        "hourly_units": {"time": "unixtime", "precipitation": "mm",
                         "precipitation_probability": "%"},
        "hourly": {
            "time": [int((START + timedelta(hours=i)).timestamp()) for i in range(1, 49)],
            "precipitation": [0.1] * 24 + [0.2] * 24,
            "precipitation_probability": [50] * 48,
        },
    }


def parse(payload):
    return parse_forecast(payload, window_start=START, source_url="https://example.test/fixture",
                          fetched_at=START, coordinates=(28.6, 77.2))


class WeatherTests(unittest.TestCase):
    def test_coordinates(self):
        for pair in ((90, 180), (-90, -180), (0, 0)):
            self.assertEqual(validate_coordinates(*pair), pair)
        for pair in ((91, 0), (0, -181), (True, 0), ("28", 0), (None, 0),
                     (float("nan"), 0), (0, float("inf"))):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                validate_coordinates(*pair)

    def test_totals_and_provenance(self):
        result = parse(fixture())
        self.assertAlmostEqual(result["totals"]["24h"]["precipitation"], 2.4)
        self.assertAlmostEqual(result["totals"]["48h"]["precipitation"], 7.2)
        self.assertEqual(result["totals"]["48h"]["missing_hours"], 0)
        self.assertEqual(result["hourly"][0]["time"], "2026-01-01T01:00:00+00:00")
        self.assertEqual(result["source_units"]["precipitation_probability"], "%")
        self.assertEqual(result["timezone"], "GMT")
        self.assertEqual(result["utc_offset_seconds"], 0)
        self.assertEqual(result["source_url"], "https://example.test/fixture")
        self.assertEqual(result["fetched_at"], START.isoformat())
        json.dumps(result, allow_nan=False)

    def test_missing_rain_invalidates_only_affected_window(self):
        payload = fixture()
        payload["hourly"]["precipitation"][30] = None
        result = parse(payload)
        self.assertIsNotNone(result["totals"]["24h"]["precipitation"])
        self.assertIsNone(result["totals"]["48h"]["precipitation"])
        self.assertEqual(result["totals"]["48h"]["missing_hours"], 1)

    def test_missing_first_hour_invalidates_both_totals(self):
        payload = fixture()
        for values in payload["hourly"].values():
            values.pop(0)
        result = parse(payload)
        for period in ("24h", "48h"):
            self.assertIsNone(result["totals"][period]["precipitation"])
        self.assertIsNone(result["hourly"][0]["precipitation"])

    def test_missing_probability_does_not_change_rain_total(self):
        payload = fixture()
        payload["hourly"]["precipitation_probability"][0] = None
        self.assertAlmostEqual(parse(payload)["totals"]["24h"]["precipitation"], 2.4)

    def test_zero_is_real_value(self):
        payload = fixture()
        payload["hourly"]["precipitation"] = [0] * 48
        self.assertEqual(parse(payload)["totals"]["48h"]["precipitation"], 0)

    def test_malformed_responses(self):
        mutations = [
            lambda p: p["hourly"]["time"].reverse(),
            lambda p: p["hourly"]["time"].__setitem__(1, p["hourly"]["time"][0]),
            lambda p: p["hourly"]["precipitation"].pop(),
            lambda p: p["hourly"]["precipitation"].__setitem__(0, -1),
            lambda p: p["hourly"]["precipitation"].__setitem__(0, float("nan")),
            lambda p: p["hourly"]["precipitation_probability"].__setitem__(0, 101),
            lambda p: p["hourly_units"].__setitem__("precipitation", "inch"),
            lambda p: p.__setitem__("utc_offset_seconds", 3600),
            lambda p: p.pop("hourly"),
        ]
        for mutation in mutations:
            payload = deepcopy(fixture())
            mutation(payload)
            with self.subTest(mutation=mutation), self.assertRaises(WeatherError):
                parse(payload)
        for payload in (None, [], {"error": True}, {}):
            with self.subTest(payload=payload), self.assertRaises(WeatherError):
                parse(payload)

    @patch("services.weather.urlopen")
    def test_request_and_window_alignment(self, opener):
        opener.return_value = BytesIO(json.dumps(fixture()).encode())
        with patch("services.weather.datetime") as clock:
            clock.now.return_value = START + timedelta(minutes=15)
            result = fetch_weather(28.6, 77.2)
        query = parse_qs(urlparse(opener.call_args.args[0]).query)
        self.assertEqual(query["start_hour"], ["2026-01-01T02:00"])
        self.assertEqual(query["end_hour"], ["2026-01-03T01:00"])
        self.assertEqual(query["hourly"], ["precipitation,precipitation_probability"])
        self.assertEqual(result["totals"]["24h"]["window_start"], "2026-01-01T01:00:00+00:00")

    @patch("services.weather.urlopen")
    def test_failures_do_not_return_fallback(self, opener):
        for failure in (URLError("offline"), TimeoutError(), IncompleteRead(b"partial"),
                        HTTPError("https://example.test", 429, "limited", {}, None)):
            opener.side_effect = failure
            with self.subTest(failure=type(failure)), self.assertRaises(WeatherError):
                fetch_weather(0, 0)
        opener.side_effect = None
        opener.return_value = BytesIO(b"not JSON")
        with self.assertRaises(WeatherError):
            fetch_weather(0, 0)

    @patch("services.weather.urlopen")
    def test_invalid_inputs_never_fetch(self, opener):
        with self.assertRaises(ValueError):
            fetch_weather(100, 0)
        for timeout in (0, -1, float("nan"), True):
            with self.assertRaises(ValueError):
                fetch_weather(0, 0, timeout=timeout)
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
