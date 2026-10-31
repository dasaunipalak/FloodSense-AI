"""Real evidence tools with investigation-local caching, including failures."""

from copy import deepcopy
from datetime import datetime, timezone
import json

from analysis.evidence import LIMITATIONS, build_evidence
from services.weather import API_URL as WEATHER_URL
from services.weather import WeatherError, fetch_weather, validate_coordinates
from services.terrain import API_URL as TERRAIN_URL
from services.terrain import TerrainError, fetch_terrain
from ai.contracts import TOOL_NAMES, strict_json


def _validated_coordinates(latitude, longitude):
    try:
        return validate_coordinates(latitude, longitude)
    except OverflowError:
        raise ValueError("Coordinates must be finite WGS84 numbers.") from None


def measurement_status(kind, data):
    """Match evidence schema v1 coverage semantics; never treat zero as missing."""
    if kind == "weather":
        values = [data["totals"][period]["precipitation"] for period in ("24h", "48h")]
        for row in data["hourly"]:
            values.extend((row["precipitation"], row["precipitation_probability"]))
    else:
        values = [point["elevation_m"] for point in data["points"]]
        values.extend(point["neighbor_minus_center_m"] for point in data["relative_elevation"])
        values.extend((data["center_minus_neighbor_mean_m"], data["sample_elevation_range_m"]))
    present = sum(value is not None for value in values)
    return "unavailable" if not present else "available" if present == len(values) else "partial"


def _validate_entry(entry):
    """Check the boundary before caching so malformed data cannot poison results."""
    if entry["status"] not in ("available", "partial", "unavailable"):
        raise ValueError("Invalid evidence availability")
    if entry["data"] is not None:
        for key in ("source", "source_url", "documentation_url", "fetched_at"):
            if not isinstance(entry["data"][key], str) or not entry["data"][key]:
                raise ValueError("Missing evidence provenance")
    json.dumps(entry, allow_nan=False)


class EvidenceTools:
    """One instance per investigation. No monkeypatching or persistent cache."""

    def __init__(self, latitude, longitude):
        self.coordinates = _validated_coordinates(latitude, longitude)
        self.cache = {}
        self.combined = None

    def validate(self, name, arguments):
        if name not in TOOL_NAMES:
            raise ValueError("Unsupported tool name.")
        if not isinstance(arguments, str) or len(arguments) > 2000:
            raise ValueError("Tool arguments must be a small JSON object.")
        try:
            args = strict_json(arguments)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("Malformed tool arguments.") from None
        if not isinstance(args, dict) or set(args) != {"latitude", "longitude"}:
            raise ValueError("Exactly latitude and longitude are required.")
        coordinates = _validated_coordinates(args["latitude"], args["longitude"])
        if coordinates != self.coordinates:
            raise ValueError("Tool coordinates must match the user's selected location.")
        return dict(zip(("latitude", "longitude"), coordinates))

    def _individual(self, kind):
        if kind in self.cache:
            return self.cache[kind]
        source, endpoint, fetch, error_type = (
            ("Open-Meteo", WEATHER_URL, fetch_weather, WeatherError) if kind == "weather"
            else ("OpenTopoData", TERRAIN_URL, fetch_terrain, TerrainError)
        )
        entry = {"source": source, "endpoint_url": endpoint, "status": "unavailable",
                 "data": None, "error": None, "warnings": [], "limitations": []}
        try:
            data = fetch(*self.coordinates)
        except error_type as exc:
            entry["error"] = {"type": type(exc).__name__,
                              "message": f"{source} evidence could not be retrieved.",
                              "retry_after": getattr(exc, "retry_after", None)}
            entry["warnings"].append({"code": "source_error", "message": entry["error"]["message"]})
        else:
            entry["status"] = measurement_status(kind, data)
            entry["data"] = data
            entry["limitations"] = [data["limitations"]]
            if entry["status"] != "available":
                entry["warnings"].append({"code": "missing_measurements", "message":
                    f"{source} returned missing measurements; inspect null values and missing-data counts."})
        _validate_entry(entry)
        self.cache[kind] = entry
        return entry

    def get_weather_evidence(self, latitude, longitude):
        self._check_location(latitude, longitude)
        return deepcopy(self._individual("weather"))

    def get_terrain_evidence(self, latitude, longitude):
        self._check_location(latitude, longitude)
        return deepcopy(self._individual("terrain"))

    def _check_location(self, latitude, longitude):
        if _validated_coordinates(latitude, longitude) != self.coordinates:
            raise ValueError("Tool coordinates must match the user's selected location.")

    def get_combined_evidence(self, latitude, longitude):
        self._check_location(latitude, longitude)
        if self.combined is None:
            if not self.cache:
                # Palak's unmodified aggregator is the cold-cache entry point.
                package = deepcopy(build_evidence(*self.coordinates))
                json.dumps(package, allow_nan=False)
                for kind in ("weather", "terrain"):
                    _validate_entry(package[kind])
                self.cache.update({kind: package[kind] for kind in ("weather", "terrain")})
                # Do not pass arbitrary exception text to the model or caller.
                for entry in self.cache.values():
                    if entry["error"] is not None:
                        entry["error"]["message"] = f"{entry['source']} evidence could not be retrieved."
                self.combined = package
            else:
                # build_evidence has no pre-fetched inputs. Reassemble its public
                # v1 contract instead of refetching or mutating its module globals.
                weather = self._individual("weather")
                terrain = self._individual("terrain")
                statuses = (weather["status"], terrain["status"])
                status = ("available" if all(s == "available" for s in statuses) else
                          "unavailable" if all(s == "unavailable" for s in statuses) else "partial")
                self.combined = {
                    "schema_version": "1.0", "requested_coordinates": dict(zip(
                        ("latitude", "longitude"), self.coordinates)),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "status": status, "weather": weather, "terrain": terrain,
                    "warnings": [{"source": kind, **warning} for kind, entry in self.cache.items()
                                 for warning in entry["warnings"]],
                    "limitations": list(LIMITATIONS),
                }
        return deepcopy(self.combined)

    def references(self):
        references = []
        for kind, entry in self.cache.items():
            data = entry["data"]
            if data is not None:
                references.append({"id": kind, "source": data["source"],
                                   "source_url": data["source_url"],
                                   "documentation_url": data["documentation_url"],
                                   "fetched_at": data["fetched_at"],
                                   "status": entry["status"]})
        return references

    def execute(self, name, arguments):
        args = self.validate(name, arguments)
        needed = ("weather", "terrain") if name == TOOL_NAMES[2] else (
            "weather" if name == TOOL_NAMES[0] else "terrain",)
        reused = [kind for kind in needed if kind in self.cache]
        data = getattr(self, name)(**args)  # name has been restricted to TOOL_NAMES
        return {"status": data["status"], "evidence": data,
                "reused_sources": reused, "source_references": self.references()}
