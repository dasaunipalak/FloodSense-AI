"""Small real SRTM terrain samples via OpenTopoData, independent of UI/agents."""

from datetime import datetime, timezone
from http.client import HTTPException
import json
import math
from numbers import Real
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from analysis.terrain import summarize_elevations

API_URL = "https://api.opentopodata.org/v1/srtm90m"
DATASET_URL = "https://www.opentopodata.org/datasets/srtm/"
DATASET = "srtm90m"
EARTH_RADIUS_M = 6_371_008.8
_request_lock = Lock()
_last_request = None


class TerrainError(Exception):
    """Elevation unavailable due to transport, API, or malformed data errors."""


class TerrainRateLimitError(TerrainError):
    """Rate limited: no automatic retry. retry_after preserves the HTTP header."""

    def __init__(self, retry_after=None):
        super().__init__("OpenTopoData rate limit reached; defer requests and retry later")
        self.retry_after = retry_after


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _coordinates(latitude, longitude):
    if not _finite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("latitude must be a finite number between -90 and 90")
    if not _finite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("longitude must be a finite number between -180 and 180")
    return float(latitude), float(longitude)


def sample_locations(latitude, longitude, *, radius_m=250):
    """Center plus N/E/S/W at radius_m (0 < radius <= 5000), on a sphere.

    Returns WGS84 points with longitude wrapped at the antimeridian. Distances
    are approximate spherical distances, not ellipsoidal survey measurements.
    """
    latitude, longitude = _coordinates(latitude, longitude)
    if not _finite(radius_m) or not 0 < radius_m <= 5000:
        raise ValueError("radius_m must be a finite number greater than 0 and at most 5000")
    points = [{"label": "center", "latitude": latitude, "longitude": longitude}]
    lat, lon = math.radians(latitude), math.radians(longitude)
    arc = radius_m / EARTH_RADIUS_M
    for label, degrees in (("north", 0), ("east", 90), ("south", 180), ("west", 270)):
        bearing = math.radians(degrees)
        # Rotate the Earth-centered unit vector using local north/east vectors.
        # This is stable even when the selected point lies exactly at a pole.
        x, y, z = math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)
        north = (-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat))
        east = (-math.sin(lon), math.cos(lon), 0)
        dest = [math.cos(arc) * v + math.sin(arc) *
                (math.cos(bearing) * n + math.sin(bearing) * e)
                for v, n, e in zip((x, y, z), north, east)]
        new_lat = math.degrees(math.atan2(dest[2], math.hypot(dest[0], dest[1])))
        new_lon = (math.degrees(math.atan2(dest[1], dest[0])) + 180) % 360 - 180
        points.append({"label": label, "latitude": new_lat, "longitude": new_lon})
    return points


def parse_terrain(payload, *, locations, source_url, fetched_at):
    """Validate a five-point response, preserving nulls and computing differences.

    Differences are neighbor minus center, in metres. Summary statistics require
    all four neighbors and the center; partial pairs remain individually usable.
    Missing/extra rows or mismatched coordinates are malformed, not zero terrain.
    """
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    if len(locations) != 5 or [p["label"] for p in locations] != ["center", "north", "east", "south", "west"]:
        raise ValueError("Expected center and four cardinal sample locations")
    try:
        if not isinstance(payload, dict) or payload.get("status") != "OK":
            raise TerrainError("OpenTopoData returned an unsuccessful or malformed API status")
        results = payload["results"]
        if not isinstance(results, list) or len(results) != len(locations):
            raise TerrainError("OpenTopoData returned an unexpected number of elevation results")
        points = []
        for requested, result in zip(locations, results):
            returned_lat, returned_lon = _coordinates(result["location"]["lat"], result["location"]["lng"])
            lon_gap = abs((returned_lon - requested["longitude"] + 180) % 360 - 180)
            if abs(returned_lat - requested["latitude"]) > 1e-6 or lon_gap > 1e-6:
                raise TerrainError("Returned coordinates do not match the requested sample order")
            elevation = result["elevation"]
            if elevation is not None and not _finite(elevation):
                raise TerrainError("Invalid elevation value")
            dataset = result["dataset"]
            if dataset != DATASET and not (dataset is None and elevation is None):
                raise TerrainError("Unexpected elevation dataset")
            points.append({
                **requested, "returned_coordinates": {"latitude": returned_lat, "longitude": returned_lon},
                "elevation_m": None if elevation is None else float(elevation),
                "dataset": dataset,
            })
        return {
            "source": "OpenTopoData", "source_url": source_url,
            "documentation_url": "https://www.opentopodata.org/api/",
            "dataset": DATASET, "dataset_url": DATASET_URL,
            "elevation_unit": "m", "vertical_datum": "EGM96 geoid",
            "coordinate_reference_system": "EPSG:4326",
            "interpolation": "bilinear", "nominal_resolution_m": 90,
            "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
            "points": points, **summarize_elevations(points),
            "limitations": "Sparse, interpolated historical SRTM elevations only. "
                "Relative elevation alone does not predict flooding or drainage; "
                "this is not a scientifically validated flood prediction.",
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise TerrainError("Malformed OpenTopoData elevation response") from exc


def _pace_requests():
    """Space request starts by 1.1 seconds within this process only."""
    global _last_request
    with _request_lock:
        now = time.monotonic()
        if _last_request is not None:
            time.sleep(max(0, 1.1 - (now - _last_request)))
        _last_request = time.monotonic()


def fetch_terrain(latitude, longitude, *, radius_m=250, timeout=20):
    """Fetch real SRTM90m elevation for a center and four nearby sample points.

    UI/agent entry point: fetch_terrain(latitude, longitude). Returns a JSON-safe
    evidence dictionary with source URL, dataset, metre units, requested/returned
    WGS84 coordinates, UTC retrieval timestamp, nulls, and relative differences.
    radius_m defaults to 250 m; distances use a sphere. Negative/zero elevations
    are valid; missing elevations are never replaced. SRTM coverage is limited.
    Uses one five-location request with bilinear interpolation and null NODATA.
    Raises ValueError for invalid input, TerrainError for unavailable/malformed
    responses, and TerrainRateLimitError for HTTP 429 (with raw Retry-After).
    No automatic retries. Process-local pacing cannot enforce shared IP/day
    quotas; callers must cache/reuse results and defer after a rate-limit error.
    fetched_at is retrieval time, not terrain survey date. No API key required.
    """
    locations = sample_locations(latitude, longitude, radius_m=radius_m)
    if not _finite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    params = {"locations": "|".join(f'{p["latitude"]},{p["longitude"]}' for p in locations),
              "interpolation": "bilinear", "nodata_value": "null"}
    source_url = API_URL + "?" + urlencode(params)
    _pace_requests()
    try:
        with urlopen(source_url, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 429:
            raise TerrainRateLimitError(exc.headers.get("Retry-After") if exc.headers else None) from exc
        raise TerrainError(f"OpenTopoData HTTP error {exc.code}") from exc
    except (URLError, OSError, HTTPException) as exc:
        raise TerrainError("Unable to retrieve OpenTopoData elevations; check network availability") from exc
    except (ValueError, UnicodeError) as exc:
        raise TerrainError("OpenTopoData returned invalid JSON") from exc
    evidence = parse_terrain(payload, locations=locations, source_url=source_url,
                             fetched_at=datetime.now(timezone.utc))
    evidence["sample_radius_m"] = float(radius_m)
    return evidence
