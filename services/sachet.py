"""Bounded official SACHET RSS/CAP retrieval. No UI, LLM, or fallback alerts."""

from copy import deepcopy
from datetime import datetime, timezone
from http.client import HTTPException
import math
from numbers import Real
from threading import RLock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from analysis.sachet import AlertDataError, parse_cap, reconcile, xml_root
from services.weather import validate_coordinates

RSS_URL = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"
GUIDE_URL = "https://sachet.ndma.gov.in/docs/Integration_Guide_For_Agencies.pdf"
LIMITATIONS = [
    "Published RSS snapshot only, not an exhaustive official warning archive or availability guarantee.",
    "No alert/no verified match is not evidence that a location is safe.",
    "Only verified inline CAP polygons are used; LGD codes/names alone do not locate a coordinate.",
    "Separate polygon downloads, circles, dateline/polar/complex rings and coordinate-to-LGD mapping are not supported.",
    "Cache and ETags are process-local; multiple Cloud workers do not share cache or request budgets.",
    "Official alert evidence is not a FloodSense flood prediction. Preserve issuer text and attribution.",
]


class SachetError(Exception):
    """Official retrieval failed; no stale or fabricated fallback is returned."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SachetError("Redirects are not permitted for SACHET requests")


def _check_url(url):
    """Strict allowlist prevents untrusted RSS from requesting arbitrary URLs."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "sachet.ndma.gov.in" or parts.fragment:
        raise SachetError("Non-allowlisted SACHET URL")
    if url == RSS_URL:
        return
    query = parse_qs(parts.query, keep_blank_values=True)
    if (parts.path != "/cap_public_website/FetchXMLFile" or set(query) != {"identifier"}
            or len(query["identifier"]) != 1
            or not query["identifier"][0].isascii() or not query["identifier"][0].isdigit()
            or len(query["identifier"][0]) > 32):
        raise SachetError("Non-allowlisted CAP download URL")


