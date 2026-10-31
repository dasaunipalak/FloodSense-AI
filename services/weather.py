"""Real Open-Meteo hourly forecasts; no flood prediction or fallback data.

Precipitation is the amount for the hour *ending* at its timestamp. Windows
start at the next full UTC hour (or now when exactly on an hour boundary).
"""

from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import json
import math
from numbers import Real
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

API_URL = "https://api.open-meteo.com/v1/forecast"
DOCS_URL = "https://open-meteo.com/en/docs"
UTC = timezone.utc


class WeatherError(Exception):
    """Forecast unavailable because of a network, API, or data format error."""


def validate_coordinates(latitude, longitude):
    """Return finite WGS84 floats; reject booleans, strings, and out-of-range values."""
    for name, value, limit in (("latitude", latitude, 90), ("longitude", longitude, 180)):
        if (isinstance(value, bool) or not isinstance(value, Real)
                or not math.isfinite(value) or not -limit <= value <= limit):
            raise ValueError(f"{name} must be a finite number between {-limit} and {limit}")
    return float(latitude), float(longitude)


def _iso(value):
    return value.astimezone(UTC).isoformat()


def _amount(value, maximum=None):
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not math.isfinite(value) or value < 0
            or (maximum is not None and value > maximum)):
        raise WeatherError("Invalid precipitation or probability value in API response")
    return float(value)


def parse_forecast(payload, *, window_start, source_url, fetched_at, coordinates):
    """Parse an API response and calculate strict 24/48-hour totals.

    Missing/null hours are preserved as None. A total is None unless every
    precipitation value in its window is available. Missing probability does
    not invalidate a precipitation total. Malformed data raises WeatherError.
    This pure parser is also usable for reprocessing saved real responses.
    """
    if window_start.tzinfo is None or fetched_at.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    window_start = window_start.astimezone(UTC)
    if window_start.minute or window_start.second or window_start.microsecond:
        raise ValueError("window_start must be a full UTC hour")
    try:
        if not isinstance(payload, dict):
            raise WeatherError("Open-Meteo response must be a JSON object")
        if payload.get("error"):
            raise WeatherError("Open-Meteo returned an API error")
        units = payload["hourly_units"]
        if (units["time"] != "unixtime" or units["precipitation"] != "mm"
                or units["precipitation_probability"] != "%"
                or payload["utc_offset_seconds"] != 0):
            raise WeatherError("Unexpected API units or UTC offset")
        if not isinstance(payload["timezone"], str) or not payload["timezone"]:
            raise WeatherError("Missing API timezone")
        hourly = payload["hourly"]
        times = hourly["time"]
        rain = hourly["precipitation"]
        probability = hourly["precipitation_probability"]
        if not all(isinstance(values, list) for values in (times, rain, probability)):
            raise WeatherError("Hourly fields must be arrays")
        if not len(times) == len(rain) == len(probability):
            raise WeatherError("Hourly arrays have unequal lengths")
        by_time = {}
        previous = None
        for stamp, amount, chance in zip(times, rain, probability):
            if (isinstance(stamp, bool) or not isinstance(stamp, Real)
                    or not math.isfinite(stamp) or stamp % 3600
                    or (previous is not None and stamp <= previous)):
                raise WeatherError("Invalid or unordered hourly timestamps")
            previous = stamp
            by_time[stamp] = (_amount(amount), _amount(chance, 100))
        rows = []
        for hour in range(1, 49):
            end = window_start + timedelta(hours=hour)
            amount, chance = by_time.get(end.timestamp(), (None, None))
            rows.append({"time": _iso(end),
                         "source_time": int(end.timestamp()) if end.timestamp() in by_time else None,
                         "precipitation": amount,
                         "precipitation_probability": chance})
        totals = {}
        for hours in (24, 48):
            values = [row["precipitation"] for row in rows[:hours]]
            missing = sum(value is None for value in values)
            totals[f"{hours}h"] = {
                "precipitation": None if missing else math.fsum(values),
                "unit": "mm", "missing_hours": missing,
                "window_start": _iso(window_start),
                "window_end": _iso(window_start + timedelta(hours=hours)),
            }
        grid_lat, grid_lon = validate_coordinates(payload["latitude"], payload["longitude"])
        return {
            "source": "Open-Meteo", "source_url": source_url,
            "documentation_url": DOCS_URL, "fetched_at": _iso(fetched_at),
            "requested_coordinates": dict(zip(("latitude", "longitude"), coordinates)),
            "grid_coordinates": {"latitude": grid_lat, "longitude": grid_lon},
            "timezone": payload["timezone"],
            "timezone_abbreviation": payload["timezone_abbreviation"],
            "utc_offset_seconds": payload["utc_offset_seconds"],
            "source_units": dict(units),
            "hourly_units": {"time": "ISO 8601 UTC", "precipitation": "mm",
                             "precipitation_probability": "%"},
            "hourly": rows, "totals": totals,
            "limitations": "Weather forecast only; not a scientifically validated flood prediction.",
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise WeatherError("Malformed Open-Meteo forecast response") from exc


def fetch_weather(latitude, longitude, *, timeout=20):
    """Fetch a real forecast and return a JSON-serializable evidence dictionary.

    Call as ``fetch_weather(latitude, longitude)`` from UI or agent tools.
    Coordinates are numeric WGS84 degrees. No API key is required. Uses UTC
    to avoid daylight-saving ambiguity. Each hourly timestamp ends its interval.
    Totals cover 24/48 full hours starting at the next UTC hour, excluding the
    current partial hour. Null/absent precipitation makes the affected total
    None; inspect missing_hours. Probabilities are percentages, not weights.
    Raises ValueError for invalid inputs and WeatherError for unavailable or
    malformed responses. The caller must display unavailability, not a risk
    result. fetched_at is retrieval time, not a model initialization timestamp.
    """
    coordinates = validate_coordinates(latitude, longitude)
    if isinstance(timeout, bool) or not isinstance(timeout, Real) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    now = datetime.now(UTC)
    start = now.replace(minute=0, second=0, microsecond=0)
    if start < now:
        start += timedelta(hours=1)
    params = {
        "latitude": coordinates[0], "longitude": coordinates[1],
        "hourly": "precipitation,precipitation_probability",
        "precipitation_unit": "mm", "timezone": "GMT", "timeformat": "unixtime",
        "start_hour": (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end_hour": (start + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M"),
    }
    source_url = API_URL + "?" + urlencode(params)
    try:
        with urlopen(source_url, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise WeatherError(f"Open-Meteo HTTP error {exc.code}") from exc
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise WeatherError("Unable to retrieve Open-Meteo forecast; check network availability") from exc
    except (ValueError, UnicodeError) as exc:
        raise WeatherError("Open-Meteo returned invalid JSON") from exc
    return parse_forecast(payload, window_start=start, source_url=source_url,
                          fetched_at=datetime.now(UTC), coordinates=coordinates)
