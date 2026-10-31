"""Mocked service responses for unit tests only; never live user evidence."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from analysis.evidence import build_evidence
from services.weather import WeatherError, parse_forecast
from services.terrain import TerrainError, TerrainRateLimitError, parse_terrain, sample_locations

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def weather_fixture():
    """Use the actual parser to enforce the current service output contract."""
    return parse_forecast({
        "latitude": 0.0, "longitude": 0.0, "timezone": "GMT",
        "timezone_abbreviation": "GMT", "utc_offset_seconds": 0,
        "hourly_units": {"time": "unixtime", "precipitation": "mm", "precipitation_probability": "%"},
        "hourly": {
            "time": [int((NOW + timedelta(hours=i)).timestamp()) for i in range(1, 49)],
            "precipitation": [0.0] * 48, "precipitation_probability": [0.0] * 48,
        },
    }, window_start=NOW, source_url="https://example.test/weather-fixture",
        fetched_at=NOW, coordinates=(0.0, 0.0))


def terrain_fixture():
    locations = sample_locations(0, 0)
    result = parse_terrain({"status": "OK", "results": [
        {"dataset": "srtm90m", "location": {"lat": p["latitude"], "lng": p["longitude"]},
         "elevation": elevation}
        for p, elevation in zip(locations, (0, -10, 10, 0, 0))
    ]}, locations=locations, source_url="https://example.test/terrain-fixture", fetched_at=NOW)
    result["sample_radius_m"] = 250.0
    return result


@patch("analysis.evidence.fetch_terrain")
@patch("analysis.evidence.fetch_weather")
class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.weather = weather_fixture()
        self.terrain = terrain_fixture()

    def build(self, weather, terrain):
        weather.return_value = self.weather
        terrain.return_value = self.terrain
        return build_evidence(0, 0)

    def test_success_preserves_complete_outputs_without_mutation(self, weather, terrain):
        before_weather, before_terrain = deepcopy(self.weather), deepcopy(self.terrain)
        result = self.build(weather, terrain)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["requested_coordinates"], {"latitude": 0.0, "longitude": 0.0})
        self.assertEqual(result["weather"]["data"], before_weather)
        self.assertEqual(result["terrain"]["data"], before_terrain)
        self.assertEqual(self.weather, before_weather)
        self.assertEqual(self.terrain, before_terrain)
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(result["weather"]["error"])
        self.assertEqual(result["weather"]["limitations"], [self.weather["limitations"]])
        self.assertEqual(result["terrain"]["limitations"], [self.terrain["limitations"]])
        self.assertEqual(datetime.fromisoformat(result["generated_at"]).utcoffset(), timedelta(0))
        weather.assert_called_once_with(0.0, 0.0)
        terrain.assert_called_once_with(0.0, 0.0)
        json.dumps(result, allow_nan=False)

    def test_weather_failure_preserves_terrain(self, weather, terrain):
        weather.side_effect = WeatherError("Weather API unavailable")
        result = self.build(weather, terrain)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["weather"]["status"], "unavailable")
        self.assertIsNone(result["weather"]["data"])
        self.assertEqual(result["weather"]["error"]["type"], "WeatherError")
        self.assertEqual(result["weather"]["error"]["message"], "Weather API unavailable")
        self.assertEqual(result["terrain"]["data"], self.terrain)
        terrain.assert_called_once()

    def test_terrain_failure_preserves_weather(self, weather, terrain):
        terrain.side_effect = TerrainError("Terrain API unavailable")
        result = self.build(weather, terrain)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["terrain"]["status"], "unavailable")
        self.assertIsNone(result["terrain"]["data"])
        self.assertEqual(result["weather"]["data"], self.weather)
        weather.assert_called_once()

    def test_both_failures(self, weather, terrain):
        weather.side_effect = WeatherError("offline")
        terrain.side_effect = TerrainError("offline")
        result = build_evidence(0, 0)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(result["warnings"]), 2)
        for source in ("weather", "terrain"):
            self.assertIsNone(result[source]["data"])
            self.assertEqual(result[source]["status"], "unavailable")
        weather.assert_called_once()
        terrain.assert_called_once()
        json.dumps(result, allow_nan=False)

    def test_rate_limit_preserves_retry_after(self, weather, terrain):
        terrain.side_effect = TerrainRateLimitError("60")
        result = self.build(weather, terrain)
        self.assertEqual(result["terrain"]["error"]["retry_after"], "60")
        self.assertEqual(result["terrain"]["error"]["type"], "TerrainRateLimitError")
        terrain.assert_called_once()

    def test_invalid_coordinates_do_not_fetch(self, weather, terrain):
        for lat, lon in ((91, 0), (0, 181), (True, 0), (None, 0), ("1", 0),
                         (float("nan"), 0), (0, float("inf"))):
            with self.subTest(lat=lat, lon=lon), self.assertRaises(ValueError):
                build_evidence(lat, lon)
        weather.assert_not_called()
        terrain.assert_not_called()

    def test_zero_and_negative_values_are_available(self, weather, terrain):
        result = self.build(weather, terrain)
        self.assertEqual(result["weather"]["status"], "available")
        self.assertEqual(result["weather"]["data"]["totals"]["48h"]["precipitation"], 0)
        self.assertEqual(result["terrain"]["data"]["points"][0]["elevation_m"], 0)
        self.assertEqual(result["terrain"]["data"]["points"][1]["elevation_m"], -10)

    def test_partial_weather_keeps_available_total(self, weather, terrain):
        self.weather["totals"]["48h"].update(precipitation=None, missing_hours=1)
        self.weather["hourly"][30]["precipitation"] = None
        result = self.build(weather, terrain)
        self.assertEqual(result["weather"]["status"], "partial")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["weather"]["data"]["totals"]["24h"]["precipitation"], 0)
        self.assertIsNone(result["weather"]["data"]["totals"]["48h"]["precipitation"])
        self.assertEqual(result["warnings"][0]["code"], "missing_measurements")

    def test_null_probability_is_not_zero(self, weather, terrain):
        self.weather["hourly"][0]["precipitation_probability"] = None
        result = self.build(weather, terrain)
        self.assertEqual(result["weather"]["status"], "partial")
        self.assertIsNone(result["weather"]["data"]["hourly"][0]["precipitation_probability"])

    def test_partial_terrain_preserves_pairs(self, weather, terrain):
        self.terrain["points"][1]["elevation_m"] = None
        self.terrain["relative_elevation"][0]["neighbor_minus_center_m"] = None
        self.terrain.update(missing_points=1, center_minus_neighbor_mean_m=None, sample_elevation_range_m=None)
        result = self.build(weather, terrain)
        self.assertEqual(result["terrain"]["status"], "partial")
        self.assertEqual(result["terrain"]["data"], self.terrain)

    def test_all_null_data_is_retained_without_claiming_availability(self, weather, terrain):
        for total in self.weather["totals"].values():
            total.update(precipitation=None)
        self.weather["totals"]["24h"]["missing_hours"] = 24
        self.weather["totals"]["48h"]["missing_hours"] = 48
        for row in self.weather["hourly"]:
            row.update(precipitation=None, precipitation_probability=None)
        for point in self.terrain["points"]:
            point["elevation_m"] = None
        for pair in self.terrain["relative_elevation"]:
            pair["neighbor_minus_center_m"] = None
        self.terrain.update(missing_points=5, center_minus_neighbor_mean_m=None, sample_elevation_range_m=None)
        result = self.build(weather, terrain)
        self.assertEqual(result["status"], "unavailable")
        for source in ("weather", "terrain"):
            self.assertEqual(result[source]["status"], "unavailable")
            self.assertIsNotNone(result[source]["data"])
            self.assertIsNone(result[source]["error"])
        json.dumps(result, allow_nan=False)

    def test_unexpected_programming_errors_are_not_disguised(self, weather, terrain):
        weather.side_effect = RuntimeError("programming error")
        with self.assertRaises(RuntimeError):
            build_evidence(0, 0)


if __name__ == "__main__":
    unittest.main()
