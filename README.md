# FloodSense AI

A Forge hackathon project for an agentic geospatial flood-risk investigation system. The planned system will combine real weather forecasts, geographic data, and an LLM tool-calling agent to produce evidence-grounded risk assessments and preparedness recommendations.

## Current stage

The Streamlit dashboard displays live Open-Meteo forecasts, OpenTopoData terrain samples, and an interactive sample map. A separate LLM investigation module under `ai/` selects real evidence tools through Gemini native function calling by default, or OpenAI Responses function calling when explicitly selected. The Streamlit sidebar now includes a separate Run AI Investigation button. Only an explicit click calls the existing agent; results and traces are retained in session state and labeled with their investigated coordinates. Coordinate edits mark older AI results. Provider failures do not discard dashboard evidence. Neither component calculates a flood-risk score or official warning.

FloodSense AI does not provide scientifically validated flood predictions.

An isolated NDMA SACHET RSS/CAP alert service is documented in
[docs/SACHET.md](docs/SACHET.md). It is not connected to the dashboard or AI
agent. Geographic applicability remains unresolved unless verified by a
supported inline CAP polygon; no alert/no verified match does not imply safety.

## Run locally

Requires Python 3.11 or newer. From the project directory on macOS:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
python -m streamlit run app.py
```

Open the local URL printed by Streamlit. Stop the server with Control-C. No API keys or application-specific environment variables are required for the dashboard. The separate agent uses the Google Gen AI SDK by default, with the OpenAI SDK available as an alternative; configure the selected provider as documented below. Services and evidence analysis use the Python standard library. See [DEPLOYMENT.md](DEPLOYMENT.md) for setup, standalone-agent configuration, secret handling, troubleshooting, and deployment limitations.

## Project structure

```text
app.py             Live weather/terrain evidence dashboard and sample-location map
services/weather.py Real Open-Meteo fetching, parsing, and precipitation totals
services/terrain.py Real OpenTopoData elevation fetching and parsing
analysis/terrain.py Descriptive local elevation differences
analysis/evidence.py Combined weather/terrain evidence and availability reporting
tests/test_weather.py Offline parser, calculation, and failure-handling tests
tests/test_terrain.py Offline terrain, geometry, and API failure tests
tests/test_evidence.py Mocked aggregation, failure isolation, and missing-data tests
data/__init__.py   Reserved for real API and permitted dataset access
analysis/__init__.py Reserved for analysis logic
ai/agent.py        Bounded LLM-selected tool loop and structured report validation
ai/provider.py     Provider selection, root dotenv loading, OpenAI Responses adapter
ai/gemini.py       Google Gen AI native function-calling adapter
ai/tools.py        Real evidence tools and investigation-local cache
ai/contracts.py    Strict schemas and evidence-grounding system instruction
ai/__main__.py     Standalone agent CLI (no Streamlit integration)
tests/test_agent.py Offline agent, tool-cache, and SDK adapter tests
tests/test_gemini.py Offline Gemini, provider selection, and SDK transport tests
AGENTS.md          Development and hackathon rules
requirements.txt   Python dependencies
.gitignore         Excludes virtual environments, caches, and secrets
README.md          Project status and setup instructions
DEPLOYMENT.md      Local/Cloud deployment, validation, and operational guidance
```

## Data integrity and security

- Use real APIs or permitted datasets; never fabricate observations.
- Never hardcode rainfall values or flood-risk results.
- Keep fetching, analysis, AI, and UI separate.
- Report API failures and missing data without inventing substitutes.
- Keep keys and credentials out of version control.
- Explain uncertainty and limitations; do not claim scientific validation.

The agent remains a separate module, reused by the dashboard and standalone CLI. A controlled live investigation with `gemini-3.1-flash-lite` successfully requested weather and terrain tools, retrieved real evidence, and produced a source-grounded report with a matching trace. This verifies that run, not every future model response.

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

## Evidence aggregation

```python
import json
from analysis.evidence import build_evidence

