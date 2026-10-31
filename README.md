# FloodSense AI

A Forge hackathon project for an agentic geospatial flood-risk investigation system. The planned system will combine real weather forecasts, geographic data, and an LLM tool-calling agent to produce evidence-grounded risk assessments and preparedness recommendations.

## Current stage

The Streamlit scaffold provides latitude/longitude inputs and an **Investigate** button. The UI is not yet connected to data fetching and does not produce risk assessments. Separate Open-Meteo weather and OpenTopoData terrain services are available for future UI and agent integration. Maps and AI agents are not implemented.

FloodSense AI does not provide scientifically validated flood predictions.

## Run locally

Requires Python 3.11 or newer. From the project directory on macOS:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

Open the local URL printed by Streamlit. Stop the server with Control-C. No API keys are needed for this scaffold.

## Project structure

```text
app.py             Streamlit UI and coordinate input validation
services/weather.py Real Open-Meteo fetching, parsing, and precipitation totals
services/terrain.py Real OpenTopoData elevation fetching and parsing
analysis/terrain.py Descriptive local elevation differences
tests/test_weather.py Offline parser, calculation, and failure-handling tests
tests/test_terrain.py Offline terrain, geometry, and API failure tests
data/__init__.py   Reserved for real API and permitted dataset access
analysis/__init__.py Reserved for analysis logic
ai/__init__.py     Reserved for the future LLM agent
AGENTS.md          Development and hackathon rules
requirements.txt   Python dependencies
.gitignore         Excludes virtual environments, caches, and secrets
README.md          Project status and setup instructions
```

## Data integrity and security

- Use real APIs or permitted datasets; never fabricate observations.
- Never hardcode rainfall values or flood-risk results.
- Keep fetching, analysis, AI, and UI separate.
- Report API failures and missing data without inventing substitutes.
- Keep keys and credentials out of version control.
- Explain uncertainty and limitations; do not claim scientific validation.

An LLM tool-calling agent is future work.

## Weather service

```python
from services.weather import WeatherError, fetch_weather

try:
    evidence = fetch_weather(28.6139, 77.2090)
    total_mm = evidence["totals"]["24h"]["precipitation"]
    # None means unavailable, not zero rainfall.
except (ValueError, WeatherError) as error:
    print(f"Weather unavailable: {error}")
```

`fetch_weather(latitude, longitude, timeout=20)` validates numeric WGS84
coordinates and requests real hourly precipitation (mm) and precipitation
probability (%) from [Open-Meteo](https://open-meteo.com/en/docs). It returns a
JSON-serializable dictionary containing requested and model grid coordinates,
the exact API URL, documentation URL, retrieval timestamp, source units and
timezone metadata, hourly values, and 24/48-hour totals.

Open-Meteo precipitation timestamps mark the **end of the preceding hour**.
Windows begin at the next full UTC hour, excluding the current partial hour;
the first included timestamp is one hour after that boundary. At an exact hour,
the window begins immediately. Explicit UTC requests avoid daylight-saving
ambiguity. Each total includes its start/end timestamps and missing-hour count.
Source Unix timestamps are retained alongside ISO UTC timestamps. Retrieval
time is not a model initialization timestamp.

Null or absent hours remain unavailable. A total is `None` if any precipitation
hour in its window is missing. Missing probabilities do not invalidate rainfall
totals. Probabilities are not multiplied into precipitation amounts. Invalid
responses, API errors, and network failures raise `WeatherError`; there is no
fabricated fallback. These weather forecasts are not flood predictions.

Run offline tests (synthetic fixtures used only for unit tests):

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Terrain service

```python
from services.terrain import TerrainError, TerrainRateLimitError, fetch_terrain

try:
    terrain = fetch_terrain(28.6139, 77.2090, radius_m=250)
    elevation_m = terrain["points"][0]["elevation_m"]
    differences = terrain["relative_elevation"]
except TerrainRateLimitError as error:
    print(f"Rate limited; defer requests. Retry-After: {error.retry_after}")
except (ValueError, TerrainError) as error:
    print(f"Terrain unavailable: {error}")
```

`fetch_terrain(latitude, longitude, radius_m=250, timeout=20)` makes one real
[OpenTopoData](https://www.opentopodata.org/api/) request for the center plus
north/east/south/west samples. Distances use a mean-radius sphere, with longitudes
wrapped at the antimeridian. Radius must be positive and at most 5000 m.
The service uses `srtm90m`, bilinear interpolation, and explicit null NODATA.
No API key or additional dependencies are required.

The evidence dictionary preserves the exact request URL, source and dataset
links, requested/returned coordinates (EPSG:4326), units (metres), EGM96 vertical
datum, interpolation, sample radius, and UTC retrieval timestamp. Dataset/unit/
datum metadata is based on the selected SRTM product, not unit fields in the API
response. Retrieval time is not the terrain acquisition date.

`relative_elevation` contains **neighbor minus center**, in metres: a positive
value means that sample is higher than the selected point. Calculations in
`analysis/terrain.py` also return center minus the four-neighbor mean and the
five-point elevation range. These summaries require all five elevations; they
are `None` if any is missing. Individual differences remain available when both
members of a pair exist. Null elevations remain null; zero and below-zero
elevations are valid. Missing or mismatched response rows raise `TerrainError`.

SRTM has roughly 90 m sampling at the equator and limited geographic coverage;
out-of-coverage/NODATA results can be null. Very small radii may sample the same
raster cell, and interpolation may be rounded by the integer source raster.
Sparse elevation differences do not establish flow paths, flood depth, or flood
risk. This service does not perform hydrological modelling or provide
scientifically validated flood predictions.

### API usage and data licensing

- The [public service limits](https://www.opentopodata.org/#public-api) are
  100 locations per request, one call per second, and 1000 calls per day.
  This implementation batches five points and spaces request starts by at
  least 1.1 seconds within one Python process. Shared IP quotas across processes
  or deployments need caller-side coordination and caching. HTTP 429 raises
  `TerrainRateLimitError`, preserving `Retry-After` if provided; there is no
  automatic retry. Defer further calls after a rate limit, including daily
  exhaustion. Other API/network failures raise `TerrainError`, with no fallback.
- [OpenTopoData's SRTM documentation](https://www.opentopodata.org/datasets/srtm/)
  identifies the NASA/USGS SRTMGL3 source. Preserve attribution to OpenTopoData
  and NASA JPL/LP DAAC SRTMGL3 v003 when sharing outputs. Product reference:
  [NASA SRTM Global 3 arc second v003](https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL3.003).
- Data rights are separate from the API server's software license. NASA's
  [science-data licensing policy](https://science.data.nasa.gov/about/license)
  describes CC0 for NASA-led mission data absent a restrictive notice. Check
  the product metadata and any notices before redistributing source data;
  do not assume the same terms apply to every OpenTopoData dataset. Follow
  [USGS product citation guidance](https://www.usgs.gov/centers/eros/data-citation).
- [USGS SRTM specifications](https://www.usgs.gov/centers/eros/science/usgs-eros-archive-digital-elevation-shuttle-radar-topography-mission-srtm)
  document the vertical datum. Keep provenance and limitations with derived
  terrain summaries. No raster files or downloaded API responses are bundled.

The test command above runs both weather and terrain tests. Synthetic fixtures
are for offline unit tests only and are never substituted for API data.
