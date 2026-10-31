"""Evidence-only dashboard using the project's real weather and terrain services."""

import pandas as pd
import pydeck as pdk
import streamlit as st

from services.weather import WeatherError, fetch_weather, validate_coordinates
from services.terrain import TerrainError, TerrainRateLimitError, fetch_terrain


def measurement(value, unit="m"):
    return "Unavailable" if value is None else f"{value:,.2f} {unit}"


def show_weather(data):
    st.subheader("Weather evidence")
    st.caption(f"Open-Meteo · Retrieved {data['fetched_at']} · Times in UTC")
    for column, window in zip(st.columns(2), ("24h", "48h")):
        total = data["totals"][window]
        with column:
            st.metric(f"Next {window} rainfall", measurement(total["precipitation"], total["unit"]))
            st.caption(f"{total['window_start']} → {total['window_end']}")
            if total["missing_hours"]:
                st.warning(f"{total['missing_hours']} rainfall hours unavailable; total unavailable.")
    st.caption("Windows begin at the next full UTC hour, excluding the current partial hour. Each timestamp marks the end of the preceding hour.")
    hourly = pd.DataFrame(data["hourly"])
    hourly["time"] = pd.to_datetime(hourly["time"], utc=True)
    hourly = hourly.rename(columns={"time": "Hour ending (UTC)", "precipitation": "Rainfall (mm)", "precipitation_probability": "Precipitation probability (%)"})
    for field, color in (("Rainfall (mm)", "#2589bd"), ("Precipitation probability (%)", "#38a89b")):
        st.write(f"**Hourly {field.lower()}**")
        if hourly[field].notna().any():
            st.bar_chart(hourly, x="Hour ending (UTC)", y=field, color=color)
        else:
            st.info("Unavailable")
    st.caption("Missing hours remain missing. Precipitation probability is not flood probability.")
    with st.expander("Hourly values and weather provenance"):
        table = hourly.drop(columns="source_time")
        st.dataframe(table.astype(object).where(table.notna(), "Unavailable").astype(str), hide_index=True, use_container_width=True)
        st.markdown(f"[Open-Meteo documentation]({data['documentation_url']}) · [Exact API request]({data['source_url']})")
        st.json({key: data[key] for key in ("requested_coordinates", "grid_coordinates", "hourly_units", "timezone", "fetched_at")})
        st.caption("Retrieval time is not model initialization time. Forecast grid coordinates can differ from the selected location.")


def show_terrain(data):
    st.subheader("Terrain evidence")
    st.caption(f"OpenTopoData · {data['dataset']} · Retrieved {data['fetched_at']}")
    columns = st.columns(3)
    columns[0].metric("Selected point elevation", measurement(data["points"][0]["elevation_m"]))
    columns[1].metric("Center − neighbor mean", measurement(data["center_minus_neighbor_mean_m"]))
    columns[2].metric("Five-point elevation range", measurement(data["sample_elevation_range_m"]))
    differences = {row["label"]: row["neighbor_minus_center_m"] for row in data["relative_elevation"]}
    rows = [{
        "Sample": point["label"].title(), "Latitude": point["latitude"],
        "Longitude": point["longitude"], "Elevation": measurement(point["elevation_m"]),
        "Neighbor − center": "Reference point" if point["label"] == "center" else measurement(differences[point["label"]]),
    } for point in data["points"]]
    st.dataframe(rows, hide_index=True, use_container_width=True)
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


def main():
    st.set_page_config(page_title="FloodSense AI", page_icon="🌧️", layout="wide")
    st.title("FloodSense AI")
    st.write("Agentic Geospatial Flood Risk Intelligence")
    st.caption("LOCATION INVESTIGATION · WEATHER + TERRAIN EVIDENCE")
    st.warning("Evidence-based screening tool, not an official flood warning system. Follow local authorities for warnings and emergency guidance.")
    with st.sidebar:
        st.header("Investigate a location")
        st.caption("Enter WGS84 coordinates in decimal degrees.")
        with st.form("investigation"):
            latitude = st.number_input("Latitude", min_value=-90.0, max_value=90.0, value=None, step=0.0001, format="%.4f", placeholder="−90 to 90")
            longitude = st.number_input("Longitude", min_value=-180.0, max_value=180.0, value=None, step=0.0001, format="%.4f", placeholder="−180 to 180")
            investigate = st.form_submit_button("Investigate Location", type="primary", use_container_width=True)
        st.caption("Each submission requests fresh evidence. Results and failures are retained during ordinary reruns. Submit again to refresh; defer requests if rate limited.")
        st.divider()
        st.markdown("**Sources**\n\n[Open-Meteo](https://open-meteo.com/) · [OpenTopoData](https://www.opentopodata.org/)")
        st.caption("AI investigation and flood-risk scoring are not implemented at this stage.")
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
    result = st.session_state.get("investigation_result")
    if result is None:
        st.info("Choose coordinates in the sidebar and select Investigate Location to retrieve real forecast and elevation evidence.")
    else:
        st.subheader(f"Evidence for {result['latitude']:.4f}, {result['longitude']:.4f}")
        st.caption("Showing the last submitted location. Editing sidebar inputs does not refresh these results.")
        for name, renderer in (("weather", show_weather), ("terrain", show_terrain)):
            with st.container(border=True):
                if result[name] is not None:
                    renderer(result[name])
                else:
                    st.subheader(f"{name.title()} evidence")
                    st.error(result[f"{name}_error"])
        show_map(result["latitude"], result["longitude"], result["terrain"])
    with st.expander("What this evidence can and cannot tell you"):
        st.write("Forecast rainfall and sparse elevation samples support an initial review only. River levels, drainage capacity, soil saturation, tides, and observed flooding are not included. No flood probability, risk score, flood boundary, or safe route is calculated.")
        st.caption("Live source availability and forecast completeness vary. Map tiles require internet access. Check retrieval timestamps before using retained results.")


if __name__ == "__main__":
    main()