class SachetClient:
    """Reusable, thread-safe bounded HTTP cache; inject transport only in tests.

    Limits per investigation: 30s overall deadline checked between reads,
    <=10s per blocking request/read, 1MB/XML document, 20 CAP downloads by default.
    Cached responses revalidate after 300s; 304 refreshes validation time, not
    content retrieval time. Never returns stale data after failed revalidation.
    No automatic retries. Redirects and non-official links are forbidden.
    """

    def __init__(self, *, timeout=10, ttl_seconds=300, max_bytes=1_000_000,
                 max_messages=20, total_seconds=30):
        for value in (timeout, ttl_seconds, total_seconds):
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
                raise ValueError("Timeouts/cache TTL must be positive finite numbers")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2_000_000:
            raise ValueError("max_bytes must be an integer from 1 to 2000000")
        if type(max_messages) is not int or not 1 <= max_messages <= 100:
            raise ValueError("max_messages must be an integer from 1 to 100")
        self.timeout, self.ttl_seconds = timeout, ttl_seconds
        self.max_bytes, self.max_messages, self.total_seconds = max_bytes, max_messages, total_seconds
        self._cache = {}
        self._opener = build_opener(_NoRedirect())
        self._lock = RLock()

    def clear_cache(self):
        """Discard this process-local cache explicitly."""
        with self._lock:
            self._cache.clear()

    def _get(self, url, now, deadline):
        _check_url(url)
        cached = self._cache.get(url)
        if cached and 0 <= (now - cached["checked"]).total_seconds() < self.ttl_seconds:
            return cached["body"], {**cached["metadata"], "cache_status": "ttl_hit"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SachetError("Investigation download deadline exceeded")
        headers = {"User-Agent": "FloodSense-AI/1.0", "Accept": "application/xml, application/rss+xml, text/xml"}
        if cached and cached["etag"]:
            headers["If-None-Match"] = cached["etag"]
        try:
            with self._opener.open(Request(url, headers=headers), timeout=min(self.timeout, remaining)) as response:
                if response.status != 200:
                    raise SachetError("Unexpected HTTP response")
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_bytes:
                    raise SachetError("XML download size limit exceeded")
                chunks, size = [], 0
                # Avoid waiting to fill a buffer while a peer trickles bytes.
                reader = getattr(response, "read1", response.read)
                while True:
                    if time.monotonic() > deadline:
                        raise SachetError("Investigation download deadline exceeded")
                    chunk = reader(min(65536, self.max_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise SachetError("XML download size limit exceeded")
                    chunks.append(chunk)
                body = b"".join(chunks)
                xml_root(body)  # Never cache malformed or unsafe XML.
                completed = datetime.now(timezone.utc)
                metadata = {"source_url": url, "retrieved_at": completed.isoformat(),
                            "validated_at": completed.isoformat(), "etag": response.headers.get("ETag"),
                            "cache_status": "downloaded"}
                self._cache[url] = {"body": body, "etag": metadata["etag"], "checked": completed, "metadata": metadata}
                # Bound cache growth across distinct feed/alert snapshots.
                while (len(self._cache) > 128 or
                       sum(len(entry["body"]) for entry in self._cache.values()) > 8_000_000):
                    del self._cache[next(iter(self._cache))]
                return body, metadata
        except HTTPError as exc:
            if exc.code == 304 and cached and cached["etag"]:
                checked = datetime.now(timezone.utc)
                cached["checked"] = checked
                cached["metadata"] = {**cached["metadata"], "validated_at": checked.isoformat()}
                return cached["body"], {**cached["metadata"], "cache_status": "revalidated"}
            if exc.code == 429:
                raise SachetError("HTTP 429 rate limited; defer requests; no automatic retry") from exc
            raise SachetError(f"SACHET HTTP {exc.code}") from exc
        except (URLError, OSError, HTTPException, ValueError) as exc:
            raise SachetError("SACHET transport or XML response unavailable") from exc

    def fetch(self, latitude, longitude):
        """Return schema 1.0 evidence; see docs/SACHET.md for every stable key.

        Invalid caller coordinates raise ValueError before requests. Expected
        source/XML failures produce unavailable/partial reports, never invented
        alerts. Matching is polygon-only; unresolved messages are never local.
        Incomplete retrieval snapshots withhold active_matching_alerts to avoid
        overlooking updates/cancellations. Lifecycle uncertainty quarantines
        linked messages while retaining independent verified matches; inspect
        coverage.lifecycle_resolved and warnings. Expiry is always reassessed.
        """
        latitude, longitude = validate_coordinates(latitude, longitude)
        with self._lock:
            return self._fetch(latitude, longitude)

    def _fetch(self, latitude, longitude):
        now = datetime.now(timezone.utc)
        deadline = time.monotonic() + self.total_seconds
        result = {
            "schema_version": "1.0", "source": "NDMA SACHET", "source_url": RSS_URL,
            "integration_guide_url": GUIDE_URL, "evaluated_at": now.isoformat(),
            "requested_coordinates": {"latitude": latitude, "longitude": longitude},
            "retrieval_status": "unavailable", "matching_status": "source_unavailable",
            "location_match": "unresolved", "active_matching_alerts": [], "alerts": [],
            "retrievals": [], "warnings": [], "limitations": list(LIMITATIONS),
            "coverage": {"complete_snapshot": False, "lifecycle_resolved": False,
                         "rss_items": None, "processed_messages": 0},
        }
        def warning(code, message):
            result["warnings"].append({"code": code, "message": message})
        try:
            body, metadata = self._get(RSS_URL, now, deadline)
            result["retrievals"].append(metadata)
            root = xml_root(body)
            if root.tag != "rss" or root.find("channel") is None:
                raise AlertDataError("Expected official RSS channel")
            items = root.findall("channel/item")
        except (SachetError, AlertDataError) as exc:
            warning("source_unavailable", str(exc))
            return result
        result["coverage"]["rss_items"] = len(items)
        urls, seen = [], set()
        for item in items:
            url = (item.findtext("link") or "").strip()
            try:
                _check_url(url)
                if url == RSS_URL:
                    raise SachetError("RSS item must link to a CAP document")
            except SachetError as exc:
                warning("invalid_item", str(exc))
                continue
            if url not in seen:
                urls.append(url)
                seen.add(url)
        if len(urls) > self.max_messages:
            warning("truncated_feed", "CAP message limit reached; snapshot is incomplete")
        messages = []
        for url in urls[:self.max_messages]:
            try:
                body, metadata = self._get(url, now, deadline)
                result["retrievals"].append(metadata)
                messages.append(parse_cap(body, url))
            except (SachetError, AlertDataError) as exc:
                warning("message_unavailable", str(exc))
        result["coverage"]["processed_messages"] = len(messages)
        evaluated = datetime.now(timezone.utc).isoformat()
        result["evaluated_at"] = evaluated
        try:
            result["alerts"] = reconcile(messages, latitude, longitude, evaluated)
        except AlertDataError as exc:
            warning("lifecycle_unresolved", str(exc))
        complete = not result["warnings"]
        result["coverage"]["complete_snapshot"] = complete
        lifecycle_uncertain = any(alert["lifecycle"] == "unresolved" for alert in result["alerts"])
        result["coverage"]["lifecycle_resolved"] = complete and not lifecycle_uncertain
        for alert in result["alerts"]:
            for issue in alert["lifecycle_issues"]:
                result["warnings"].append({
                    "code": issue["code"], "message": "Inspect CAP lifecycle issue; no unsupported relationship is treated as confirmed.",
                    "source_url": alert["source_url"], "identifier": alert["identifier"],
                    "reference": issue["reference"],
                })
        result["retrieval_status"] = "available" if complete and not lifecycle_uncertain else "partial"
        if not complete:
            result["matching_status"] = "incomplete_source"
            return result
        active = [alert for alert in result["alerts"] if alert["active"]]
        matched = [alert for alert in active if alert["location_match"] == "matched"]
        unknown_validity = any(section["temporal_status"] == "unresolved"
                               for alert in result["alerts"] for section in alert["info"])
        if unknown_validity:
            warning("validity_unresolved", "Missing expiry or inconsistent validity times prevent an active-alert claim")
        result["active_matching_alerts"] = matched
        if matched:
            result.update(location_match="matched", matching_status="active_matching_alerts")
        elif lifecycle_uncertain:
            result.update(location_match="unresolved", matching_status="alert_lifecycle_unresolved")
        elif unknown_validity:
            result.update(location_match="unresolved", matching_status="alert_validity_unresolved")
        elif any(alert["location_match"] == "unresolved" for alert in active):
            result.update(location_match="unresolved", matching_status="location_matching_unresolved")
            warning("location_unresolved", "No authoritative coordinate match for one or more active alerts")
        else:
            result.update(location_match="not_matched", matching_status="no_verified_active_matching_alerts")
        return result


_default_client = SachetClient()


def fetch_sachet_alerts(latitude, longitude, *, client=None):
    """Callable service for future integration; preserves process-local ETag cache.

    Returns a defensive copy, JSON-serializable without custom encoders. This
    service is intentionally not connected to Streamlit, evidence-v1, or Gemini.
    Pass a reusable SachetClient for independent cache/lifetime control.
    """
    return deepcopy((client or _default_client).fetch(latitude, longitude))
