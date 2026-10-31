# FloodSense AI

A Forge hackathon project for an agentic geospatial flood-risk investigation system. The planned system will combine real weather forecasts, geographic data, and an LLM tool-calling agent to produce evidence-grounded risk assessments and preparedness recommendations.

## Current stage

The Streamlit scaffold provides latitude/longitude inputs and an **Investigate** button. The UI is not yet connected to weather fetching and does not produce risk assessments. A separate Open-Meteo weather service is available for future UI and agent integration. Terrain analysis, maps, and AI agents are not implemented.

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
tests/test_weather.py Offline parser, calculation, and failure-handling tests
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

OpenTopoData and an LLM tool-calling agent are future work.

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
