"""Evidence and AI investigation dashboard using the project's real weather and terrain services."""

from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

from services.sachet import fetch_sachet_alerts
from sachet_ui import show_sachet

from services.weather import WeatherError, fetch_weather, validate_coordinates
from services.terrain import TerrainError, TerrainRateLimitError, fetch_terrain


def measurement(value, unit="m"):
    return "Unavailable" if value is None else f"{value:,.2f} {unit}"


def show_weather_totals(data):
    for column, window in zip(st.columns(2), ("24h", "48h")):
        total = data["totals"][window]
        with column:
            st.metric(f"Next {window} rainfall", measurement(total["precipitation"], total["unit"]))
            st.caption(f"{total['window_start']} → {total['window_end']}")
            if total["missing_hours"]:
                st.warning(f"{total['missing_hours']} rainfall hours unavailable; total unavailable.")


def show_weather(data):
    st.subheader("Weather evidence")
    st.caption(f"Open-Meteo · Retrieved {data['fetched_at']} · Times in UTC")
    st.caption("Windows begin at the next full UTC hour, excluding the current partial hour. Each timestamp marks the end of the preceding hour.")
    hourly = pd.DataFrame(data["hourly"])
    hourly["time"] = pd.to_datetime(hourly["time"], utc=True)
    hourly = hourly.rename(columns={"time": "Hour ending (UTC)", "precipitation": "Rainfall (mm)", "precipitation_probability": "Precipitation probability (%)"})
    for field, color in (("Rainfall (mm)", "#4F8B83"), ("Precipitation probability (%)", "#244638")):
        st.write(f"**Hourly {field.lower()}**")
        if hourly[field].notna().any():
            st.bar_chart(hourly, x="Hour ending (UTC)", y=field, color=color)
        else:
            st.info("Unavailable")
    st.caption("Missing hours remain missing. Precipitation probability is not flood probability.")
    with st.expander("Hourly values and weather provenance"):
        table = hourly.drop(columns="source_time")
        st.dataframe(table.astype(object).where(table.notna(), "Unavailable").astype(str), hide_index=True, width="stretch")
        st.markdown(f"[Open-Meteo documentation]({data['documentation_url']}) · [Exact API request]({data['source_url']})")
        st.json({key: data[key] for key in ("requested_coordinates", "grid_coordinates", "hourly_units", "timezone", "fetched_at")})
        st.caption("Retrieval time is not model initialization time. Forecast grid coordinates can differ from the selected location.")


def show_terrain(data):
    st.subheader("Terrain evidence")
    st.caption(f"OpenTopoData · {data['dataset']} · Retrieved {data['fetched_at']}")
    columns = st.columns(2)
    columns[0].metric("Center − neighbor mean", measurement(data["center_minus_neighbor_mean_m"]))
    columns[1].metric("Five-point elevation range", measurement(data["sample_elevation_range_m"]))
    differences = {row["label"]: row["neighbor_minus_center_m"] for row in data["relative_elevation"]}
    rows = [{
        "Sample": point["label"].title(), "Latitude": point["latitude"],
        "Longitude": point["longitude"], "Elevation": measurement(point["elevation_m"]),
        "Neighbor − center": "Reference point" if point["label"] == "center" else measurement(differences[point["label"]]),
    } for point in data["points"]]
    st.dataframe(rows, hide_index=True, width="stretch")
    if data["missing_points"]:
        st.warning(f"{data['missing_points']} elevation samples unavailable. Summaries require all five points; individual differences require both elevations.")
    st.write("A positive neighbor − center difference means that sample is higher than your selected location; a negative difference means it is lower. Center − neighbor mean compares your location with the average of the four surrounding samples.")
    st.caption(f"Sampling radius: {data['sample_radius_m']:g} m · Units: {data['elevation_unit']} · Vertical datum: {data['vertical_datum']} · Nominal resolution: {data['nominal_resolution_m']} m · Interpolation: {data['interpolation']}")
    st.info("Sparse historical elevations describe local relief. They do not establish drainage paths, flood depth, flood probability, or road safety.")
    with st.expander("Terrain provenance and limitations"):
        st.markdown(f"[OpenTopoData]({data['documentation_url']}) · [Dataset]({data['dataset_url']}) · [Exact API request]({data['source_url']})")
        st.markdown("Elevation source: NASA JPL/LP DAAC SRTMGL3 v003. [Product reference](https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL3.003).")
        st.caption("Retrieval time is not terrain acquisition time. SRTM coverage is limited; nearby samples can share raster cells. Zero and below-zero elevations are valid.")
        st.json(data)


