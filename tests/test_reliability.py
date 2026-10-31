"""Offline integration tests: synthetic HTTP fixtures never enter production.

Exercise real fetchers, parsers, analysis, evidence aggregation, and dashboard.
Only HTTP responses/clocks/pacing are mocked; outbound connections are blocked.
"""

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from analysis.evidence import build_evidence
from streamlit.testing.v1 import AppTest

NOW = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)
APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def weather_response(url):
    query = parse_qs(urlparse(url).query)
    first = datetime.fromisoformat(query["start_hour"][0]).replace(tzinfo=timezone.utc)
    return {
        "latitude": float(query["latitude"][0]), "longitude": float(query["longitude"][0]),
        "timezone": "GMT", "timezone_abbreviation": "GMT", "utc_offset_seconds": 0,
        "hourly_units": {"time": "unixtime", "precipitation": "mm", "precipitation_probability": "%"},
        "hourly": {
            "time": [int((first + timedelta(hours=i)).timestamp()) for i in range(48)],
            "precipitation": [0.25] * 24 + [0.5] * 24,
            "precipitation_probability": [50] * 48,
        },
    }


def terrain_response(url):
    locations = parse_qs(urlparse(url).query)["locations"][0].split("|")
    return {"status": "OK", "results": [
        {"location": dict(zip(("lat", "lng"), map(float, location.split(",")))),
         "dataset": "srtm90m", "elevation": elevation}
        for location, elevation in zip(locations, (0, -10, 10, 5, -5))
    ]}


class OfflineTransportCase(unittest.TestCase):
    def setUp(self):
        # A broken/missing transport mock must fail, never fall through to live HTTP.
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("Live network forbidden")))
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("Unmocked HTTP forbidden")))
        self.weather_http = self.enterContext(patch("services.weather.urlopen", side_effect=self.weather_ok))
        self.terrain_http = self.enterContext(patch("services.terrain.urlopen", side_effect=self.terrain_ok))
        self.enterContext(patch("services.terrain._pace_requests"))
        for module in ("services.weather", "services.terrain"):
            clock = self.enterContext(patch(f"{module}.datetime"))
            clock.now.return_value = NOW

    def weather_ok(self, url, **kwargs):
        return BytesIO(json.dumps(weather_response(url)).encode())

    def terrain_ok(self, url, **kwargs):
        return BytesIO(json.dumps(terrain_response(url)).encode())

    def assert_json(self, report):
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)