try:
    report = build_evidence(28.6139, 77.2090)
    print(json.dumps(report, allow_nan=False))
except ValueError as error:
    print(f"Invalid coordinates: {error}")
```

`build_evidence(latitude, longitude)` accepts numeric WGS84 coordinates and
validates them before any network request. It calls each existing service once,
using its defaults, and preserves each returned dictionary without modification.
It does not retry or cache across calls. Reuse one report throughout an
investigation to avoid duplicate API requests. Normal service timeouts apply;
requests are sequential, and source retrieval times may differ.

The JSON-serializable report uses stable **schema version `1.0`**:

| Key | Meaning |
| --- | --- |
| `schema_version` | Schema identifier, currently `"1.0"`. |
| `requested_coordinates` | Numeric `latitude` and `longitude` in WGS84 degrees. |
| `generated_at` | ISO 8601 UTC assembly time; source retrieval times stay in source data. |
| `status` | Evidence coverage: `available`, `partial`, or `unavailable`. Never a risk level. |
| `weather`, `terrain` | Source entries described below. |
| `warnings` | List of `{source, code, message}` entries; `source` is `weather` or `terrain`. These are data-quality messages, not official weather/emergency alerts. |
| `limitations` | List of scientific and interpretation limitations that always accompanies the report. |

Each source entry has the same keys:

| Key | Meaning |
| --- | --- |
| `source` | `Open-Meteo` or `OpenTopoData`. |
| `endpoint_url` | Known service endpoint; not a claim of successful retrieval. |
| `status` | `available` if all relevant measurements exist; `partial` if some exist; `unavailable` if none exist or the request failed. |
| `data` | Complete original service result, or `null` after a service error. |
| `error` | `null` or `{type, message, retry_after}`; `retry_after` is the raw rate-limit header when available, otherwise `null`. |
| `warnings` | List of `{code, message}` objects. Codes are `source_error` and `missing_measurements`. |
| `limitations` | Original service limitation as a list, or an empty list when retrieval failed. Global limitations still apply. |

Overall status is `available` only when both sources are fully available,
`unavailable` when both are unavailable, and `partial` otherwise. A successful
all-null response is preserved under `data`, with `status="unavailable"` and
`error=null`. That differs from an API failure, where `data=null` and `error`
describes the failure. Missing precipitation probability makes weather partial
even if its rainfall totals are available. Missing terrain pairs or summary
measurements also make terrain partial. Zero measurements remain available.

Use the original service field names inside `data`:

| Evidence | Access path |
| --- | --- |
| Rainfall totals, units, missing-hour counts, UTC window boundaries | `report["weather"]["data"]["totals"]["24h"]` and `["48h"]`; each contains `precipitation`, `unit`, `missing_hours`, `window_start`, `window_end`. |
| Hourly precipitation/probability and timestamps | `report["weather"]["data"]["hourly"]`. |
| Weather units and time zone | Weather `data.hourly_units`, `data.source_units`, `data.timezone`, `data.timezone_abbreviation`, `data.utc_offset_seconds`. |
| Elevation measurements and coordinates | `report["terrain"]["data"]["points"]`, using each point's `elevation_m`, coordinates, and `dataset`. |
| Relative elevation differences | Terrain `data.relative_elevation`, `data.center_minus_neighbor_mean_m`, and `data.sample_elevation_range_m`. |
| Terrain units, dataset, and sampling metadata | Terrain `data.elevation_unit`, `data.dataset`, `data.dataset_url`, `data.vertical_datum`, `data.sample_radius_m`. |
| Exact request URL and retrieval time | Each source's `data.source_url` and `data.fetched_at`; documentation links and other original metadata are also retained. |

Check `data is not None` before accessing these paths, and test measurements with
`is None` rather than truthiness. Expected `WeatherError`/`TerrainError` failures
are reported independently: one failure never discards the other source. If
both fail, the report contains metadata/errors and no invented measurements.
Unexpected programming or service-contract errors propagate for diagnosis.

This is an evidence-based screening tool, not a validated flood prediction
model. It generates no numerical flood probability, safe/unsafe label, or
official warning. Preserve the report's limitations, source attribution, and
missing-data information when passing it to an LLM or displaying results.

The existing test command also discovers the new agent tests. Evidence and agent
tests mock responses only inside offline unit tests. Production tools always
call real services or reuse results retrieved in the same investigation.

## AI investigation agent

Install `requirements.txt` in the project virtual environment. Provider selection
uses `LLM_PROVIDER=gemini` or `LLM_PROVIDER=openai`; Gemini is the default when
the variable is absent. The maintained `google-genai` SDK (`>=1.75,<2`) provides
[Gemini native function calling](https://ai.google.dev/gemini-api/docs/generate-content/function-calling).
The deprecated `google-generativeai` package is not used. OpenAI support uses the
official `openai` SDK and its
[Responses function-calling API](https://developers.openai.com/api/docs/guides/function-calling).

Create or edit `.env` in the repository root using `.env.example` as a template.
Preserve any existing local credentials. For Gemini, fill in only the real
`GEMINI_API_KEY` locally and choose an accessible `GEMINI_MODEL`:

```dotenv
LLM_PROVIDER=gemini
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.1-flash-lite
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
```

The model names above are configuration examples, not hardcoded runtime defaults.
The selected provider requires its corresponding key and model. Never paste
keys into chat, command arguments, logs, or Git. `.env` and `.env.*` are ignored;
only the sanitized `.env.example` is trackable. Streamlit secrets are not read
by this standalone CLI.

`python-dotenv` loads the exact repository-root `.env` before provider selection,
regardless of shell directory. Existing process variables take precedence;
an empty process key will still mask a nonempty `.env` key and cause a
configuration error. Unset that empty variable in your local shell if needed.
The loader never prints configuration values. `OPENAI_BASE_URL` remains rejected
by the OpenAI adapter.

Windows PowerShell, from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m ai 29.2183 79.5130
```

