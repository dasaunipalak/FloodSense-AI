# FloodSense AI deployment guide

## Scope and prerequisites

The app integrates weather, terrain, official SACHET alerts, and the existing
Gemini investigation agent in Streamlit. OpenAI is an explicitly selected
alternative. This guide describes PR #10 plus the local reliability fixes;
those uncommitted fixes must be reviewed and published before a host can use them.
No hosted deployment or production source access has been verified.

Use Python 3.11, the locally tested version family, Git, and a virtual environment.
The repository permits Python 3.11 or newer, but other versions and Cloud Linux
need separate checks. Install the root `requirements.txt`: Streamlit, pandas,
pydeck, google-genai, python-dotenv, and OpenAI. GDAL/local raster files are not
required. Version ranges are not a lockfile or a cross-platform guarantee.

Public weather, terrain, and SACHET retrieval need no user-supplied API keys.
AI investigation requires a configured provider key and model. Outbound HTTPS
must reach Open-Meteo, OpenTopoData, NDMA SACHET, and the selected LLM provider;
browser map assets also need network access. Never disable TLS verification.

## Local installation and execution

From the repository root, reuse an existing `.venv` if available. Otherwise:

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m streamlit run app.py
```

macOS/Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
streamlit run app.py
```

With the virtual environment active, `python -m streamlit run app.py` is equivalent.
Use the printed URL, rather than assuming a port. Control-C stops the server.
Starting the app does not perform an AI investigation or retrieve public evidence.

## Provider configuration and secrets

`ai/provider.py` loads the repository-root `.env` using python-dotenv with
`override=False`, then selects the provider. Existing process variables take
precedence, even when empty. A blank process key can therefore mask a valid
local `.env` key. Restart the process after changing configuration; do not assume
an imported provider will reload it on every Streamlit rerun.

Use `.env.example` as a reference, without overwriting an existing `.env`:

```dotenv
LLM_PROVIDER=gemini
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.1-flash-lite
```

Enter the actual key only in your local ignored configuration or host secret
settings, never in documentation, Git, logs, screenshots, or command history.
`GEMINI_API_KEY` and `GEMINI_MODEL` must be nonempty. The model above succeeded in
an earlier weather/terrain investigation; it is not a runtime default or a promise
of current model access. `LLM_PROVIDER` is optional: absent means `gemini`; an
empty or unknown value is invalid. The adapter uses the Gemini Developer API
(`vertexai=False`) through the maintained `google-genai` SDK.

For an explicit OpenAI alternative set `LLM_PROVIDER=openai`, `OPENAI_API_KEY`,
and `OPENAI_MODEL`. The model must support Responses function calling and strict
structured output. There is no automatic provider/model fallback. The OpenAI
adapter rejects `OPENAI_BASE_URL`. Public source settings are defined in the
service code; arbitrary key, endpoint, or timeout environment variables do not
configure those services.

Both dashboard and standalone CLI use the same agent and environment contract.
The CLI loads `.env` but does not initialize Streamlit's secrets system:

```sh
python -m ai 29.2183 79.5130
```

This command makes live LLM and selected evidence requests; run it only as an
explicitly authorized investigation, not as an installation/offline check.
Missing configuration returns a structured configuration error. AI failure does
not remove separately loaded dashboard evidence. Quota, billing, model access,
and provider data-use terms apply; free-tier eligibility is not guaranteed.

## Streamlit Community Cloud — instructions, not verified deployment