class EvidenceReliabilityTests(OfflineTransportCase):
    def test_both_succeed_with_real_parsers_and_analysis(self):
        report = build_evidence(28.6, 77.2)
        self.assertEqual(report["status"], "available")
        self.assertEqual(report["weather"]["data"]["totals"]["24h"]["precipitation"], 6)
        self.assertEqual(report["weather"]["data"]["totals"]["48h"]["precipitation"], 18)
        self.assertEqual([r["neighbor_minus_center_m"] for r in report["terrain"]["data"]["relative_elevation"]],
                         [-10, 10, 5, -5])
        self.weather_http.assert_called_once()
        self.terrain_http.assert_called_once()
        self.assert_json(report)

    def test_weather_timeout_keeps_terrain(self):
        self.weather_http.side_effect = TimeoutError("synthetic timeout")
        report = build_evidence(0, 0)
        self.assertEqual(report["status"], "partial")
        self.assertIsNone(report["weather"]["data"])
        self.assertEqual(report["weather"]["error"]["type"], "WeatherError")
        self.assertEqual(report["terrain"]["status"], "available")
        self.weather_http.assert_called_once_with(self.weather_http.call_args.args[0], timeout=20)
        self.assert_json(report)

    def test_terrain_timeout_keeps_weather(self):
        self.terrain_http.side_effect = TimeoutError("synthetic timeout")
        report = build_evidence(0, 0)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["weather"]["status"], "available")
        self.assertIsNone(report["terrain"]["data"])
        self.assertEqual(report["terrain"]["error"]["type"], "TerrainError")
        self.terrain_http.assert_called_once_with(self.terrain_http.call_args.args[0], timeout=20)
        self.assert_json(report)

    def test_both_time_out_without_measurement_fallback(self):
        self.weather_http.side_effect = TimeoutError()
        self.terrain_http.side_effect = TimeoutError()
        report = build_evidence(0, 0)
        self.assertEqual(report["status"], "unavailable")
        for name in ("weather", "terrain"):
            self.assertIsNone(report[name]["data"])
            self.assertIsNotNone(report[name]["error"])
        self.assertEqual({w["source"] for w in report["warnings"]}, {"weather", "terrain"})
        self.assertNotIn("flood_probability", report)
        self.weather_http.assert_called_once()
        self.terrain_http.assert_called_once()
        self.assert_json(report)

    def test_terrain_rate_limit_retains_retry_after_without_retry(self):
        self.terrain_http.side_effect = HTTPError("url", 429, "limited", {"Retry-After": "120"}, None)
        report = build_evidence(0, 0)
        self.assertEqual(report["terrain"]["error"]["type"], "TerrainRateLimitError")
        self.assertEqual(report["terrain"]["error"]["retry_after"], "120")
        self.assertEqual(report["weather"]["status"], "available")
        self.terrain_http.assert_called_once()
        self.assert_json(report)

    def test_weather_rate_limit_is_explicit_not_a_fallback(self):
        self.weather_http.side_effect = HTTPError("url", 429, "limited", {"Retry-After": "60"}, None)
        report = build_evidence(0, 0)
        self.assertEqual(report["weather"]["error"]["type"], "WeatherError")
        self.assertIn("429", report["weather"]["error"]["message"])
        self.assertIsNone(report["weather"]["data"])
        self.assertEqual(report["terrain"]["status"], "available")
        # Existing weather interface does not expose Retry-After; do not invent it.
        self.assertIsNone(report["weather"]["error"]["retry_after"])
        self.weather_http.assert_called_once()

    def test_missing_and_zero_survive_full_pipeline(self):
        def weather_missing(url, **kwargs):
            data = weather_response(url)
            data["hourly"]["precipitation"][30] = None
            data["hourly"]["precipitation"][0] = 0
            data["hourly"]["precipitation_probability"][0] = None
            return BytesIO(json.dumps(data).encode())

        def terrain_missing(url, **kwargs):
            data = terrain_response(url)
            data["results"][1]["elevation"] = None
            return BytesIO(json.dumps(data).encode())

        self.weather_http.side_effect = weather_missing
        self.terrain_http.side_effect = terrain_missing
        report = build_evidence(0, 0)
        weather, terrain = report["weather"]["data"], report["terrain"]["data"]
        self.assertEqual(report["weather"]["status"], "partial")
        self.assertEqual(report["terrain"]["status"], "partial")
        self.assertEqual(weather["hourly"][0]["precipitation"], 0)
        self.assertIsNone(weather["hourly"][0]["precipitation_probability"])
        self.assertEqual(weather["totals"]["24h"]["precipitation"], 5.75)
        self.assertIsNone(weather["totals"]["48h"]["precipitation"])
        self.assertEqual(weather["totals"]["48h"]["missing_hours"], 1)
        self.assertEqual(terrain["points"][0]["elevation_m"], 0)
        self.assertIsNone(terrain["points"][1]["elevation_m"])
        self.assertIsNone(terrain["relative_elevation"][0]["neighbor_minus_center_m"])
        self.assertEqual(terrain["relative_elevation"][1]["neighbor_minus_center_m"], 10)
        self.assertIsNone(terrain["center_minus_neighbor_mean_m"])
        self.assert_json(report)

    def test_empty_weather_and_null_terrain_are_not_zero(self):
        def weather_empty(url, **kwargs):
            data = weather_response(url)
            data["hourly"] = {key: [] for key in data["hourly"]}
            return BytesIO(json.dumps(data).encode())

        def terrain_null(url, **kwargs):
            data = terrain_response(url)
            for row in data["results"]:
                row.update(elevation=None, dataset=None)
            return BytesIO(json.dumps(data).encode())

        self.weather_http.side_effect = weather_empty
        self.terrain_http.side_effect = terrain_null
        report = build_evidence(0, 0)
        self.assertEqual(report["status"], "unavailable")
        for name in ("weather", "terrain"):
            self.assertIsNone(report[name]["error"])
            self.assertIsNotNone(report[name]["data"])
        self.assertEqual(report["weather"]["data"]["totals"]["48h"]["missing_hours"], 48)
        self.assertIsNone(report["weather"]["data"]["totals"]["24h"]["precipitation"])
        self.assertEqual(report["terrain"]["data"]["missing_points"], 5)
        self.assert_json(report)

    def test_invalid_coordinates_never_reach_transport(self):
        for lat, lon in ((90.01, 0), (-90.01, 0), (0, 180.01), (0, -180.01),
                         (True, 0), (0, False), ("0", 0), (None, 0),
                         (float("nan"), 0), (0, float("inf"))):
            with self.subTest(lat=lat, lon=lon), self.assertRaises(ValueError):
                build_evidence(lat, lon)
        self.weather_http.assert_not_called()
        self.terrain_http.assert_not_called()

    def test_coordinate_boundaries_remain_valid_requests(self):
        for lat, lon in ((90, 180), (-90, -180), (0, 0)):
            with self.subTest(lat=lat, lon=lon):
                report = build_evidence(lat, lon)
                self.assertEqual(report["requested_coordinates"], {"latitude": lat, "longitude": lon})
                self.assertEqual(report["status"], "available")
                self.assert_json(report)
        # Fixtures verify software boundaries, not real SRTM coverage at poles.
        self.assertEqual(self.weather_http.call_count, 3)
        self.assertEqual(self.terrain_http.call_count, 3)

    def test_provenance_units_and_forecast_windows_are_preserved(self):
        report = build_evidence(28.6, 77.2)
        weather, terrain = report["weather"]["data"], report["terrain"]["data"]
        self.assertEqual(weather["source_url"], self.weather_http.call_args.args[0])
        self.assertEqual(terrain["source_url"], self.terrain_http.call_args.args[0])
        self.assertEqual(weather["fetched_at"], NOW.isoformat())
        self.assertEqual(terrain["fetched_at"], NOW.isoformat())
        self.assertEqual(weather["source_units"]["precipitation"], "mm")
        self.assertEqual(weather["hourly_units"]["precipitation_probability"], "%")
        self.assertEqual(weather["timezone"], "GMT")
        self.assertEqual(weather["utc_offset_seconds"], 0)
        self.assertEqual(terrain["elevation_unit"], "m")
        self.assertEqual(terrain["dataset"], "srtm90m")
        for hours in (24, 48):
            total = weather["totals"][f"{hours}h"]
            self.assertEqual(total["unit"], "mm")
            start, end = map(datetime.fromisoformat, (total["window_start"], total["window_end"]))
            self.assertEqual(start, NOW.replace(minute=0) + timedelta(hours=1))
            self.assertEqual(end - start, timedelta(hours=hours))
        self.assertEqual(weather["hourly"][0]["source_time"],
                         int(datetime(2026, 1, 1, 2, tzinfo=timezone.utc).timestamp()))
        self.assert_json(report)

    def test_invalid_payloads_do_not_generate_measurements(self):
        self.weather_http.side_effect = lambda *a, **k: BytesIO(b"invalid json")
        self.terrain_http.side_effect = lambda *a, **k: BytesIO(b'{"status":"SERVER_ERROR"}')
        report = build_evidence(0, 0)
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["weather"]["data"])
        self.assertIsNone(report["terrain"]["data"])
        self.assert_json(report)