def show_map(latitude, longitude, terrain):
    st.subheader("Location & sample map")
    points = [{"latitude": latitude, "longitude": longitude, "label": "Selected location", "elevation": "Unavailable", "color": [37, 137, 189]}]
    if terrain is not None:
        points = [{
            "latitude": point["latitude"], "longitude": point["longitude"],
            "label": "Selected location / center" if point["label"] == "center" else point["label"].title(),
            "elevation": measurement(point["elevation_m"]),
            "color": [37, 137, 189] if point["label"] == "center" else [56, 168, 155],
        } for point in terrain["points"]]
    # Explicit centering also avoids misleading automatic bounds at the dateline.
    st.pydeck_chart(pdk.Deck(
        map_style=None,
        initial_view_state=pdk.ViewState(latitude=latitude, longitude=longitude, zoom=14),
        layers=[pdk.Layer("ScatterplotLayer", data=points, get_position="[longitude, latitude]", get_fill_color="color", get_radius=12, radius_min_pixels=6, pickable=True)],
        tooltip={"text": "{label}\n{latitude}, {longitude}\nElevation: {elevation}"},
    ))
    st.caption("Blue: selected location / center sample. Green: surrounding samples. The center is one of the five terrain samples. Hover for details. Markers are sampling locations, not flood zones.")


def show_ai(latitude, longitude):
    st.header("AI interpretation")
    saved = st.session_state.get("ai_investigation")
    if saved is None:
        st.info("Select Run AI Investigation to request an interpretation. The evidence dashboard works without an LLM key.")
        return
    lat, lon = saved["coordinates"]
    result = saved["result"]
    st.subheader(f"AI investigation for {lat:.4f}, {lon:.4f}")
    if (latitude, longitude) != (lat, lon):
        st.warning("Previous AI investigation belongs to an earlier location, not the currently selected coordinates.")
    st.caption("Retained result from the last explicit AI request; reruns do not contact the model.")
    st.write(f"Investigation status: {result['agent_status']}")
    messages = {
        "configuration_error": "AI configuration is missing or invalid. Configure the selected provider's API key and model locally; the evidence dashboard still works without them.",
        "provider_rate_limit": "AI provider quota or rate limit reached. No automatic retry was made.",
        "provider_authentication": "AI authentication or access failed. Check the local key and permissions.",
        "provider_timeout": "AI provider timed out. No automatic retry was made.",
        "provider_network_error": "AI provider could not be reached. No automatic retry was made.",
        "source_error": "An evidence source was unavailable; inspect availability below.",
    }
    for error in result.get("errors", []):
        st.error(messages.get(error.get("code"), "AI investigation encountered an error. No automatic retry was made; no substitute report was invented."))
        from ai.provider import ProviderError
        diagnostics = ProviderError("", "", **{key: error.get(key) for key in
            ("http_status", "provider_status", "exception_type", "retry_after_seconds")}).diagnostics
        if any(value is not None for value in diagnostics.values()):
            st.json(diagnostics)
    for warning in result.get("warnings", []):
        st.warning(warning)
    report = result.get("final_report")
    if result["agent_status"] == "completed" and report:
        for key, label in (("investigation_summary", "Investigation summary"),
                           ("weather_findings", "Weather interpretation"),
                           ("terrain_findings", "Terrain interpretation"),
                           ("evidence_gaps", "Missing information"),
                           ("preparedness_considerations", "General preparedness"),
                           ("limitations", "Limitations")):
            st.subheader(label)
            st.text(report[key])
    else:
        st.info("No AI report was generated. Any previously loaded dashboard evidence remains above. Agent-retrieved evidence, if any, is listed separately below.")
    st.subheader("Live evidence used by the agent")
    st.caption("API-retrieved forecasts and historical terrain samples, separate from AI interpretation. Retrieval times may differ from the dashboard refresh.")
    for name in ("weather", "terrain"):
        entry = result.get("evidence", {}).get(name)
        st.write(f"{name.title()}: {entry['status'] if entry else 'Not retrieved'}")
        if entry:
            with st.expander(f"Agent {name} evidence"):
                st.json(entry)
    official = result.get("evidence", {}).get("official_alerts")
    with st.expander("Official alert evidence used by the AI"):
        if official and official.get("data") is not None:
            if official.get("status") == "unavailable":
                st.warning("Official-alert retrieval was attempted but the source was unavailable. No verified alert evidence was established.")
            show_sachet(official["data"], allow_local_cards=(latitude, longitude) == (lat, lon))
        elif official is not None or any(call.get("tool") == "get_official_alerts"
                                        for call in result.get("tool_call_trace", [])):
            st.warning("An official-alert tool request was recorded, but no usable evidence was returned. Retrieval failed, was unavailable, or could not execute; inspect the trace.")
        else:
            st.info("The official-alert tool was not invoked by this investigation.")
    for source in result.get("evidence_source_references", []):
        st.write(f"{source['source']} · Retrieved {source['fetched_at']}")
        st.link_button(f"Open {source['id']} source", source["source_url"])
    with st.expander("Investigation Trace"):
        st.caption("Actual model-requested tools, not private reasoning.")
        trace = result.get("tool_call_trace", [])
        if not trace:
            st.info("No evidence tools were requested.")
        for call in trace:
            st.write(f"Round {call['round']} · {call['tool']} · {call['status']}")
            st.code(call.get("arguments") or "Unavailable", language="json")
            st.json(call.get("result"))


