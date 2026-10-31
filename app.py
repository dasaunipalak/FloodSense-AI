"""Streamlit UI for the initial FloodSense AI scaffold."""

import streamlit as st


st.set_page_config(page_title="FloodSense AI", page_icon="🌧️")
st.title("FloodSense AI")
st.write("Evidence-grounded flood-risk investigation and preparedness guidance.")
st.caption(
    "Initial scaffold: no weather data, terrain analysis, maps, or AI agent is connected. "
    "This app does not provide scientifically validated flood predictions."
)

with st.form("investigation"):
    latitude = st.number_input(
        "Latitude", min_value=-90.0, max_value=90.0, value=None,
        step=0.0001, format="%.4f", placeholder="Enter latitude",
    )
    longitude = st.number_input(
        "Longitude", min_value=-180.0, max_value=180.0, value=None,
        step=0.0001, format="%.4f", placeholder="Enter longitude",
    )
    investigate = st.form_submit_button("Investigate")

if investigate:
    if latitude is None or longitude is None:
        st.warning("Enter both latitude and longitude to continue.")
    else:
        st.info(
            f"Location selected: {latitude:.4f}, {longitude:.4f}. "
            "Investigation is not implemented yet. No data has been fetched "
            "and no risk assessment has been generated."
        )
