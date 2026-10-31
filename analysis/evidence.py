"""Aggregate real service evidence for future UI and LLM tool callers."""

from datetime import datetime, timezone

from services.weather import API_URL as WEATHER_URL
from services.weather import WeatherError, fetch_weather, validate_coordinates
from services.terrain import API_URL as TERRAIN_URL
from services.terrain import TerrainError, fetch_terrain


LIMITATIONS = (
    "Evidence-based screening only; not a validated flood prediction model.",
    "Weather forecasts and sparse historical terrain samples alone cannot establish flood risk or safety.",
    "No numerical flood probability, safe/unsafe classification, or official warning/alert is generated.",
    "Missing data is unknown, not zero. Retrieval timestamps are not model initialization or survey dates.",
    "Sources are retrieved separately and may represent different spatial resolutions and time periods.",
)


def _measurement_status(values):
    """None means unknown; zero and negative elevations remain measurements."""
    present = sum(value is not None for value in values)
    if not present:
        return "unavailable"
    return "available" if present == len(values) else "partial"


def _weather_status(data):
    values = [data["totals"][period]["precipitation"] for period in ("24h", "48h")]
    for row in data["hourly"]:
        values.extend((row["precipitation"], row["precipitation_probability"]))
    return _measurement_status(values)


def _terrain_status(data):
    values = [point["elevation_m"] for point in data["points"]]
    values.extend(point["neighbor_minus_center_m"] for point in data["relative_elevation"])
    values.extend((data["center_minus_neighbor_mean_m"], data["sample_elevation_range_m"]))
    return _measurement_status(values)


def _collect(latitude, longitude, *, name, endpoint, fetch, service_error, status_for):
    result = {
        "source": name, "endpoint_url": endpoint,
        "status": "unavailable", "data": None, "error": None,
        "warnings": [], "limitations": [],
    }
    try:
        data = fetch(latitude, longitude)
    except service_error as exc:
        result["error"] = {
            "type": type(exc).__name__, "message": str(exc),
            "retry_after": getattr(exc, "retry_after", None),
        }
        result["warnings"].append({
            "code": "source_error", "message": f"{name} evidence could not be retrieved.",
        })
        return result
    result["data"] = data
    result["status"] = status_for(data)
    result["limitations"] = [data["limitations"]]
    if result["status"] != "available":
        result["warnings"].append({
            "code": "missing_measurements",
            "message": f"{name} returned missing measurements; inspect null values and missing-data counts.",
        })
    return result


def build_evidence(latitude: float, longitude: float) -> dict:
    """Build schema_version '1.0' evidence without inferring a flood prediction.

    Tool inputs are numeric WGS84 degrees. Raises ValueError for invalid
    coordinates before calling either service. Calls each service exactly once
    with its defaults; no retries, fallback measurements, or cross-call caching.
    Reuse the returned report to avoid repeat requests for the same investigation.

    Returns JSON-serializable keys: schema_version, requested_coordinates,
    generated_at (UTC assembly time), status, weather, terrain, warnings,
    limitations. Each source has source, endpoint_url, status, data (unaltered
    service output or None), error, warnings, limitations. Actual query URLs,
    units, retrieval timestamps and forecast windows are retained within data.
    endpoint_url is service metadata, never a claimed successful retrieval URL.

    Source status is 'available' when all relevant measurements are present,
    'partial' when some are present, and 'unavailable' when none are present or
    retrieval failed. A successful all-null response retains data with error=None.
    Weather status covers both totals and hourly precipitation/probability;
    terrain status covers elevations, pair differences, and summary measurements.
    Report status is available only if both sources are available, unavailable
    if both are unavailable, otherwise partial. It describes evidence coverage,
    never safety or risk. Test nulls explicitly: 0 is a valid measurement.

    Expected WeatherError/TerrainError failures are isolated per source. Error
    dictionaries retain type, message and optional raw Retry-After. Unexpected
    programming/contract errors propagate rather than masquerading as API failure.
    See README for the stable nested access paths and warning/error schemas.
    """
    latitude, longitude = validate_coordinates(latitude, longitude)
    weather = _collect(latitude, longitude, name="Open-Meteo", endpoint=WEATHER_URL,
                       fetch=fetch_weather, service_error=WeatherError, status_for=_weather_status)
    terrain = _collect(latitude, longitude, name="OpenTopoData", endpoint=TERRAIN_URL,
                       fetch=fetch_terrain, service_error=TerrainError, status_for=_terrain_status)
    statuses = (weather["status"], terrain["status"])
    status = ("available" if all(value == "available" for value in statuses) else
              "unavailable" if all(value == "unavailable" for value in statuses) else "partial")
    warnings = [
        {"source": key, **warning}
        for key, source in (("weather", weather), ("terrain", terrain))
        for warning in source["warnings"]
    ]
    return {
        "schema_version": "1.0",
        "requested_coordinates": {"latitude": latitude, "longitude": longitude},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status, "weather": weather, "terrain": terrain,
        "warnings": warnings, "limitations": list(LIMITATIONS),
    }
