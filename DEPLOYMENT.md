# FloodSense AI deployment guide

## Scope and prerequisites

This guide describes the merged evidence dashboard at main commit `0bc9bfb`.
The dashboard calls real weather and terrain services directly; the combined
evidence engine is a separate callable interface. The AI agent being developed
on `feature/ai-agent` is not included or required. No deployment is performed by
these instructions or by the automated tests.

Use Python 3.11 (the locally verified version is 3.11.0), Git, and internet
access for installation and live investigations. Python 3.11 or newer is the
repository convention; other versions and the Cloud Linux environment need
their own deployment smoke check. API access is from the server to
`api.open-meteo.com` and `api.opentopodata.org` over HTTPS.

## Local installation and execution

From a clone of the repository, in its root directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
python -m streamlit run app.py
```

Use the URL printed by Streamlit; do not assume a particular port is free.
Control-C stops the server. On Windows activate with `.venv\Scripts\Activate.ps1`.
The app initially displays an input form without requesting weather or terrain.
Each valid submission requests fresh evidence. Ordinary reruns retain results
in session state; edited coordinates do not replace results until resubmission.

## Dependencies and existing configuration

`requirements.txt` declares `streamlit>=1.49,<2`, `pandas`, and `pydeck`. The
dashboard directly imports all three, so pandas/pydeck are explicitly declared
rather than relying on Streamlit to supply them transitively. Services,
analysis, and unittest use the Python standard library.
No LLM SDK, API key, GDAL, or local raster datasets are required by this revision.

The inspected local environment has Streamlit 1.66.1, pandas 3.1.0, and pydeck
0.9.3. These are observed installed versions, not a lockfile or a reproducible
dependency guarantee. No new version pins or compatibility bounds were inferred
from this one environment; a tested version policy remains a release decision.
The Streamlit minimum reflects dashboard use of
`width="stretch"`; using an older installation can break rendering.

There is no tracked `.streamlit/config.toml`, Python runtime file, Dockerfile,
`packages.txt`, lockfile, or CI/deployment workflow in this revision. None is
required just to attempt Community Cloud deployment; select Python explicitly
and let Cloud use the root `requirements.txt`. See the official
[dependency guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies).

## Environment variables and secrets

No application-specific environment variables or secrets are read by the
current dashboard, weather/terrain services, or evidence engine. Public data
endpoints are fixed in the service modules. Setting an arbitrary API key,
endpoint, or timeout environment variable will not change their behavior.
There is no dotenv loader, so a `.env` file is not automatically consumed.

Streamlit's standard server options can be set through its CLI or environment,
for example when running without a browser or on a different port:

```sh
python -m streamlit run app.py --server.headless true --server.port 8502
```

Do not add a speculative LLM secret yet. When the agent is merged, review its
actual configuration contract and dependencies separately. If it requires
secrets, use ignored local `.streamlit/secrets.toml` or environment injection,
and the Cloud secret manager, with matching names in the consuming code.
Streamlit supports local `st.secrets` and Cloud secret settings as described in
the [secret-management guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).

## Streamlit Community Cloud

Follow the official [deployment instructions](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy):

1. Review and merge approved changes into the intended release branch first.
   Deployment permission and account/GitHub authorization are separate from
   creating this guide. Do not select another teammate's in-progress branch.
2. Sign in at `share.streamlit.io` and authorize access to the repository.
3. Choose **Create app**, select `dasaunipalak/FloodSense-AI`, branch `main`,
   and entrypoint `app.py` at the repository root.
4. In advanced settings select Python 3.11 if offered. If unavailable, select
   a supported version only after testing that version. Leave app secrets empty
   for this revision; never paste credentials into code or logs.
5. Deploy and inspect build/runtime logs. Cloud discovers root dependencies;
   it does not use your local `.venv`. Record the resulting app URL and deployed
   commit, rather than inventing or assuming a URL.
6. Check the empty form loads, validation works, and scientific limitations
   and attribution are visible. An authorized manual live investigation can
   then check API connectivity and map rendering while respecting quotas.
   Never use synthetic unit-test fixtures as deployed demo results.

No Cloud build, Linux install, browser map check, or deployment has been verified
by the offline test suite. Review map behavior in a real browser: `app.py` sets
`map_style=None`, so markers must not be interpreted as a guaranteed street
basemap or flood boundary. Import checks do not verify browser rendering.

## Offline validation

```sh
python -m unittest discover -s tests -v
python -m pip check
python -c 'import app, services.weather, services.terrain, analysis.evidence, analysis.terrain, ai'
git diff --check
```

New reliability tests mock HTTP transport, run the real service parsers and
analysis, and exercise dashboard paths with Streamlit AppTest. Outbound socket
connections are blocked in these tests; no live API requests are made. The
30-second AppTest timeout permits cold dashboard imports; it does not alter
production API timeouts. Existing unit tests use controlled mocks as well.

These tests verify contracts and failure behavior, not accuracy of the upstream
forecast/DEM or uptime. Synthetic boundary fixtures do not imply real SRTM
coverage at the poles. Re-run this validation after future agent changes.

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

## API failures and troubleshooting

| Symptom | Meaning and action |
| --- | --- |
| Import error for Streamlit/pandas/pydeck | Activate the right virtual environment, install root requirements, and run `python -m pip check`. Confirm the deployed commit and dependency build logs. |
| Dashboard width argument failure | Check Streamlit meets the declared minimum; rebuild the intended environment rather than changing UI code during reliability work. |
| DNS, TLS, connectivity error, timeout, HTTP 5xx | Check outbound HTTPS access, system clock/certificate trust, provider status, and hosting logs. Keep the successful source visible and the failed source unavailable. Do not disable TLS verification or substitute sample measurements. |
| HTTP 429 / rate limit | Defer requests. OpenTopoData preserves raw `Retry-After` on `TerrainRateLimitError` and in evidence reports; it may be seconds or an HTTP date. The dashboard gives a generic defer message. Weather reports HTTP 429 as `WeatherError` without preserving the header. No service automatically retries. |
| Null rainfall totals/elevations | Inspect missing-hour/point counts, coverage, and individual values. Null is unknown, not a dry forecast or zero elevation. Totals require every rainfall hour in the window; terrain summaries require all samples. |
| Old results after editing coordinates | Submit the form again to refresh. Ordinary reruns intentionally reuse last-submitted results; inspect retrieval timestamps. |
| No street basemap / incomplete browser map | Markers use pydeck with `map_style=None`; inspect browser/WebGL/network behavior separately. This is not evidence of a flood-data failure. |
| Invalid/malformed API response | Services reject invalid formats, units, coordinate mismatches, or values. Preserve unavailability and investigate provider/schema changes with sanitized logs. |

The [OpenTopoData public API](https://www.opentopodata.org/#public-api) publishes
limits of 100 points per request, one request per second, and 1000 requests/day.
Five points are batched into one request. Existing pacing is only within one
Python process; it cannot coordinate shared IP/day quotas or multiple workers.
There is no persistent/shared cache, quota accounting, or background refresh.
Avoid repeated submissions; decide on shared caching/quota controls before a
larger public launch. Sequential source calls use 20-second default socket
timeouts each, not a single overall investigation deadline.

## Security and items for release discussion

- `.gitignore` excludes `.venv/`, `.env` variants (except a sanitized example),
  `.streamlit/secrets.toml`, key files, and common credential directories. Check
  `git status` and staged filenames before every commit; ignore rules do not
  remove already-tracked secrets. Rotate exposed credentials if a leak occurs.
- Keep credentials out of screenshots, PR descriptions, downloaded evidence,
  browser output, and logs. Raw evidence errors are currently from public
  services; review error sanitization if authenticated services are introduced.
- Exact coordinates appear in request URLs, session state, and evidence.
  Avoid sensitive locations in public demos/logs and review retention/access
  expectations before collecting user location histories.
- Existing evidence tests are not a guarantee that a future agent preserves
  uncertainty. Validate its prompts, dependencies, secrets, and displayed claims
  after Harshita's branch is reviewed and merged.
- No tracked deployment automation or cloud runtime validation exists. Version
  reproducibility, shared quotas, and a manual
  Cloud/browser smoke check remain release decisions, not verified blockers
  preventing a local evidence-only demo.

This work changes tests, documentation, and direct dependency declarations only;
it does not modify app, AI, service, or analysis code or create a cloud deployment.