class DashboardReliabilityTests(OfflineTransportCase):
    def submit(self):
        # Cold pandas/pydeck imports can exceed AppTest's 3-second default.
        app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.weather_http.assert_not_called()
        self.terrain_http.assert_not_called()
        app.number_input[0].set_value(0.0)
        app.number_input[1].set_value(0.0)
        app.button[0].click().run()
        self.assertFalse(app.exception)
        return app

    def test_success_and_rerun_do_not_duplicate_requests(self):
        app = self.submit()
        self.assertTrue(any(metric.value == "0.00 m" for metric in app.metric))
        app.run()
        self.assertFalse(app.exception)
        self.weather_http.assert_called_once()
        self.terrain_http.assert_called_once()

    def test_weather_failure_still_renders_terrain(self):
        self.weather_http.side_effect = TimeoutError()
        app = self.submit()
        self.assertTrue(any("Weather evidence unavailable" in error.value for error in app.error))
        self.assertTrue(any(metric.label == "Selected point elevation" for metric in app.metric))

    def test_terrain_rate_limit_still_renders_weather(self):
        self.terrain_http.side_effect = HTTPError("url", 429, "limited", {}, None)
        app = self.submit()
        self.assertTrue(any("rate limit" in error.value for error in app.error))
        self.assertTrue(any(metric.label == "Next 24h rainfall" for metric in app.metric))

    def test_both_failures_show_errors_without_measurement_metrics(self):
        self.weather_http.side_effect = TimeoutError()
        self.terrain_http.side_effect = TimeoutError()
        app = self.submit()
        self.assertEqual(len(app.error), 2)
        self.assertEqual(len(app.metric), 0)


if __name__ == "__main__":
    unittest.main()