macOS/Linux, from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m ai 29.2183 79.5130
```

These coordinates are an example location, not observational data. Each run
contacts the selected LLM and whichever real evidence services it chooses.
Selected coordinates, task text, and retrieved evidence are sent to the selected
provider. The SDKs make no automatic retries, use a 30-second request timeout,
and permit at most 4,000 output tokens per response. Account quotas, model
availability, billing, and provider data-use policies apply. Free-tier access is
not guaranteed; a quota error terminates the investigation without switching
providers. The verified model is `gemini-3.1-flash-lite`. Google lists a free tier for this model, but model listing or a successful request does not establish your project's free-tier eligibility, remaining quota, or billing status. Check Google AI Studio before running investigations.

For an explicit OpenAI fallback, set `LLM_PROVIDER=openai` in `.env` and supply
`OPENAI_API_KEY` and `OPENAI_MODEL`. The chosen model must support Responses
function calling and strict
[structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
OpenAI requests continue using `store=False`; that is not a zero-retention
guarantee. There is no automatic fallback to a paid provider on errors.

Programmatic entry point:

```python
from ai.agent import investigate

result = investigate(latitude, longitude)
if result["agent_status"] == "completed":
    report = result["final_report"]
```

`task=` optionally focuses the investigation, up to 4,000 characters. Invalid
caller coordinates/tasks raise `ValueError` before any network operation.
Missing configuration produces `agent_status="configuration_error"`; no
evidence APIs are called and no substitute report is invented.

### Architecture and workflow

1. The selected coordinates are validated and pinned in strict tool schemas.
2. The LLM chooses from `get_weather_evidence`, `get_terrain_evidence`, and
   `get_combined_evidence`. There is no fixed Python sequence that calls all
   three tools. Tool arguments are validated again locally, including exact
   coordinate equality, allowed names, finite numbers, and no extra keys.
3. Real tool outputs return through each provider's native protocol: Gemini
   `FunctionResponse` parts or OpenAI `function_call_output` items. The Gemini
   adapter normalizes calls into the existing loop's transport contract and
   retains full original Google model content privately, preserving thought
   signatures and native call IDs across rounds. Locally assigned correlation
   IDs identify actual Gemini calls even when the API omits an ID. The public
   trace contains requested tools and results, not purported internal reasoning.
4. The model can inspect missingness, choose another source, compare findings,
   and decide whether the evidence supports a limited final explanation.
5. Final JSON must include investigation summary, weather findings, terrain
   findings, evidence gaps, preparedness considerations, limitations, and the
   exact retrieved source IDs. Code attaches canonical source URLs and retrieval
   timestamps; invented/omitted IDs and generated web links are rejected.

Google SDK automatic function execution is explicitly disabled; the shared
loop alone dispatches tools and enforces budgets. Gemini uses `AUTO` native
function calling, so the model decides which tools to invoke. The adapter
configuration does not combine tools with structured-output mode: tool-enabled
turns receive the report schema in instructions and final JSON is checked by the
existing validator. A final-only turn after tool exhaustion uses Google's
structured JSON schema mode with no tools. Malformed/empty final text is an
error, never replaced with a prewritten report.

Each investigation has its own in-memory cache. Repeated tools reuse successful,
partial, all-null, failed, and rate-limited results without retries. A combined
call on an empty cache invokes Palak's unchanged `build_evidence` and seeds both
source caches. When individual results already exist, the combined tool fetches
only the missing source and assembles the public evidence-v1 shape. This is
necessary because `build_evidence` has no pre-fetched-input parameter. It does
not monkeypatch service globals or change the analysis module. Contract tests
compare the assembled shape with the real aggregator. The cache is discarded
after each investigation; forecasts are never persistently cached.

The loop permits at most **5 tool-call rounds** and **6 dispatch attempts**;
cache hits and invalid dispatch attempts count toward the latter. A combined
tool is one dispatch but can request two upstream services. There can be one
final-only model response after the tool budget is exhausted (at most 6 model
requests total). Calls beyond a limit are recorded as blocked, not executed.
Service timeouts remain those of the existing modules. API failures are data
gaps; unexpected service/schema errors terminate the investigation safely.

### Result contract and limitations

The return value is JSON-serializable and includes:

- `selected_coordinates`, `agent_status`, and `final_report` (null on failure).
- `tools_used` and `tool_call_trace`: actual LLM-requested calls, arguments,
  availability/rejection/error status, results, and reused sources.
- `evidence`: original source data in availability envelopes, including nulls.
- `evidence_source_references`: canonical retrieved URLs and timestamps only;
  a failed request does not acquire a fabricated retrieval reference.
- `errors`, `warnings`, `tool_rounds`, and `tool_executions`.

Statuses are `completed`, `configuration_error`, `provider_error`,
`invalid_response`, `refused`, `tool_error`, or `limit_reached`. `completed`
means a structurally valid explanation was generated, not that evidence is
complete or that a location is safe. Both APIs can fail and the model can still
produce an explanation of those gaps. Always display errors, warnings, and
source availability alongside a report.

The system instruction prohibits fabricated measurements, flood probabilities,
official warnings, safe/unsafe labels, and unverified evacuation routes.
Preparedness is general and refers users to local official advisories. Schema
and citation checks do **not** prove that every sentence generated by an LLM is
factually correct or safe. Human review and live model evaluations remain
necessary before integrating or deploying this agent. There is no hydrological
model, routing engine, official advisory feed, or flood-risk score.

Run all offline tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Agent tests use synthetic evidence and scripted model responses only inside
`tests/test_agent.py`. They exercise dispatch, schema/coordinate validation,
missing measurements, independent/both-source failures, rate limits, caching,
loop bounds, provider errors, JSON serialization, and native SDK request wiring.
`tests/test_gemini.py` additionally checks provider selection, native call parsing,
actual result responses, multiple rounds and parallel calls, thought-signature
preservation, trace accuracy, caching, validation, source references, loop limits,
and sanitized key/quota/network/timeout errors. An offline HTTP transport verifies
the real Google SDK's request/response serialization without contacting Gemini.
The original OpenAI tests remain unchanged. Live Gemini execution has been verified once with `gemini-3.1-flash-lite`; offline tests do not make model requests. Further live evaluations require explicit authorization.

A live report used an imprecise relative timing description. The system instruction now requires explicit evidence timestamps and timezone for timing claims instead of unsupported early/middle/late labels. Offline coverage verifies that the instruction reaches Gemini and that hourly evidence is preserved; it cannot guarantee model compliance.

### Windows offline test harness

On Windows/Python 3.11, the existing dashboard tests patch all socket connects.
This also blocks the loopback socket pair used by asyncio during Streamlit
AppTest startup, before dashboard code executes. The standard command currently
reports four failures for this reason. To run all assertions with the socket
guard unchanged, pre-create event loops before loading the test suite:

```powershell
@'
import asyncio
import unittest

