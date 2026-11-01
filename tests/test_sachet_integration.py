"""Offline SACHET UI and native Gemini integration; synthetic CAP fixtures only."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import socket
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest
from ai.agent import investigate
from ai.contracts import tool_definitions
from ai.gemini import GeminiProvider
from ai.tools import EvidenceTools
from services.sachet import fetch_sachet_alerts, SachetClient, RSS_URL
from test_sachet import NOW, cap, rss, url, Response
from test_gemini import native, function, final
from test_agent import weather_fixture, terrain_fixture
from test_reliability import OfflineTransportCase, APP_PATH


def fixture(*, polygon="10,70 10,71 11,71 11,70 10,70", expires="2026-11-01T11:00:00Z", cancel=False, incomplete=False, unavailable=False):
    client = SachetClient(max_messages=1 if incomplete else 20)
    docs = {RSS_URL: rss(1, 2) if cancel or incomplete else rss(1), url(1): cap(polygon=polygon, expires=expires),
            url(2): cap("2", kind="Cancel", references="IMD-Test-Issuer,1,2026-11-01T09:00:00+00:00", sent="2026-11-01T09:30:00Z")}
    def respond(request, **kwargs):
        if unavailable:
            raise OSError("synthetic private failure")
        return Response(docs[request.full_url])
    with patch("services.sachet.datetime") as clock, patch.object(client._opener, "open", side_effect=respond):
        clock.now.return_value = NOW
        return fetch_sachet_alerts(10.5, 70.5, client=client)


class OfficialToolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("Live network forbidden")))

    def test_registration_and_gemini_native_result_roundtrip(self):
        definitions = tool_definitions(10.5, 70.5)
        self.assertEqual(len(definitions), 4)
        self.assertEqual(definitions[-1]["name"], "get_official_alerts")
        data = fixture()
        client = Mock()
        client.models.generate_content.side_effect = [native(function("get_official_alerts", {"latitude":10.5,"longitude":70.5})), final(("official_alerts_0", "official_alerts_1"))]
        with patch("ai.tools.fetch_sachet_alerts", return_value=data) as fetch:
            result = investigate(10.5, 70.5, provider=GeminiProvider(client, "offline-model"))
        fetch.assert_called_once_with(10.5, 70.5)
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(result["tool_call_trace"][0]["tool"], "get_official_alerts")
        self.assertEqual(result["tool_call_trace"][0]["status"], "available")
        output = client.models.generate_content.call_args.kwargs["contents"][-1].parts[0].function_response
        self.assertEqual(output.name, "get_official_alerts")
        self.assertEqual(output.response["evidence"]["data"], data)
        config = client.models.generate_content.call_args_list[0].kwargs["config"]
        self.assertIn("get_official_alerts", [f.name for f in config.tools[0].function_declarations])
        self.assertIn("Unresolved geography is NOT a local warning", config.system_instruction)
        self.assertIn("investigation_summary", config.system_instruction)
        refs = result["final_report"]["sources_and_retrieval_timestamps"]
        self.assertEqual(refs[1]["source_url"], url(1))
        self.assertEqual(refs[1]["fetched_at"], data["retrievals"][1]["retrieved_at"])
        self.assertEqual(refs[1]["evaluated_at"], data["evaluated_at"])

    def test_uncertainty_and_failure_cache_are_preserved(self):
        for data in (fixture(polygon=None), fixture(incomplete=True), fixture(unavailable=True), fixture(cancel=True)):
            tools = EvidenceTools(10.5, 70.5)
            with patch("ai.tools.fetch_sachet_alerts", return_value=data) as fetch:
                first = tools.execute("get_official_alerts", '{"latitude":10.5,"longitude":70.5}')
                second = tools.execute("get_official_alerts", '{"latitude":10.5,"longitude":70.5}')
            fetch.assert_called_once()
            self.assertEqual(first["evidence"]["data"], data)
            self.assertEqual(first["status"], data["retrieval_status"])
            self.assertEqual(second["reused_sources"], ["official_alerts"])
            self.assertEqual(data["active_matching_alerts"], [])
            if data["retrieval_status"] == "unavailable":
                self.assertEqual(first["source_references"], [])

    def test_coordinate_restriction_and_budget(self):
        tools = EvidenceTools(10.5, 70.5)
        with patch("ai.tools.fetch_sachet_alerts") as fetch:
            with self.assertRaises(ValueError):
                tools.execute("get_official_alerts", '{"latitude":0,"longitude":0}')
            fetch.assert_not_called()
        client = Mock()
        client.models.generate_content.return_value = native(*(function("get_official_alerts", {"latitude":10.5,"longitude":70.5}) for _ in range(8)))
        with patch("ai.tools.fetch_sachet_alerts", return_value=fixture()) as fetch:
            result = investigate(10.5,70.5,provider=GeminiProvider(client,"offline-model"))
        self.assertEqual(result["agent_status"], "limit_reached")
        self.assertEqual(result["tool_executions"], 6)
        self.assertEqual(result["tool_call_trace"][-1]["status"], "blocked")
        fetch.assert_called_once()

    def test_sachet_then_combined_keeps_weather_contract_separate(self):
        tools = EvidenceTools(0,0)
        with patch("ai.tools.fetch_sachet_alerts", return_value=fixture()), patch("ai.tools.fetch_weather", return_value=weather_fixture()), patch("ai.tools.fetch_terrain", return_value=terrain_fixture()):
            tools.get_official_alerts(0,0)
            combined = tools.get_combined_evidence(0,0)
        self.assertEqual(combined["status"], "available")
        self.assertNotIn("official_alerts", combined)
        self.assertEqual(combined["warnings"], [])

    def test_unexpected_source_failure_sanitized_cached(self):
        tools = EvidenceTools(0,0)
        with patch("ai.tools.fetch_sachet_alerts", side_effect=RuntimeError("private-sentinel")) as fetch:
            tools.get_official_alerts(0,0)
            result = tools.get_official_alerts(0,0)
        fetch.assert_called_once()
        self.assertNotIn("private-sentinel", json.dumps(result))
        self.assertEqual(result["status"], "unavailable")


class OfficialDashboardTests(OfflineTransportCase):
    def setUp(self):
        loops = [asyncio.get_event_loop_policy().new_event_loop() for _ in range(18)]
        self.addCleanup(lambda: [loop.close() for loop in loops if not loop.is_closed()])
        pool = iter(loops)
        self.enterContext(patch("asyncio.new_event_loop", side_effect=lambda: next(pool)))
        super().setUp()
        self.clock = self.enterContext(patch("sachet_ui.datetime"))
        from datetime import datetime
        self.clock.fromisoformat.side_effect = datetime.fromisoformat
        self.clock.now.return_value = NOW
        self.fetch = self.enterContext(patch("services.sachet.fetch_sachet_alerts", return_value=fixture()))

    def app(self):
        app = AppTest.from_file(str(APP_PATH),default_timeout=30).run()
        self.assertFalse(app.exception)
        self.fetch.assert_not_called()
        app.number_input[0].set_value(10.5)
        app.number_input[1].set_value(70.5)
        return app

    def refresh(self, app):
        next(button for button in app.button if button.label == "Refresh Official Alerts").click().run()
        self.assertFalse(app.exception)
        return app

    def test_active_rendering_provenance_rerun_and_stale_coordinates(self):
        app = self.refresh(self.app())
        self.assertTrue(any("Verified active local alert" in item.value for item in app.markdown))
        text = " ".join(item.value for item in app.text)
        self.assertIn("Test issuing authority", text)
        self.assertIn("Severity: Severe", text)
        self.assertIn("Expires:", text)
        self.assertGreater(len(app.get("link_button")),0)
        app.run()
        self.fetch.assert_called_once_with(10.5,70.5)
        app.number_input[0].set_value(10.6).run()
        self.assertTrue(any("earlier location" in item.value for item in app.warning))
        self.fetch.assert_called_once()
        self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))
        self.assertEqual(app.session_state["sachet_result"]["coordinates"], (10.5, 70.5))
        app.number_input[0].set_value(10.5).run()
        self.assertTrue(any("Verified active local alert" in item.value for item in app.markdown))
        self.fetch.assert_called_once()

    def test_unresolved_incomplete_and_unavailable(self):
        for data, message in ((fixture(polygon=None), "applicability to this location could not be verified"),
                              (fixture(incomplete=True), "Only part of the feed"),
                              (fixture(unavailable=True), "source unavailable")):
            self.fetch.reset_mock()
            self.fetch.return_value = data
            app = self.refresh(self.app())
            self.assertTrue(any(message in item.value for item in app.warning))
            self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))

    def test_cancelled_expired_quarantined_and_stale_not_local_cards(self):
        quarantined = fixture()
        quarantined["active_matching_alerts"][0]["lifecycle"] = "unresolved"
        for data in (fixture(cancel=True), fixture(expires="2026-11-01T09:30:00Z"), quarantined):
            self.fetch.reset_mock()
            self.fetch.return_value=data
            app=self.refresh(self.app())
            self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))
        self.fetch.reset_mock()
        self.fetch.return_value=fixture()
        app=self.refresh(self.app())
        self.clock.now.return_value=NOW+timedelta(minutes=6)
        app.run()
        self.assertTrue(any("snapshot is stale" in item.value for item in app.warning))
        self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))
        self.fetch.assert_called_once()

    def test_only_matched_valid_sections_render_and_expiry_is_rechecked(self):
        data=fixture(expires="2026-11-01T10:02:00Z")
        unmatched=deepcopy(data["active_matching_alerts"][0]["info"][0])
        unmatched.update(location_match="unresolved", event="Unresolved section must not be local")
        data["active_matching_alerts"][0]["info"].append(unmatched)
        self.fetch.return_value=data
        app=self.refresh(self.app())
        self.assertNotIn("Unresolved section must not be local", " ".join(item.value for item in app.text))
        self.clock.now.return_value=NOW+timedelta(minutes=2)
        app.run()
        self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))
        self.fetch.assert_called_once()

    def test_no_match_is_not_a_safety_conclusion(self):
        self.fetch.return_value=fixture(polygon="20,70 20,71 21,71 21,70 20,70")
        app=self.refresh(self.app())
        self.assertTrue(any("does not establish absence of official alerts or safety" in item.value for item in app.info))
        self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))

    def test_source_exception_does_not_break_weather_terrain(self):
        app=self.app()
        app.button[0].click().run()
        self.fetch.side_effect=RuntimeError("private-sentinel")
        self.refresh(app)
        self.assertGreater(len(app.metric),0)
        self.assertEqual(len(app.get("deck_gl_json_chart")),1)
        self.assertTrue(any("source unavailable" in item.value for item in app.warning))
        self.assertNotIn("private-sentinel", str([item.value for item in app.warning]))
        app.run()
        self.fetch.assert_called_once()


    def test_source_validation_not_reevaluation_controls_freshness(self):
        data = fixture()
        data["retrievals"][0]["validated_at"] = (NOW - timedelta(minutes=5)).isoformat()
        data["evaluated_at"] = NOW.isoformat()
        self.fetch.return_value = data
        app = self.refresh(self.app())
        self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))
        self.assertTrue(any("freshness is unknown" in item.value for item in app.warning))
        app.run()
        self.fetch.assert_called_once()

    def test_invalid_or_missing_source_validation_fails_closed(self):
        from sachet_ui import _sources_fresh
        for value in (None, "", "invalid", "2026-11-01T10:00:00", 123,
                      (NOW + timedelta(seconds=1)).isoformat(),
                      (NOW - timedelta(minutes=6)).isoformat()):
            with self.subTest(value=value):
                data = fixture()
                data["retrievals"][1]["validated_at"] = value
                self.assertFalse(_sources_fresh(data, NOW))
        data = fixture()
        del data["retrievals"][0]["validated_at"]
        self.assertFalse(_sources_fresh(data, NOW))
        self.fetch.return_value = data
        app = self.refresh(self.app())
        self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))
        data = fixture()
        data["retrievals"].pop()
        self.assertFalse(_sources_fresh(data, NOW))
        self.assertTrue(_sources_fresh(fixture(), NOW))

    def test_agent_official_fetch_states_and_coordinate_mismatch(self):
        app = self.app().run()
        trace = [{"round": 1, "tool": "get_official_alerts", "status": "unavailable"}]
        cases = [
            ({}, [], "not invoked"),
            ({}, trace, "no usable evidence"),
            ({"official_alerts": {"status": "unavailable", "data": None}}, trace, "no usable evidence"),
            ({"official_alerts": {"status": "unavailable", "data": fixture(unavailable=True)}}, trace, "source was unavailable"),
        ]
        for evidence, calls, message in cases:
            app.session_state["ai_investigation"] = {"coordinates": (10.5,70.5), "result": {
                "agent_status": "provider_error", "final_report": None,
                "evidence": evidence, "tool_call_trace": calls}}
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(any(message in item.value for item in list(app.info)+list(app.warning)))
        app.session_state["ai_investigation"] = {"coordinates": (10.5,70.5), "result": {
            "agent_status": "provider_error", "final_report": None,
            "evidence": {"official_alerts": {"status": "available", "data": fixture()}}}}
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Verified active local alert" in item.value for item in app.markdown))
        app.number_input[0].set_value(10.6).run()
        self.assertTrue(any("earlier location" in item.value for item in app.warning))
        self.assertFalse(any("Verified active local alert" in item.value for item in app.markdown))
        self.assertEqual(app.session_state["ai_investigation"]["coordinates"], (10.5,70.5))
        self.fetch.assert_not_called()
