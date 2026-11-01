"""Presentation of SACHET snapshots; no requests or geographic inference here."""
from datetime import datetime, timezone
import streamlit as st


def show_sachet(data):
    st.write(f"Official source availability: {data['retrieval_status']}")
    st.write(f"Geographic matching status: {data['matching_status']}")
    st.write(f"Location match: {data['location_match']}")
    st.caption(f"Snapshot evaluated at {data['evaluated_at']} (UTC). Retrieval, validation and evaluation times are distinct.")
    st.caption("Retained snapshot, not a continuously monitored warning feed. New updates or cancellations may not yet be reflected. Refresh explicitly to recheck using the backend cache policy.")
    if data["retrieval_status"] == "unavailable":
        st.warning("Official alert source unavailable. Current alert applicability could not be established.")
    elif not data["coverage"]["complete_snapshot"]:
        st.warning("Only part of the feed was examined successfully. Retrieval is incomplete; no authoritative local alert list can be established.")
    if data["retrieval_status"] != "unavailable" and data["location_match"] == "unresolved":
        st.warning("Official alerts were retrieved, but their applicability to this location could not be verified.")
    if data["matching_status"] == "no_verified_active_matching_alerts":
        st.info("No verified active matching alerts in this snapshot. This does not establish absence of official alerts or safety.")
    if not data["coverage"]["lifecycle_resolved"]:
        st.warning("Alert lifecycle coverage is unresolved. Quarantined messages are not verified local alerts.")
    now = datetime.now(timezone.utc)
    evaluated = datetime.fromisoformat(data["evaluated_at"])
    fresh = 0 <= (now - evaluated).total_seconds() < 300
    if not fresh:
        st.warning("Retained alert snapshot is stale. Refresh official alerts before reviewing active local alert cards.")
    shown = 0
    if fresh and data["coverage"]["complete_snapshot"] and data["retrieval_status"] != "unavailable":
        for alert in data["active_matching_alerts"]:
            if (not alert["active"] or alert["lifecycle"] != "active"
                    or alert["location_match"] != "matched" or alert["status"] != "Actual"
                    or alert["scope"] != "Public" or alert["msgType"] not in ("Alert", "Update")):
                continue
            sections = [info for info in alert["info"] if info["active"]
                        and info["location_match"] == "matched" and info["expires"]
                        and datetime.fromisoformat(info["effective"] or alert["sent"]) <= now
                        < datetime.fromisoformat(info["expires"])]
            if not sections:
                continue
            shown += 1
            with st.container(border=True):
                st.write("Verified active local alert in the evaluated snapshot")
                st.text(f"Identifier: {alert['identifier']} | Sender: {alert['sender']}")
                for info in sections:
                    st.text(f"Issuing authority: {info['senderName'] or alert['sender']}")
                    st.text(f"Event: {info['event']} | Severity: {info['severity']}")
                    st.text(f"Language: {info['language'] or 'Unavailable'}")
                    st.text(f"Effective: {info['effective'] or alert['sent']} | Expires: {info['expires']}")
                    for field in ("headline", "description", "instruction"):
                        if info.get(field):
                            st.text(f"Official {field}: {info[field]}")
                st.link_button("Official alert source", alert["source_url"])
    if not shown:
        st.caption("No verified local alert cards are displayed. Inspect snapshot status and limitations; this is not a safety conclusion.")
    for warning in data["warnings"]:
        st.warning(f"{warning['code']}: {warning['message']}")
    for limitation in data["limitations"]:
        st.caption(limitation)
    with st.expander("Official alert provenance and coverage"):
        st.json(data["coverage"])
        for resource in data["retrievals"]:
            st.link_button("Official retrieved resource", resource["source_url"])
            st.text(f"Retrieved: {resource['retrieved_at']} | Validated: {resource['validated_at']} | Cache: {resource['cache_status']}")
    with st.expander("All parsed alert records (not a local warning list)"):
        st.caption("May include expired, cancelled, superseded, unmatched or unresolved records. Do not interpret these as current local warnings.")
        st.json(data["alerts"])
