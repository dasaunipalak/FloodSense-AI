"""Offline UI integration with the real agent loop and scripted provider responses."""
import asyncio
import os
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from test_reliability import OfflineTransportCase, APP_PATH
from test_agent import report
from ai.provider import ProviderError


class StreamlitAgentTests(OfflineTransportCase):
    def setUp(self):
        # Windows loopback initialization must precede the unchanged socket guard.
        self.loops = [asyncio.get_event_loop_policy().new_event_loop() for _ in range(12)]
        self.addCleanup(self.close_loops)
        pool = iter(self.loops)
        self.enterContext(patch("asyncio.new_event_loop", side_effect=lambda: next(pool)))
        super().setUp()
        self.provider = self.enterContext(patch("ai.agent.provider_from_environment")).return_value
        self.provider.respond.side_effect = [
            {"status": "completed", "output": [{"type": "function_call", "call_id": "ui-call",
             "name": "get_weather_evidence", "arguments": '{"latitude": 0, "longitude": 0}'}]},
            report(("weather",)),
        ]

    def close_loops(self):
        for loop in self.loops:
            if not loop.is_closed():
                loop.close()

    def app(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
        self.assertFalse(app.exception)
        return app

    def select(self, app):
        app.number_input[0].set_value(0.0)
        app.number_input[1].set_value(0.0)
        return app

    def test_initial_load_and_invalid_coordinates_never_call_model(self):
        app = self.app()
        self.provider.respond.assert_not_called()
        app.button[1].click().run()
        self.assertFalse(app.exception)
        self.provider.respond.assert_not_called()
        self.weather_http.assert_not_called()
        self.assertTrue(any("valid latitude" in e.value for e in app.error))

    def test_button_runs_real_agent_renders_report_trace_and_reuses_result(self):
        app = self.select(self.app())
        app.button[1].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.provider.respond.call_count, 2)
        saved = app.session_state["ai_investigation"]
        self.assertEqual(saved["coordinates"], (0.0, 0.0))
        self.assertEqual(saved["result"]["selected_coordinates"], {"latitude": 0.0, "longitude": 0.0})
        self.assertEqual(saved["result"]["agent_status"], "completed")
        self.assertTrue(any(e.label == "Investigation Trace" for e in app.expander))
        self.assertTrue(any("get_weather_evidence" in m.value for m in app.markdown))
        self.assertTrue(any("Missing information" == h.value for h in app.subheader))
        self.assertEqual(len(app.text), 6)
        self.assertTrue(any("Retrieved" in m.value for m in app.markdown))
        self.assertEqual(len(app.get("link_button")), 1)
        app.run()
        self.assertEqual(self.provider.respond.call_count, 2)
        self.weather_http.assert_called_once()

    def test_current_nonzero_coordinates_are_forwarded(self):
        app = self.app()
        app.number_input[0].set_value(29.2183)
        app.number_input[1].set_value(79.5130)
        with patch("ai.agent.investigate", return_value={"agent_status": "configuration_error", "final_report": None}) as agent:
            app.button[1].click().run()
            agent.assert_called_once_with(29.2183, 79.5130)
        self.assertFalse(app.exception)

    def test_coordinate_change_marks_previous_result_without_call(self):
        app = self.select(self.app())
        app.button[1].click().run()
        app.number_input[0].set_value(1.0).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("earlier location" in w.value for w in app.warning))
        self.assertEqual(app.session_state["ai_investigation"]["coordinates"], (0.0, 0.0))
        self.assertEqual(self.provider.respond.call_count, 2)

    def test_provider_error_preserves_existing_dashboard_and_diagnostics(self):
        app = self.select(self.app())
        app.button[0].click().run()
        self.provider.respond.side_effect = ProviderError("provider_rate_limit", "private", http_status=429, provider_status="RESOURCE_EXHAUSTED")
        app.button[1].click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("quota" in e.value for e in app.error))
        self.assertTrue(any(m.label == "Next 24h rainfall" for m in app.metric))
        self.assertGreater(len(app.dataframe), 0)
        self.assertEqual(len(app.get("deck_gl_json_chart")), 1)
        self.assertTrue(any("429" in j.value for j in app.json))
        self.assertEqual(len(app.text), 0)
        app.run()
        self.provider.respond.assert_called_once()
        self.assertNotIn("private", str([e.value for e in app.error]))

    def test_503_without_dashboard_evidence_distinguishes_agent_evidence(self):
        app = self.select(self.app())
        self.provider.respond.side_effect = ProviderError(
            "provider_error", "private", http_status=503, provider_status="UNAVAILABLE")
        app.button[1].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.metric), 0)
        self.assertEqual(len(app.text), 0)
        self.assertTrue(any("Any previously loaded dashboard evidence" in item.value
                            and "Agent-retrieved evidence, if any" in item.value for item in app.info))
        self.assertTrue(any("Weather: Not retrieved" in item.value for item in app.markdown))
        self.assertTrue(any("503" in item.value for item in app.json))
        app.run()
        self.provider.respond.assert_called_once()
        self.weather_http.assert_not_called()
        self.terrain_http.assert_not_called()

    def test_missing_key_uses_real_configuration_failure(self):
        from ai.provider import provider_from_environment
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "", "GEMINI_MODEL": "unit-test-model"}, clear=False), patch("ai.agent.provider_from_environment", side_effect=provider_from_environment):
            app = self.select(self.app())
            app.button[1].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("configuration" in e.value for e in app.error))
            self.provider.respond.assert_not_called()
            self.weather_http.assert_not_called()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertGreater(len(app.metric), 0)

    def test_unexpected_error_is_sanitized_and_replaces_prior_report(self):
        app = self.select(self.app())
        app.button[1].click().run()
        with patch("ai.agent.investigate", side_effect=RuntimeError("private-sentinel")) as agent:
            app.button[1].click().run()
            app.run()
            agent.assert_called_once_with(0.0, 0.0)
        self.assertFalse(app.exception)
        self.assertEqual(len(app.text), 0)
        self.assertNotIn("private-sentinel", str([e.value for e in app.error]))
