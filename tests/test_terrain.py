"""Synthetic test fixtures only; never used as real terrain evidence."""

from datetime import datetime, timezone
from http.client import IncompleteRead
from io import BytesIO
import json
import math
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from services.terrain import (
    DATASET, EARTH_RADIUS_M, TerrainError, TerrainRateLimitError,
    _pace_requests, fetch_terrain, parse_terrain, sample_locations,
)

LOCATIONS = sample_locations(28.6, 77.2)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fixture(elevations=(100, 110, 90, 105, 95)):
    return {"status": "OK", "results": [
        {"dataset": DATASET, "elevation": value,
         "location": {"lat": p["latitude"], "lng": p["longitude"]}}
        for p, value in zip(LOCATIONS, elevations)
    ]}


def parse(payload):
    return parse_terrain(payload, locations=LOCATIONS,
                         source_url="https://example.test/unit-fixture", fetched_at=NOW)


class TerrainParsingTests(unittest.TestCase):
    def test_calculations_and_provenance(self):
        result = parse(fixture())
        self.assertEqual([p["neighbor_minus_center_m"] for p in result["relative_elevation"]],
                         [10, -10, 5, -5])
        self.assertEqual(result["center_minus_neighbor_mean_m"], 0)
        self.assertEqual(result["sample_elevation_range_m"], 20)
        self.assertEqual(result["missing_points"], 0)
        self.assertEqual(result["source"], "OpenTopoData")
        self.assertEqual(result["dataset"], "srtm90m")
        self.assertEqual(result["elevation_unit"], "m")
        self.assertEqual(result["fetched_at"], NOW.isoformat())
        self.assertEqual(result["source_url"], "https://example.test/unit-fixture")
        self.assertEqual(result["points"][0]["returned_coordinates"],
                         {"latitude": 28.6, "longitude": 77.2})
        json.dumps(result, allow_nan=False)

    def test_center_minus_mean_sign(self):
        self.assertEqual(parse(fixture((50, 10, 20, 30, 40)))["center_minus_neighbor_mean_m"], 25)

    def test_null_center(self):
        result = parse(fixture((None, 10, 20, 30, 40)))
        self.assertTrue(all(p["neighbor_minus_center_m"] is None for p in result["relative_elevation"]))
        self.assertIsNone(result["center_minus_neighbor_mean_m"])
        self.assertIsNone(result["sample_elevation_range_m"])

    def test_partial_neighbors_preserve_valid_pairs(self):
        payload = fixture((100, None, 90, 105, 95))
        payload["results"][1]["dataset"] = None
        result = parse(payload)
        self.assertEqual(result["missing_points"], 1)
        self.assertIsNone(result["relative_elevation"][0]["neighbor_minus_center_m"])
        self.assertEqual(result["relative_elevation"][1]["neighbor_minus_center_m"], -10)
        self.assertIsNone(result["center_minus_neighbor_mean_m"])

    def test_zero_negative_and_all_missing(self):
        result = parse(fixture((0, -10, 0, 10, 20)))
        self.assertEqual(result["points"][0]["elevation_m"], 0)
        self.assertEqual(result["relative_elevation"][0]["neighbor_minus_center_m"], -10)
        self.assertEqual(parse(fixture((None,) * 5))["missing_points"], 5)

    def test_invalid_values_and_dataset(self):
        for value in (True, "12", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(TerrainError):
                parse(fixture((value, 10, 20, 30, 40)))
        payload = fixture()
        payload["results"][0]["dataset"] = "unrequested-dataset"
        with self.assertRaises(TerrainError):
            parse(payload)

    def test_malformed_and_api_errors(self):
        for payload in (None, [], {}, {"status": "SERVER_ERROR"},
                        {"status": "OK", "results": []}, {"status": "OK", "results": None}):
            with self.subTest(payload=payload), self.assertRaises(TerrainError):
                parse(payload)
        for mutation in (lambda p: p["results"].reverse(),
                         lambda p: p["results"][0].pop("elevation"),
                         lambda p: p["results"][0]["location"].update(lat=91)):
            payload = fixture()
            mutation(payload)
            with self.assertRaises(TerrainError):
                parse(payload)


class TerrainSamplingTests(unittest.TestCase):
    def test_cardinal_directions_and_distance(self):
        points = sample_locations(0, 0)
        self.assertEqual(len(points), 5)
        self.assertGreater(points[1]["latitude"], 0)
        self.assertGreater(points[2]["longitude"], 0)
        self.assertLess(points[3]["latitude"], 0)
        self.assertLess(points[4]["longitude"], 0)
        for point in points[1:]:
            distance = EARTH_RADIUS_M * math.acos(math.cos(math.radians(point["latitude"])) *
                                                 math.cos(math.radians(point["longitude"])))
            self.assertAlmostEqual(distance, 250, places=3)

    def test_antimeridian_and_poles(self):
        self.assertLess(sample_locations(0, 179.999)[2]["longitude"], 0)
        for lat in (-90, 90):
            for point in sample_locations(lat, 180):
                self.assertTrue(-90 <= point["latitude"] <= 90)
                self.assertTrue(-180 <= point["longitude"] <= 180)

    def test_invalid_inputs(self):
        for lat, lon in ((91, 0), (0, 181), (None, 0), (True, 0), (float("nan"), 0), (0, "1")):
            with self.assertRaises(ValueError):
                sample_locations(lat, lon)
        for radius in (0, -1, 5001, True, float("inf")):
            with self.assertRaises(ValueError):
                sample_locations(0, 0, radius_m=radius)


@patch("services.terrain._pace_requests")
@patch("services.terrain.urlopen")
class TerrainNetworkTests(unittest.TestCase):
    def test_single_real_endpoint_request(self, opener, pace):
        opener.return_value = BytesIO(json.dumps(fixture()).encode())
        result = fetch_terrain(28.6, 77.2)
        self.assertEqual(opener.call_count, 1)
        query = parse_qs(urlparse(result["source_url"]).query)
        self.assertEqual(len(query["locations"][0].split("|")), 5)
        self.assertEqual(query["nodata_value"], ["null"])
        self.assertEqual(query["interpolation"], ["bilinear"])
        self.assertEqual(result["sample_radius_m"], 250)
        self.assertTrue(result["source_url"].startswith("https://api.opentopodata.org/v1/srtm90m?"))

    def test_network_errors(self, opener, pace):
        for error in (URLError("offline"), TimeoutError(), IncompleteRead(b"partial"),
                      HTTPError("url", 503, "unavailable", {}, None)):
            opener.side_effect = error
            with self.assertRaises(TerrainError):
                fetch_terrain(0, 0)

    def test_rate_limit_no_retry(self, opener, pace):
        opener.side_effect = HTTPError("url", 429, "limited", {"Retry-After": "60"}, None)
        with self.assertRaises(TerrainRateLimitError) as caught:
            fetch_terrain(0, 0)
        self.assertEqual(caught.exception.retry_after, "60")
        opener.assert_called_once()

    def test_invalid_json(self, opener, pace):
        opener.return_value = BytesIO(b"not json")
        with self.assertRaises(TerrainError):
            fetch_terrain(0, 0)

    def test_invalid_input_never_fetches(self, opener, pace):
        for timeout in (0, True, float("nan")):
            with self.assertRaises(ValueError):
                fetch_terrain(0, 0, timeout=timeout)
        with self.assertRaises(ValueError):
            fetch_terrain(91, 0)
        opener.assert_not_called()
        pace.assert_not_called()


class TerrainPacingTests(unittest.TestCase):
    def test_process_local_pacing(self):
        with patch("services.terrain._last_request", 10), \
             patch("services.terrain.time.monotonic", side_effect=[10.2, 11.1]), \
             patch("services.terrain.time.sleep") as sleep:
            _pace_requests()
            self.assertAlmostEqual(sleep.call_args.args[0], 0.9)


if __name__ == "__main__":
    unittest.main()