Use the official [deployment guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
and [Cloud secrets guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).
After separate approval to publish and deploy:

1. Ensure the intended fixes are reviewed and present on the release branch.
   Community Cloud cannot use uncommitted local changes.
2. In Community Cloud choose Create app and select repository
   `dasaunipalak/FloodSense-AI`, the approved branch (normally `main`), and root
   entry point `app.py`.
3. In Advanced settings choose Python 3.11 if available. Test another version
   before choosing it; do not assume the host default matches local validation.
4. For AI investigations configure these **root-level TOML entries** in Secrets:

   ```toml
   LLM_PROVIDER = "gemini"
   GEMINI_API_KEY = ""
   GEMINI_MODEL = "gemini-3.1-flash-lite"
   ```

   Replace the empty key only in the secret settings. Do not put these entries
   under a `[gemini]` table: Streamlit exposes root-level secrets as environment
   variables, which the current provider reads. This uses Streamlit's
   [documented environment-variable behavior](https://docs.streamlit.io/develop/concepts/connections/secrets-management),
   not a custom `st.secrets` adapter. Confirm configuration on the target host
   without printing values. Evidence-only operation can leave AI secrets unset.
5. Deploy only after approval; inspect build/runtime logs without exposing secrets.
   Cloud installs root requirements and does not upload the local `.venv` or
   ignored `.env`. Record the actual deployed commit and URL.
6. Verify initial load, input validation, source failures, provenance, and map
   rendering. Separately authorize and bound live source/model smoke tests;
   stop on failure and do not automatically retry or substitute synthetic data.

Keep `.env` and `.streamlit/secrets.toml` ignored and untracked. Local Streamlit
can also use root-level secrets in that TOML file; the standalone CLI cannot
assume those values exist unless injected into its environment. Never display
`st.secrets` or dump environment variables. Do not commit either secret file.

## Dashboard requests, caching, and safeguards

- Investigate Location calls weather and terrain independently. A failed source
  does not erase the successful one; hourly charts, terrain samples, and the map
  use actual returned data.
- Refresh Official Alerts calls the existing SACHET RSS/CAP backend separately.
  Availability, geographic matching, lifecycle, warnings, and provenance remain
  distinct. Incomplete retrieval is not absence of alerts.
- Run AI Investigation explicitly starts the agent. Gemini chooses from
  `get_weather_evidence`, `get_terrain_evidence`, `get_combined_evidence`, and
  `get_official_alerts`; the dispatcher returns structured evidence and records
  actual tool calls. OpenAI supports the same tools.
- Session state retains results and failures on ordinary reruns; no model or
  source request is automatically repeated. AI results are labeled with their
  original coordinates. Changed coordinates hide older official local-alert cards
  in both dashboard and AI evidence views while preserving the snapshots.
- SACHET uses a process-local 300-second TTL/ETag cache. UI cards require recent,
  timezone-aware `validated_at` values for the feed and parsed resources, plus
  existing matching, completeness, lifecycle, and expiry checks. Reevaluation
  does not refresh source validation. Missing/invalid/future/stale validation
  suppresses cards. These checks run on reruns, not a timer.
- Agent caches are per investigation, including failures. Source/model requests
  have no automatic retries; the loop limits tool rounds to five and attempts
  to six. SDK request timeouts are 30 seconds. SACHET has separate bounded CAP,
  document-size, and network-time limits; see [SACHET documentation](docs/SACHET.md).

## Offline validation

Standard command from the repository root (with the environment active):

```sh
python -m unittest discover -s tests -v
python -m pip check
git diff --check
```

The standard Windows runner currently reports **190 passed, four failed, zero
skipped** across 194 tests. The same four `DashboardReliabilityTests` failed on
the unmodified PR #10 baseline (187 passed / four failed). Streamlit AppTest
creates a Windows Proactor event loop whose loopback `socket.socketpair()` calls
the mocked `socket.connect`; the guard raises `Live network forbidden` before
application execution. This is independently reproduced, not silently ignored.

The complete temporary PowerShell harness in
[README](README.md#windows-offline-test-harness) pre-creates event loops before
the unchanged socket guard is installed. It ran **194 tests successfully**.
There is no dedicated checked-in harness script. Use the complete documented
command; do not disable the guard or allow arbitrary network traffic to pass tests.
All fixtures and provider responses are offline mocks; tests require no live keys
or API calls. These results do not validate Cloud Linux, all dependency versions,
source uptime, or the factual accuracy of future generated reports.

## Sources, attribution, and scientific limits

- Weather: [Open-Meteo](https://open-meteo.com/), with source/request links,
  units, retrieval times, and forecast windows preserved. Its data is supplied
  under CC BY 4.0 with attribution; free API access has separate non-commercial
  [terms](https://open-meteo.com/en/terms). Review API usage eligibility before
  commercial deployment; this revision has no commercial-endpoint configuration.
- Elevation: [OpenTopoData SRTM90m](https://www.opentopodata.org/datasets/srtm/),
  derived from NASA JPL/LP DAAC
  [SRTMGL3 v003](https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL3.003).
  Keep this attribution and the exact request/dataset links with outputs.
  Follow the README's data licensing discussion and product-specific notices;
  the API software license does not license every dataset it hosts.
- Forecast precipitation probability is not flood probability. Twenty-four and
  forty-eight-hour precipitation totals cover full UTC hours starting at the
  next full hour. Retrieval time is not model initialization or survey time.
- Terrain is a sparse five-point historical sample, default radius 250 m, with
  roughly 90 m raster sampling and bilinear interpolation. Coverage/NODATA gaps
  remain null. Negative and zero elevations are valid. Relative differences
  do not establish drainage paths, inundation depth, road safety, or flood risk.
- No river levels, observed flooding, soil saturation, drainage capacity, or
  tides are included. This is evidence-based screening, not a validated flood
  prediction or official warning system. No safe/unsafe label, risk score,
  flood probability, or emergency alert is produced.

## Operational limitations and release checks

The previous Windows UI smoke test verified weather/terrain rendering, retained
results, stale-coordinate warnings, and safe alert failure display. Its one
bounded SACHET retrieval returned `retrieval_status=unavailable` and
`matching_status=source_unavailable`. The exact transport/XML cause remains
undiagnosed. This is not a successful live SACHET integration test or evidence
that no official alerts exist. Successful live end-to-end SACHET access and
production-host availability remain unverified.

SACHET matches only supported inline CAP polygons. District names/LGD codes,
separate polygon files, circles, and unsupported geometry cannot establish local
applicability. Cancelled, expired, superseded, or quarantined alerts are not
verified local warnings. Source-validation checks may conservatively withhold
individual alerts if any required resource is stale. Inspect statuses and
limitations; never infer safety from missing cards.

Public services and LLM accounts can have quota, availability, or network failures.
Defer requests on rate limits. Keep failures explicit, preserve successful sources,
and inspect sanitized diagnostics; do not disable TLS or invent measurements.
Generic SACHET transport/XML failures do not currently expose a precise underlying
cause. Any future diagnostic change needs separate review and secret protection.

SACHET cache and terrain pacing are process-local, not shared across workers.
There is no persistent alert archive or shared deployment quota accounting.
Missing updates/cancellations and cached data limit currentness. Session-state
results are snapshots, not background warning monitoring. `map_style=None` means
browser basemap rendering must be checked independently; markers are sample
locations, never predicted flood boundaries.

Review provider quota/billing, source licensing/attribution, exact-coordinate
privacy, secret configuration, dependency resolution, and host outbound access
before a public demo. Human review of AI prose remains necessary. No Cloud build,
production smoke test, deployment URL, or successful hosted SACHET retrieval is
claimed by this guide. Deployment and live testing require explicit approval.
