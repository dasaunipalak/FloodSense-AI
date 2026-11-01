"""Synthetic realistic RSS/CAP fixtures for offline tests, never production data."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from xml.sax.saxutils import escape

from analysis.sachet import AlertDataError, parse_cap, xml_root
from services.sachet import RSS_URL, SachetClient, SachetError, _NoRedirect, fetch_sachet_alerts

NOW = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
SENT = "2026-11-01T09:00:00+00:00"
POLYGON = "10,70 10,71 11,71 11,70 10,70"


def url(identifier):
    return "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=" + str(identifier)


def rss(*identifiers):
    return ("<rss version='2.0'><channel><title>All India: CAP Disaster Alert Feeds</title>"
            + "".join(f"<item><guid>{i}</guid><link>{escape(url(i))}</link>"
                      "<pubDate>Sun, 01 Nov 2026 09:00:00 GMT</pubDate></item>" for i in identifiers)
            + "</channel></rss>").encode()


def cap(identifier="1", *, sent=SENT, kind="Alert", references="", expires="2026-11-01T11:00:00Z",
        effective=SENT, polygon=POLYGON, languages=("en-IN",), status="Actual", scope="Public", sender="IMD-Test-Issuer"):
    # Real schema/field shapes; all values here are synthetic test data.
    infos = []
    for language in languages:
        infos.append(f"""<info><language>{language}</language><category>Met</category>
        <event>Heavy Rain</event><urgency>Expected</urgency><severity>Severe</severity>
        <certainty>Likely</certainty><senderName>Test issuing authority</senderName>
        {f'<effective>{escape(effective)}</effective>' if effective else ''}
        <onset>{sent}</onset>{f'<expires>{escape(expires)}</expires>' if expires else ''}
        <headline>Synthetic offline alert</headline><description>Test only</description>
        <area><areaDesc>Synthetic district</areaDesc>
        <geocode><valueName>LGD District Code</valueName><value>123</value></geocode>
        {f'<polygon>{polygon}</polygon>' if polygon is not None else ''}</area></info>""")
    return (f"<alert xmlns='urn:oasis:names:tc:emergency:cap:1.2'><identifier>{identifier}</identifier>"
            f"<sender>{sender}</sender><sent>{sent}</sent><status>{status}</status>"
            f"<msgType>{kind}</msgType><scope>{scope}</scope>"
            f"<references>{references}</references>" + "".join(infos) + "</alert>").encode()


class Response(BytesIO):
    def __init__(self, body, *, etag='"test-etag"', headers=None):
        super().__init__(body)
        self.status = 200
        self.headers = {"ETag": etag, **(headers or {})}


class SachetTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("Live network forbidden")))
        self.clock = self.enterContext(patch("services.sachet.datetime"))
        self.clock.now.return_value = NOW
        self.client = SachetClient()
        self.documents = {RSS_URL: rss(1), url(1): cap()}
        self.http = self.enterContext(patch.object(self.client._opener, "open", side_effect=self.respond))

    def respond(self, request, **kwargs):
        body = self.documents[request.full_url]
        if isinstance(body, Exception):
            raise body
        return Response(body)

    def fetch(self, latitude=10.5, longitude=70.5):
        result = fetch_sachet_alerts(latitude, longitude, client=self.client)
        json.dumps(result, allow_nan=False)
        return result

    def test_active_verified_polygon_alert_schema(self):
        result = self.fetch()
        self.assertEqual(result["retrieval_status"], "available")
        self.assertEqual(result["matching_status"], "active_matching_alerts")
        self.assertEqual(result["location_match"], "matched")
        alert = result["active_matching_alerts"][0]
        self.assertEqual(alert["identifier"], "1")
        self.assertEqual(alert["sender"], "IMD-Test-Issuer")
        self.assertEqual(alert["source_url"], url(1))
        info = alert["info"][0]
        self.assertEqual(info["senderName"], "Test issuing authority")
        self.assertEqual((info["event"], info["severity"], info["urgency"], info["certainty"]),
                         ("Heavy Rain", "Severe", "Expected", "Likely"))
        self.assertEqual(info["areas"][0]["lgd_district_codes"], ["123"])
        self.assertEqual(info["expires_original"], "2026-11-01T11:00:00Z")
        self.assertEqual(info["expires"], "2026-11-01T11:00:00+00:00")
        self.assertEqual(self.http.call_count, 2)

    def test_polygon_mismatch(self):
        result = self.fetch(0, 0)
        self.assertEqual(result["location_match"], "not_matched")
        self.assertEqual(result["matching_status"], "no_verified_active_matching_alerts")
        self.assertEqual(result["active_matching_alerts"], [])

    def test_names_and_lgd_codes_never_imply_coordinate_match(self):
        self.documents[url(1)] = cap(polygon=None)
        result = self.fetch()
        self.assertEqual(result["matching_status"], "location_matching_unresolved")
        self.assertEqual(result["location_match"], "unresolved")
        self.assertEqual(result["active_matching_alerts"], [])
        self.assertTrue(result["alerts"][0]["active"])

    def test_invalid_complex_dateline_or_polar_polygons_unresolved(self):
        for polygon in ("bad", "10,70 10,71 11,71", "0,0 1,1 0,1 1,0 0,0",
                        "0,179 0,-179 1,-179 1,179 0,179", "90,0 90,1 89,1 90,0"):
            with self.subTest(polygon=polygon):
                self.client.clear_cache()
                self.documents[url(1)] = cap(polygon=polygon)
                self.assertEqual(self.fetch()["location_match"], "unresolved")

    def test_polygon_boundary_is_included(self):
        self.assertEqual(self.fetch(10, 70)["location_match"], "matched")

    def test_expired_and_exact_expiry_not_active(self):
        self.documents[url(1)] = cap(expires=NOW.isoformat())
        result = self.fetch()
        self.assertEqual(result["active_matching_alerts"], [])
        self.assertEqual(result["alerts"][0]["info"][0]["temporal_status"], "expired")

    def test_missing_expiry_never_claims_no_active_alert(self):
        self.documents[url(1)] = cap(expires=None)
        result = self.fetch()
        self.assertEqual(result["matching_status"], "alert_validity_unresolved")
        self.assertEqual(result["active_matching_alerts"], [])

    def test_future_effective_not_yet_active(self):
        self.documents[url(1)] = cap(effective="2026-11-01T10:30:00Z")
        result = self.fetch()
        self.assertFalse(result["alerts"][0]["active"])
        self.assertEqual(result["alerts"][0]["info"][0]["temporal_status"], "pending")

    def test_test_and_private_messages_are_not_public_active(self):
        for kwargs in ({"status": "Test"}, {"scope": "Private"}):
            self.client.clear_cache()
            self.documents[url(1)] = cap(**kwargs)
            self.assertEqual(self.fetch()["active_matching_alerts"], [])

    def test_cancellation_suppresses_referenced_alert_even_if_order_reversed(self):
        self.documents[RSS_URL] = rss(2, 1)
        self.documents[url(2)] = cap("2", kind="Cancel", sent="2026-11-01T09:30:00Z",
                                     references=f"IMD-Test-Issuer,1,{SENT}", languages=())
        result = self.fetch()
        self.assertEqual(result["active_matching_alerts"], [])
        self.assertEqual(next(a for a in result["alerts"] if a["identifier"] == "1")["lifecycle"], "superseded")

    def test_update_replaces_original_without_restoring_expired_original(self):
        self.documents[RSS_URL] = rss(1, 2)
        self.documents[url(2)] = cap("2", kind="Update", sent="2026-11-01T09:30:00Z",
                                     references=f"IMD-Test-Issuer,1,{SENT}")
        self.assertEqual([a["identifier"] for a in self.fetch()["active_matching_alerts"]], ["2"])
        self.clock.now.return_value = NOW + timedelta(hours=2)
        self.assertEqual(self.fetch()["active_matching_alerts"], [])

    def test_cross_sender_cancellation_is_not_trusted(self):
        self.documents[RSS_URL] = rss(1, 2)
        self.documents[url(2)] = cap("2", kind="Cancel", sent="2026-11-01T09:30:00Z",
                                     sender="Other-Issuer", references=f"IMD-Test-Issuer,1,{SENT}")
        result = self.fetch()
        self.assertEqual(result["retrieval_status"], "partial")
        self.assertEqual(result["active_matching_alerts"], [])
        self.assertEqual(result["matching_status"], "alert_lifecycle_unresolved")
        self.assertEqual(len(result["alerts"]), 2)
        self.assertTrue(all(alert["lifecycle"] == "unresolved" for alert in result["alerts"]))

    def test_observed_sachet_cross_sender_update_preserves_independent_alerts(self):
        # Routing identities/times are from the approved 2026-11-01 CAP reads.
        # All other fixture fields (including polygons) are synthetic test data.
        self.clock.now.return_value = datetime(2026, 11, 1, 7, 45, tzinfo=timezone.utc)
        self.documents[RSS_URL] = rss(1, 2, 3)
        self.documents[url(1)] = cap("IN-1793518806917007_43", sender="IMD-Chennai",
            sent="2026-11-01T13:09:37+05:30", effective="2026-11-01T13:09:37+05:30")
        raw_reference = "IMD-Mumbai,IN-1793518105564029_46,2026-11-01T12:57:48+05:30"
        self.documents[url(2)] = cap("IN-1793518105564029_29", sender="Maharashtra-SDMA", kind="Update",
            sent="2026-11-01T13:02:29+05:30", effective="2026-11-01T13:02:29+05:30", references=raw_reference)
        self.documents[url(3)] = cap("IN-1793508125283029_46", sender="IMD-Mumbai",
            sent="2026-11-01T10:11:00+05:30", effective="2026-11-01T10:11:00+05:30")
        result = self.fetch()
        self.assertEqual(len(result["alerts"]), 3)
        update = next(a for a in result["alerts"] if a["sender"] == "Maharashtra-SDMA")
        self.assertEqual(update["references_original"], raw_reference)
        self.assertEqual(update["lifecycle"], "unresolved")
        self.assertFalse(update["active"])
        self.assertEqual(update["lifecycle_issues"][0]["code"], "cross_sender_authority_unresolved")
        self.assertEqual(len(result["active_matching_alerts"]), 2)
        self.assertEqual(result["retrieval_status"], "partial")
        self.assertTrue(result["coverage"]["complete_snapshot"])
        self.assertFalse(result["coverage"]["lifecycle_resolved"])

    def test_non_chronological_reference_quarantines_only_connected_messages(self):
        for reference_sent in ("2026-11-01T09:30:00Z", "2026-11-01T09:45:00Z"):
            with self.subTest(reference_sent=reference_sent):
                self.client.clear_cache()
                self.documents[RSS_URL] = rss(1, 2, 3)
                self.documents[url(2)] = cap("2", kind="Cancel", sent="2026-11-01T09:30:00Z",
                    references=f"IMD-Test-Issuer,1,{reference_sent}", languages=())
                self.documents[url(3)] = cap("3")
                result = self.fetch()
                self.assertEqual([a["identifier"] for a in result["active_matching_alerts"]], ["3"])
                related = [a for a in result["alerts"] if a["identifier"] != "3"]
                self.assertTrue(all(a["lifecycle"] == "unresolved" and not a["active"] for a in related))
                self.assertTrue(any(w["code"] == "non_chronological_reference" for w in result["warnings"]))

    def test_missing_referenced_predecessor_is_not_fabricated(self):
        self.documents[RSS_URL] = rss(2)
        self.documents[url(2)] = cap("2", kind="Update", sent="2026-11-01T09:30:00Z",
                                     references=f"IMD-Test-Issuer,1,{SENT}")
        result = self.fetch()
        self.assertEqual([a["identifier"] for a in result["alerts"]], ["2"])
        self.assertEqual([a["identifier"] for a in result["active_matching_alerts"]], ["2"])
        self.assertTrue(any(w["code"] == "referenced_message_not_in_snapshot" for w in result["warnings"]))

    def test_reference_timestamp_mismatch_does_not_leave_possible_target_active(self):
        self.documents[RSS_URL] = rss(1, 2, 3)
        self.documents[url(2)] = cap("2", kind="Update", sent="2026-11-01T09:30:00Z",
                                     references="IMD-Test-Issuer,1,2026-11-01T08:59:00Z")
        self.documents[url(3)] = cap("3")
        result = self.fetch()
        self.assertEqual([a["identifier"] for a in result["active_matching_alerts"]], ["3"])
        self.assertTrue(any(w["code"] == "reference_identity_mismatch" for w in result["warnings"]))

    def test_uncertainty_propagates_through_later_updates(self):
        self.documents[RSS_URL] = rss(1, 2, 3)
        self.documents[url(2)] = cap("2", sender="Other-Issuer", kind="Update", sent="2026-11-01T09:10:00Z",
                                     references=f"IMD-Test-Issuer,1,{SENT}")
        self.documents[url(3)] = cap("3", sender="Other-Issuer", kind="Update", sent="2026-11-01T09:20:00Z",
                                     references="Other-Issuer,2,2026-11-01T09:10:00Z")
        result = self.fetch()
        self.assertTrue(all(a["lifecycle"] == "unresolved" and not a["active"] for a in result["alerts"]))
        self.assertEqual(result["active_matching_alerts"], [])

    def test_valid_multiple_references_cancel_all_targets(self):
        self.documents[RSS_URL] = rss(1, 2, 3)
        self.documents[url(2)] = cap("2")
        self.documents[url(3)] = cap("3", kind="Cancel", sent="2026-11-01T09:30:00Z",
            references=f"IMD-Test-Issuer,1,{SENT}\nIMD-Test-Issuer,2,{SENT}", languages=())
        result = self.fetch()
        self.assertEqual(result["active_matching_alerts"], [])
        self.assertEqual(sum(a["lifecycle"] == "superseded" for a in result["alerts"]), 2)

    def test_malformed_references_retain_unrelated_evidence_but_withhold_local_claims(self):
        for malformed in ("missing-fields", "sender,,2026-11-01T09:00:00Z", "sender,1,no-time", "sender,1,2026-11-01T09:00:00"):
            with self.subTest(malformed=malformed):
                self.client.clear_cache()
                self.documents[RSS_URL] = rss(1, 2)
                self.documents[url(2)] = cap("2", kind="Cancel", sent="2026-11-01T09:30:00Z", references=malformed)
                result = self.fetch()
                self.assertEqual(len(result["alerts"]), 1)
                self.assertEqual(result["alerts"][0]["identifier"], "1")
                self.assertEqual(result["matching_status"], "incomplete_source")
                self.assertEqual(result["active_matching_alerts"], [])
                # The malformed message could target anything; retaining evidence
                # is safe, but a complete matching list cannot be established.

    def test_truncation_still_withholds_independent_matches(self):
        self.client.max_messages = 1
        self.documents[RSS_URL] = rss(1, 2)
        result = self.fetch()
        self.assertTrue(result["alerts"][0]["active"])
        self.assertEqual(result["active_matching_alerts"], [])
        self.assertEqual(result["matching_status"], "incomplete_source")

    def test_duplicate_feed_items_and_languages_do_not_duplicate_alert(self):
        self.documents[RSS_URL] = rss(1, 1)
        self.documents[url(1)] = cap(languages=("en-IN", "hi-IN", "en-IN"))
        result = self.fetch()
        self.assertEqual(len(result["active_matching_alerts"]), 1)
        self.assertEqual(len(result["alerts"][0]["info"]), 3)
        self.assertEqual(self.http.call_count, 2)

    def test_duplicate_cap_identity_at_two_urls_deduplicates(self):
        self.documents[RSS_URL] = rss(1, 2)
        self.documents[url(2)] = cap()
        self.assertEqual(len(self.fetch()["alerts"]), 1)

    def test_conflicting_duplicate_identity_withholds_matches(self):
        self.documents[RSS_URL] = rss(1, 2)
        self.documents[url(2)] = cap(polygon=None)
        self.assertEqual(self.fetch()["matching_status"], "incomplete_source")

    def test_invalid_timestamps_do_not_become_active(self):
        for bad in ("bad", "2026-11-01T09:00:00", "2026-13-01T09:00:00Z"):
            self.client.clear_cache()
            self.documents[url(1)] = cap(sent=bad)
            result = self.fetch()
            self.assertEqual(result["retrieval_status"], "partial")
            self.assertEqual(result["active_matching_alerts"], [])

    def test_non_utc_timestamps_preserve_original_and_normalize(self):
        self.documents[url(1)] = cap(sent="2026-11-01T14:30:00+05:30")
        alert = self.fetch()["alerts"][0]
        self.assertEqual(alert["sent"], SENT)
        self.assertEqual(alert["sent_original"], "2026-11-01T14:30:00+05:30")

    def test_invalid_xml_dtd_entity_and_utf16_are_rejected(self):
        bad_documents = (b"<broken", b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss>&x;</rss>',
                         b'<!DOCTYPE rss SYSTEM "file:///etc/passwd"><rss/>',
                         '<rss/>'.encode('utf-16-le'))
        for body in bad_documents:
            with self.subTest(body=body), self.assertRaises(AlertDataError):
                xml_root(body)

    def test_malformed_feed_is_unavailable(self):
        self.documents[RSS_URL] = b"<broken"
        result = self.fetch()
        self.assertEqual(result["retrieval_status"], "unavailable")
        self.assertEqual(result["matching_status"], "source_unavailable")

    def test_malformed_cap_is_partial_not_no_alerts(self):
        self.documents[url(1)] = b"<broken"
        result = self.fetch()
        self.assertEqual(result["matching_status"], "incomplete_source")
        self.assertFalse(result["coverage"]["complete_snapshot"])

    def test_http_errors_rate_limit_timeout_and_network_failure(self):
        for error in (HTTPError(RSS_URL, 503, "unavailable", {}, None),
                      HTTPError(RSS_URL, 429, "limited", {}, None),
                      TimeoutError(), URLError("offline")):
            self.client.clear_cache()
            self.documents[RSS_URL] = error
            self.assertEqual(self.fetch()["retrieval_status"], "unavailable")
        self.assertEqual(self.http.call_count, 4)

    def test_ttl_cache_and_defensive_copy(self):
        first = self.fetch()
        first["active_matching_alerts"][0]["identifier"] = "changed-by-caller"
        result = self.fetch()
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(result["alerts"][0]["identifier"], "1")
        self.assertTrue(all(r["cache_status"] == "ttl_hit" for r in result["retrievals"]))

    def test_304_uses_cached_xml_and_etag(self):
        first = self.fetch()
        self.clock.now.return_value = NOW + timedelta(minutes=6)
        seen = []
        def unchanged(request, **kwargs):
            seen.append(request.get_header("If-none-match"))
            raise HTTPError(request.full_url, 304, "unchanged", {}, None)
        self.http.side_effect = unchanged
        result = self.fetch()
        self.assertEqual(seen, ['"test-etag"', '"test-etag"'])
        self.assertEqual(result["alerts"], first["alerts"])
        self.assertEqual(result["retrievals"][0]["retrieved_at"], NOW.isoformat())
        self.assertEqual(result["retrievals"][0]["validated_at"], (NOW + timedelta(minutes=6)).isoformat())
        self.assertEqual(result["retrievals"][0]["cache_status"], "revalidated")

    def test_304_without_cache_is_unavailable(self):
        self.documents[RSS_URL] = HTTPError(RSS_URL, 304, "unchanged", {}, None)
        self.assertEqual(self.fetch()["retrieval_status"], "unavailable")

    def test_failed_revalidation_never_returns_stale_alert(self):
        self.fetch()
        self.clock.now.return_value = NOW + timedelta(minutes=6)
        self.documents[RSS_URL] = HTTPError(RSS_URL, 503, "offline", {}, None)
        result = self.fetch()
        self.assertEqual(result["retrieval_status"], "unavailable")
        self.assertEqual(result["active_matching_alerts"], [])

    def test_changed_etag_replaces_cached_cancellation(self):
        self.fetch()
        self.clock.now.return_value = NOW + timedelta(minutes=6)
        self.documents[RSS_URL] = rss(1, 2)
        self.documents[url(2)] = cap("2", kind="Cancel", sent="2026-11-01T09:30:00Z",
                                     references=f"IMD-Test-Issuer,1,{SENT}", languages=())
        self.http.side_effect = lambda request, **kwargs: Response(self.documents[request.full_url], etag='"new-etag"')
        result = self.fetch()
        self.assertEqual(result["active_matching_alerts"], [])
        self.assertTrue(all(row["etag"] == '"new-etag"' for row in result["retrievals"]))

    def test_cap_failure_withholds_matching_sibling(self):
        self.documents[RSS_URL] = rss(1, 2)
        self.documents[url(2)] = HTTPError(url(2), 503, "offline", {}, None)
        result = self.fetch()
        self.assertEqual(result["retrieval_status"], "partial")
        self.assertTrue(result["alerts"][0]["active"])
        self.assertEqual(result["active_matching_alerts"], [])

    def test_update_without_reference_is_incomplete(self):
        self.documents[url(1)] = cap(kind="Update")
        self.assertEqual(self.fetch()["matching_status"], "incomplete_source")

    def test_inverted_validity_is_unresolved(self):
        self.documents[url(1)] = cap(effective="2026-11-01T11:30:00Z")
        self.assertEqual(self.fetch()["matching_status"], "alert_validity_unresolved")

    def test_foreign_redirect_is_rejected_before_following(self):
        with self.assertRaises(SachetError):
            _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://127.0.0.1/")

    def test_reject_non_cap_xml_and_excessive_xml_nodes(self):
        with self.assertRaises(AlertDataError):
            parse_cap(b"<alert/>", url(1))
        with self.assertRaises(AlertDataError):
            xml_root(b"<rss>" + b"<x/>" * 20001 + b"</rss>")

    def test_expiry_is_rechecked_inside_ttl(self):
        self.documents[url(1)] = cap(expires=(NOW + timedelta(minutes=1)).isoformat())
        self.assertEqual(len(self.fetch()["active_matching_alerts"]), 1)
        self.clock.now.return_value = NOW + timedelta(minutes=2)
        self.assertEqual(self.fetch()["active_matching_alerts"], [])
        self.assertEqual(self.http.call_count, 2)

    def test_message_budget_partial_snapshot_withholds_otherwise_matching_alert(self):
        self.client.max_messages = 1
        self.documents[RSS_URL] = rss(1, 2)
        result = self.fetch()
        self.assertEqual(result["retrieval_status"], "partial")
        self.assertEqual(result["active_matching_alerts"], [])
        self.assertEqual(self.http.call_count, 2)

    def test_untrusted_external_url_is_never_requested(self):
        for unsafe in ('http://sachet.ndma.gov.in/a', 'https://evil.example/a',
                       'https://sachet.ndma.gov.in@127.0.0.1/a',
                       'https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=1&x=2'):
            self.client.clear_cache()
            self.http.reset_mock()
            self.documents[RSS_URL] = f'<rss><channel><item><link>{escape(unsafe)}</link></item></channel></rss>'.encode()
            result = self.fetch()
            self.assertEqual(result["retrieval_status"], "partial")
            self.http.assert_called_once()

    def test_download_byte_and_declared_size_limit(self):
        self.client.max_bytes = 10
        self.assertEqual(self.fetch()["retrieval_status"], "unavailable")
        self.client.clear_cache()
        self.http.side_effect = lambda *a, **k: Response(b"<rss/>", headers={"Content-Length": "99999999"})
        self.assertEqual(self.fetch()["retrieval_status"], "unavailable")

    def test_deadline_stops_network_requests(self):
        with patch("services.sachet.time.monotonic", side_effect=[0, 31]):
            self.assertEqual(self.fetch()["retrieval_status"], "unavailable")
        self.http.assert_not_called()

    def test_no_alerts_does_not_claim_safety(self):
        self.documents[RSS_URL] = rss()
        result = self.fetch()
        self.assertEqual(result["matching_status"], "no_verified_active_matching_alerts")
        self.assertTrue(any("not evidence" in text for text in result["limitations"]))

    def test_invalid_coordinates_never_fetch(self):
        for latitude, longitude in ((91, 0), (0, -181), (True, 0), ("1", 0), (float("nan"), 0)):
            with self.assertRaises(ValueError):
                self.fetch(latitude, longitude)
        self.http.assert_not_called()

    def test_invalid_client_limits(self):
        for kwargs in ({"timeout": 0}, {"ttl_seconds": float("nan")}, {"max_bytes": True},
                       {"max_messages": 0}, {"total_seconds": -1}):
            with self.assertRaises(ValueError):
                SachetClient(**kwargs)


if __name__ == "__main__":
    unittest.main()