loops = [asyncio.new_event_loop() for _ in range(24)]
pool = iter(loops)
original = asyncio.new_event_loop
asyncio.new_event_loop = lambda: next(pool)
try:
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
finally:
    asyncio.new_event_loop = original
    for loop in loops:
        if not loop.is_closed():
            loop.close()
raise SystemExit(not result.wasSuccessful())
'@ | .\.venv\Scripts\python.exe -B -
```

Validation: all 133 tests passed with this harness; the standard Windows command
passed 129 and failed the four AppTest startup cases. No test assertions or
network guards were removed. The portable test-runner issue remains unresolved.


## Official Disaster Alerts and agent tool

The dashboard's **Refresh Official Alerts** button retrieves the existing NDMA
SACHET service for the selected coordinates. It is independent of weather/terrain
refresh and AI investigation; initial load and ordinary reruns do not fetch alerts.
Availability, matching status, evaluation time, issuer, event, severity, effective/
expiry timestamps, source links, warnings and geographic limitations are displayed.
Only verified active matching sections receive local-alert cards. Other parsed
records remain explicitly labeled as not a local warning list.

Retained results are snapshots, not continuous monitoring. Coordinate edits label
older snapshots. Cards are withheld after five minutes or on expiry at a rerun;
there is no background timer or automatic refresh. An explicit refresh reuses the
backend's process-local TTL/ETag cache and reevaluates lifecycle using its public
API. Changes at the source may be unseen until revalidation. Do not rely on a
retained dashboard or AI report for current emergency decisions.

The provider-neutral `get_official_alerts` tool uses the same pinned coordinates,
validation, per-investigation cache (including failures), trace and execution limits
as existing tools. Gemini and OpenAI receive the declaration through their existing
adapters. The model chooses whether to request it; no tool sequence is hardcoded.
The original weather/terrain combined-evidence contract remains unchanged.

SACHET data is returned as structured evidence with its original matching and
retrieval statuses, coverage, warnings and limitations. Each successfully retrieved
resource gets a canonical source ID/link and original retrieval/validation/evaluation
timestamps. Evaluation time is never presented as retrieval time. The existing
report schema is retained: official-alert findings belong in the investigation
summary and uncertainty in evidence gaps. The UI also exposes the exact official
alert evidence used by the AI. Prompts constrain interpretation but cannot prove
all model prose is correct; source checks and human review remain necessary.

Unresolved geographic/lifecycle/validity states and incomplete/unavailable sources
must not become local warnings. No verified match is not evidence of safety or
absence of official alerts. Separate polygon downloads, circles and district/LGD
inference remain unsupported. See [SACHET schema and safeguards](docs/SACHET.md).

Integration validation uses mocked SACHET transport and model responses only in
`tests/test_sachet_integration.py`. No live SACHET or Gemini calls were made during
this implementation. A separately authorized live integration smoke test and
host-specific secret/network checks remain prerequisites for deployment readiness.