def main():
    st.set_page_config(page_title="FloodSense AI", page_icon="🌧️", layout="wide")
    st.markdown("<style>" + Path(__file__).with_name("ui.css").read_text(encoding="utf-8") + "</style>", unsafe_allow_html=True)
    st.caption("FLOODSENSE / ENVIRONMENTAL INTELLIGENCE")
    st.title("FloodSense AI")
    st.write("Geospatial evidence for flood preparedness")
    st.caption("Explore real forecasts, local terrain, and official alerts. Investigate the evidence with an autonomous AI agent.")
    st.warning("FloodSense AI provides preliminary, evidence-based screening, not official flood forecasts or emergency warnings.")
    with st.container(border=True):
        st.subheader("Explore a location")
        st.caption("WGS84 coordinates · decimal degrees")
        lat_col, lon_col = st.columns(2)
        with lat_col:
            latitude = st.number_input("Latitude", min_value=-90.0, max_value=90.0, value=None, step=0.0001, format="%.4f", placeholder="−90 to 90")
        with lon_col:
            longitude = st.number_input("Longitude", min_value=-180.0, max_value=180.0, value=None, step=0.0001, format="%.4f", placeholder="−180 to 180")
        evidence_action, ai_action, alert_action = st.columns(3)
        with evidence_action:
            investigate = st.button("Investigate Location", width="stretch")
        with ai_action:
            run_ai = st.button("Run AI Investigation", type="primary", width="stretch")
        with alert_action:
            refresh_alerts = st.button("Refresh Official Alerts", width="stretch")
        st.caption("Each action runs only on click. Weather / terrain, official alerts, and AI refresh independently; ordinary reruns retain results.")
    st.caption("AI uses provider quota and sends the selected coordinates and retrieved evidence to the configured provider. Gemini is the default; OpenAI is optional.")
    if investigate:
        try:
            latitude, longitude = validate_coordinates(latitude, longitude)
        except ValueError:
            st.error("Enter valid latitude (−90 to 90) and longitude (−180 to 180) before investigating.")
        else:
            result = {"latitude": latitude, "longitude": longitude, "weather": None, "terrain": None, "weather_error": None, "terrain_error": None}
            with st.spinner("Retrieving real weather and terrain evidence…"):
                for name, fetcher, expected_error in (("weather", fetch_weather, WeatherError), ("terrain", fetch_terrain, TerrainError)):
                    try:
                        result[name] = fetcher(latitude, longitude)
                    except TerrainRateLimitError:
                        result[f"{name}_error"] = "OpenTopoData rate limit reached. Defer requests and retry later; no automatic retry was made."
                    except (expected_error, ValueError):
                        result[f"{name}_error"] = f"{name.title()} evidence unavailable: the service could not retrieve valid data. Try again later."
                    except Exception:
                        # Isolate unexpected service failures without revealing raw errors.
                        result[f"{name}_error"] = f"{name.title()} evidence unavailable due to an unexpected service error."
            st.session_state["investigation_result"] = result
    if run_ai:
        try:
            coordinates = validate_coordinates(latitude, longitude)
        except ValueError:
            st.error("Enter valid latitude and longitude before running AI investigation.")
        else:
            with st.spinner("AI is investigating weather and terrain evidence…"):
                try:
                    from ai.agent import investigate as investigate_ai
                    ai_result = investigate_ai(*coordinates)
                except Exception:
                    # Never expose SDK errors or credentials in Streamlit tracebacks.
                    ai_result = {"agent_status": "provider_error", "final_report": None,
                                 "errors": [{"code": "unexpected_error"}]}
            st.session_state["ai_investigation"] = {"coordinates": coordinates, "result": ai_result}
    if refresh_alerts:
        try:
            alert_coordinates = validate_coordinates(latitude, longitude)
        except ValueError:
            st.error("Enter valid latitude and longitude before retrieving official alerts.")
        else:
            with st.spinner("Retrieving official alert evidence…"):
                try:
                    alert_data = fetch_sachet_alerts(*alert_coordinates)
                except Exception:
                    alert_data = None
            st.session_state["sachet_result"] = {"coordinates": alert_coordinates, "data": alert_data}
    result = st.session_state.get("investigation_result")
    saved_alerts = st.session_state.get("sachet_result")
    weather = result["weather"] if result else None
    terrain = result["terrain"] if result else None
    if result:
        st.subheader(f"Evidence for {result['latitude']:.4f}, {result['longitude']:.4f}")
        st.caption("Showing the last submitted location. Editing location inputs does not refresh these results.")
        if (latitude, longitude) != (result["latitude"], result["longitude"]):
            st.warning("Weather and terrain belong to the previous location shown above; refresh to investigate the selected coordinates.")
    if weather is not None or terrain is not None:
        rain_summary, elevation_summary, alert_summary = st.columns([2, 1, 1])
        with rain_summary:
            if weather is not None:
                show_weather_totals(weather)
            else:
                st.caption("24h / 48h rainfall: Unavailable")
        with elevation_summary:
            st.metric("Selected point elevation", measurement(terrain["points"][0]["elevation_m"]) if terrain else "Unavailable")
        with alert_summary:
            status = "Not checked"
            if saved_alerts:
                if saved_alerts["coordinates"] != (latitude, longitude):
                    status = "Earlier location"
                else:
                    status = saved_alerts["data"]["retrieval_status"].title() if saved_alerts["data"] else "Unavailable"
            st.metric("Official-alert source", status)
            st.caption("Retrieval status, not a safety assessment. See Official Alerts for snapshot age and applicability.")
    overview, details, alerts, ai_panel = st.tabs(["Overview", "Weather & Terrain", "Official Alerts", "AI Investigation"])
    with overview:
        if result is None:
            st.info("Enter coordinates → Load evidence → Explore results")
        else:
            map_column, weather_column = st.columns([1, 1])
            with map_column:
                show_map(result["latitude"], result["longitude"], terrain)
            with weather_column:
                if weather is not None:
                    show_weather(weather)
                else:
                    st.subheader("Weather evidence")
                    st.error(result["weather_error"])
        st.caption("Want an evidence-based explanation? Select Run AI Investigation above, then open AI Investigation for findings, sources, and the actual tool trace.")
    with details:
        st.caption("Hourly forecast charts and weather provenance are in Overview. Inspect the five terrain samples and their measurement limits here.")
        if terrain is not None:
            show_terrain(terrain)
        elif result:
            st.subheader("Terrain evidence")
            st.error(result["terrain_error"])
        else:
            st.info("Terrain evidence has not been requested in this session.")
    with alerts:
        st.header("Official Disaster Alerts")
        st.caption("NDMA SACHET distributes issuer-provided alerts for India. Geographic coverage is verified only through supported inline polygons, never district names or LGD codes alone.")
        saved_alerts = st.session_state.get("sachet_result")
        if saved_alerts is None:
            st.info("Select Refresh Official Alerts to retrieve SACHET evidence. No alerts have been checked in this dashboard session.")
        else:
            alert_lat, alert_lon = saved_alerts["coordinates"]
            st.subheader(f"Official alert snapshot for {alert_lat:.4f}, {alert_lon:.4f}")
            if (latitude, longitude) != (alert_lat, alert_lon):
                st.warning("Previous official alert snapshot belongs to an earlier location, not the selected coordinates.")
            if saved_alerts["data"] is None:
                st.warning("Official alert source unavailable. Other dashboard evidence remains independent. No automatic retry was made.")
            else:
                show_sachet(saved_alerts["data"], allow_local_cards=(latitude, longitude) == (alert_lat, alert_lon))
    with ai_panel:
        st.caption("AUTONOMOUS EVIDENCE INVESTIGATION")
        st.write("Gemini independently selects weather, terrain, and official-alert tools. Review its interpretation alongside the evidence, source timestamps, and limitations.")
        show_ai(latitude, longitude)
    with st.expander("What this evidence can and cannot tell you"):
        st.write("Forecast rainfall and sparse elevation samples support an initial review only. River levels, drainage capacity, soil saturation, tides, and observed flooding are not included. No flood probability, risk score, flood boundary, or safe route is calculated.")
        st.caption("Live source availability and forecast completeness vary. Map tiles require internet access. Check retrieval timestamps before using retained results.")


if __name__ == "__main__":
    main()
