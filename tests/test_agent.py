"""Offline agent checks. Synthetic evidence and LLM responses exist only here."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import Mock, patch

from ai.agent import investigate
from ai.contracts import REPORT_SECTIONS, SYSTEM_INSTRUCTION, tool_definitions
from ai.provider import ConfigurationError, OpenAIProvider, ProviderError
from ai.tools import EvidenceTools
from analysis.evidence import build_evidence
from services.weather import WeatherError, parse_forecast
from services.terrain import TerrainError, TerrainRateLimitError, parse_terrain, sample_locations


def weather_fixture(missing=False):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return parse_forecast({
        "latitude": 0, "longitude": 0, "timezone": "GMT", "timezone_abbreviation": "GMT",
        "utc_offset_seconds": 0,
        "hourly_units": {"time": "unixtime", "precipitation": "mm", "precipitation_probability": "%"},
        "hourly": {"time": [int((now + timedelta(hours=i)).timestamp()) for i in range(1, 49)],
                   "precipitation": [None if missing else 0.0] * 48,
                   "precipitation_probability": [None if missing else 0.0] * 48},
    }, window_start=now, fetched_at=now, source_url="https://example.test/weather", coordinates=(0, 0))


def terrain_fixture(missing=False):
    locations = sample_locations(0, 0)
    data = parse_terrain({"status": "OK", "results": [
        {"dataset": "srtm90m", "location": {"lat": p["latitude"], "lng": p["longitude"]},
         "elevation": None if missing else value}
        for p, value in zip(locations, (0, -10, 10, 0, 0))
    ]}, locations=locations, source_url="https://example.test/terrain",
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    data["sample_radius_m"] = 250.0
    return data


def call(name="get_weather_evidence", call_id="call_1", arguments=None):
    return {"type": "function_call", "name": name, "call_id": call_id,
            "arguments": arguments if arguments is not None else '{"latitude":0,"longitude":0}'}


def turn(*items):
    return {"status": "completed", "output": list(items)}


def report(ids=("weather",)):
    data = {key: "Evidence is limited; consult local official advisories." for key in REPORT_SECTIONS}
    data["source_ids"] = list(ids)
    return turn({"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": json.dumps(data)}]})


def scripted(*responses):
    provider = Mock()
    # Save snapshots, since the real loop extends the same history list.
    provider.requests = []
    sequence = iter(responses)

    def respond(**kwargs):
        provider.requests.append(deepcopy(kwargs))
        response = next(sequence)
        if isinstance(response, Exception):
            raise response
        return response

    provider.respond.side_effect = respond
    return provider


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.weather = weather_fixture()
        self.terrain = terrain_fixture()
        self.fw = self.enterContext(patch("ai.tools.fetch_weather", return_value=self.weather))
        self.ft = self.enterContext(patch("ai.tools.fetch_terrain", return_value=self.terrain))
        # Keep the real aggregator implementation, replacing only its network inputs.
        self.aw = self.enterContext(patch("analysis.evidence.fetch_weather", return_value=self.weather))
        self.at = self.enterContext(patch("analysis.evidence.fetch_terrain", return_value=self.terrain))

    def run_agent(self, *responses):
        provider = scripted(*responses)
        result = investigate(0, 0, provider=provider)
        json.dumps(result, allow_nan=False)
        return result, provider

    def test_model_selects_only_weather_no_fixed_pipeline(self):
        result, provider = self.run_agent(turn(call()), report())
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(result["tools_used"], ["get_weather_evidence"])
        self.fw.assert_called_once_with(0.0, 0.0)
        self.ft.assert_not_called()
        self.aw.assert_not_called()
        self.assertEqual(result["evidence"]["weather"]["data"], self.weather)
        outputs = [x for x in provider.requests[1]["history"] if x.get("type") == "function_call_output"]
        self.assertEqual(outputs[0]["call_id"], "call_1")
        self.assertEqual(json.loads(outputs[0]["output"])["evidence"]["data"], self.weather)
        self.assertEqual(result["evidence_source_references"][0]["fetched_at"], self.weather["fetched_at"])

    def test_model_can_select_terrain_first(self):
        result, _ = self.run_agent(turn(call("get_terrain_evidence")), report(("terrain",)))
        self.assertEqual(result["agent_status"], "completed")
        self.ft.assert_called_once()
        self.fw.assert_not_called()
        self.assertEqual(result["evidence"]["terrain"]["data"]["points"][1]["elevation_m"], -10)

    def test_combined_cold_uses_existing_aggregator_and_seeds_cache(self):
        with patch("ai.tools.build_evidence", wraps=build_evidence) as combined:
            result, _ = self.run_agent(turn(call("get_combined_evidence")),
                turn(call("get_weather_evidence", "c2"), call("get_terrain_evidence", "c3")),
                report(("weather", "terrain")))
        combined.assert_called_once_with(0.0, 0.0)
        self.aw.assert_called_once()
        self.at.assert_called_once()
        self.fw.assert_not_called()
        self.ft.assert_not_called()
        self.assertEqual(result["tool_call_trace"][1]["result"]["reused_sources"], ["weather"])

    def test_individual_then_combined_reuses_and_fetches_only_missing(self):
        result, _ = self.run_agent(turn(call()), turn(call("get_combined_evidence", "c2")),
                                  report(("weather", "terrain")))
        self.fw.assert_called_once()
        self.ft.assert_called_once()
        self.aw.assert_not_called()
        self.at.assert_not_called()
        package = result["tool_call_trace"][1]["result"]["evidence"]
        reference = build_evidence(0, 0)
        package.pop("generated_at")
        reference.pop("generated_at")
        self.assertEqual(package, reference)

    def test_both_individuals_then_combined_and_repeated_combined(self):
        result, _ = self.run_agent(turn(call(), call("get_terrain_evidence", "c2")),
            turn(call("get_combined_evidence", "c3")), turn(call("get_combined_evidence", "c4")),
            report(("weather", "terrain")))
        self.fw.assert_called_once()
        self.ft.assert_called_once()
        self.aw.assert_not_called()
        self.assertEqual(result["tool_call_trace"][-1]["result"]["reused_sources"], ["weather", "terrain"])

    def test_repeated_individual_and_investigation_isolation(self):
        self.run_agent(turn(call()), turn(call(call_id="c2")), report())
        self.fw.assert_called_once()
        self.run_agent(turn(call()), report())
        self.assertEqual(self.fw.call_count, 2)

    def test_missing_weather_and_terrain_are_not_zero(self):
        self.fw.return_value = weather_fixture(missing=True)
        self.ft.return_value = terrain_fixture(missing=True)
        result, _ = self.run_agent(turn(call(), call("get_terrain_evidence", "c2")),
                                  report(("weather", "terrain")))
        self.assertEqual(result["agent_status"], "completed")
        for entry in result["evidence"].values():
            self.assertEqual(entry["status"], "unavailable")
            self.assertIsNone(entry["error"])
        self.assertIsNone(result["evidence"]["weather"]["data"]["totals"]["24h"]["precipitation"])
        self.assertIsNone(result["evidence"]["terrain"]["data"]["points"][0]["elevation_m"])

    def test_partial_weather_and_terrain(self):
        self.weather["hourly"][0]["precipitation_probability"] = None
        self.terrain["points"][1]["elevation_m"] = None
        result, _ = self.run_agent(turn(call(), call("get_terrain_evidence", "c2")),
                                  report(("weather", "terrain")))
        self.assertEqual(result["evidence"]["weather"]["status"], "partial")
        self.assertEqual(result["evidence"]["terrain"]["status"], "partial")

    def test_both_api_failures_have_no_fabricated_evidence(self):
        self.aw.side_effect = WeatherError("secret must not leak")
        self.at.side_effect = TerrainError("secret must not leak")
        result, _ = self.run_agent(turn(call("get_combined_evidence")), report(()))
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(result["evidence_source_references"], [])
        for entry in result["evidence"].values():
            self.assertIsNone(entry["data"])
            self.assertEqual(entry["status"], "unavailable")
        self.assertNotIn("secret must not leak", json.dumps(result))

    def test_rate_limit_is_cached_and_retry_after_preserved(self):
        self.ft.side_effect = TerrainRateLimitError("120")
        result, _ = self.run_agent(turn(call("get_terrain_evidence")),
            turn(call("get_combined_evidence", "c2")), report())
        self.ft.assert_called_once()
        self.assertEqual(result["evidence"]["terrain"]["error"]["retry_after"], "120")
        self.assertEqual(result["evidence"]["weather"]["data"], self.weather)

    def test_unknown_tool_and_invalid_arguments_are_rejected(self):
        cases = [("exec", '{}'), ("get_weather_evidence", 'not json'),
                 ("get_weather_evidence", '[]'), ("get_weather_evidence", '{}'),
                 ("get_weather_evidence", '{"latitude":0,"longitude":0,"extra":1}'),
                 ("get_weather_evidence", '{"latitude":true,"longitude":0}'),
                 ("get_weather_evidence", '{"latitude":"0","longitude":0}'),
                 ("get_weather_evidence", '{"latitude":91,"longitude":0}'),
                 ("get_weather_evidence", '{"latitude":1,"longitude":0}'),
                 ("get_weather_evidence", '{"latitude":NaN,"longitude":0}'),
                 ("get_weather_evidence", '{"latitude":1e999,"longitude":0}'),
                 ("get_weather_evidence", '{"latitude":' + '9' * 500 + ',"longitude":0}'),
                 ("get_weather_evidence", '{"latitude":0,"latitude":1,"longitude":0}')]
        for name, args in cases:
            with self.subTest(name=name, arguments=args):
                result, _ = self.run_agent(turn(call(name, arguments=args)), report(()))
                self.assertEqual(result["tool_call_trace"][0]["status"], "rejected")
                self.assertEqual(result["tools_used"], [])
                self.assertEqual(result["agent_status"], "invalid_response")
        self.fw.assert_not_called()
        self.ft.assert_not_called()

    def test_invalid_user_input_never_calls_provider(self):
        provider = Mock()
        for lat, lon in ((None, 0), (float("inf"), 0), (0, 181), (True, 0), (10 ** 500, 0)):
            with self.assertRaises(ValueError):
                investigate(lat, lon, provider=provider)
        with self.assertRaises(ValueError):
            investigate(0, 0, task="", provider=provider)
        provider.respond.assert_not_called()

    def test_round_limit_allows_final_report_but_no_sixth_tool_round(self):
        responses = [turn(call(call_id=f"c{i}")) for i in range(5)]
        result, provider = self.run_agent(*responses, report())
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(result["tool_rounds"], 5)
        self.assertFalse(provider.requests[-1]["allow_tools"])
        result, _ = self.run_agent(*responses, turn(call(call_id="sixth")))
        self.assertEqual(result["agent_status"], "limit_reached")
        self.assertEqual(result["tool_executions"], 5)
        self.assertEqual(result["tool_call_trace"][-1]["status"], "blocked")

    def test_execution_limit_blocks_overflow_and_records_all_calls(self):
        result, _ = self.run_agent(turn(*(call(call_id=f"c{i}") for i in range(8))))
        self.assertEqual(result["tool_executions"], 6)
        self.assertEqual(len(result["tool_call_trace"]), 8)
        self.assertEqual(result["agent_status"], "limit_reached")
        self.assertEqual([r["status"] for r in result["tool_call_trace"][-2:]], ["blocked", "blocked"])

    def test_six_executions_can_finalize_without_more_tools(self):
        result, provider = self.run_agent(turn(*(call(call_id=f"c{i}") for i in range(6))), report())
        self.assertEqual(result["agent_status"], "completed")
        self.assertFalse(provider.requests[-1]["allow_tools"])

    def test_duplicate_or_missing_call_id(self):
        for calls in ((call(call_id=None),), (call(), call())):
            result, _ = self.run_agent(turn(*calls))
            self.assertEqual(result["agent_status"], "invalid_response")
            self.assertEqual(len(result["tool_call_trace"]), len(calls))

    def test_malformed_and_incomplete_provider_responses(self):
        for response in (None, {}, {"status": "incomplete", "output": []}, turn(), turn(None),
                         turn({"type": "unexpected"}), turn({"type": "reasoning"})):
            result, _ = self.run_agent(response)
            self.assertEqual(result["agent_status"], "invalid_response")
            self.assertIsNone(result["final_report"])
        self.fw.assert_not_called()

    def test_no_tool_report_invented_citation_and_malformed_report_rejected(self):
        result, _ = self.run_agent(report())
        self.assertEqual(result["agent_status"], "invalid_response")
        for final in (report(("invented",)), report(()), turn({"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": "not json"}]})):
            result, _ = self.run_agent(turn(call()), final)
            self.assertEqual(result["agent_status"], "invalid_response")
            self.assertIsNone(result["final_report"])

    def test_provider_failure_after_tools_keeps_real_evidence_and_trace(self):
        for error in (ProviderError("provider_timeout", "Timed out."), RuntimeError("secret")):
            result, _ = self.run_agent(turn(call()), error)
            self.assertEqual(result["agent_status"], "provider_error")
            self.assertEqual(result["evidence"]["weather"]["data"], self.weather)
            self.assertIsNone(result["final_report"])
            self.assertNotIn("secret", json.dumps(result))

    def test_unexpected_service_error_stops_without_invention(self):
        self.fw.side_effect = RuntimeError("secret")
        result, _ = self.run_agent(turn(call()))
        self.assertEqual(result["agent_status"], "tool_error")
        self.assertEqual(result["evidence"], {})
        self.assertIsNone(result["final_report"])
        self.assertNotIn("secret", json.dumps(result))

    def test_failed_batch_records_remaining_calls_without_running_them(self):
        self.fw.side_effect = RuntimeError("secret")
        result, _ = self.run_agent(turn(call(), call("get_terrain_evidence", "c2")))
        self.assertEqual(result["agent_status"], "tool_error")
        self.assertEqual([t["status"] for t in result["tool_call_trace"]], ["error", "blocked"])
        self.ft.assert_not_called()

    def test_incomplete_response_preserves_unexecuted_call_trace(self):
        result, _ = self.run_agent({"status": "incomplete", "output": [call()]})
        self.assertEqual(result["agent_status"], "invalid_response")
        self.assertEqual(result["tool_call_trace"][0]["status"], "blocked")
        self.assertEqual(result["tool_executions"], 0)
        self.fw.assert_not_called()

    def test_report_links_and_empty_sections_are_rejected(self):
        for text in ("https://example.test/invented", ""):
            final = report()
            data = json.loads(final["output"][0]["content"][0]["text"])
            data["weather_findings"] = text
            final["output"][0]["content"][0]["text"] = json.dumps(data)
            result, _ = self.run_agent(turn(call()), final)
            self.assertEqual(result["agent_status"], "invalid_response")
            self.assertIsNone(result["final_report"])

    def test_malformed_service_data_is_not_cached(self):
        self.fw.return_value = {"hourly": [], "totals": {}}
        result, _ = self.run_agent(turn(call()))
        self.assertEqual(result["agent_status"], "tool_error")
        self.assertEqual(result["evidence"], {})

    def test_refusal(self):
        result, _ = self.run_agent(turn({"type": "message", "role": "assistant",
            "content": [{"type": "refusal", "refusal": "Declined"}]}))
        self.assertEqual(result["agent_status"], "refused")
        self.assertIsNone(result["final_report"])

    def test_reasoning_context_is_preserved_but_not_exposed_as_trace(self):
        reasoning = {"type": "reasoning", "id": "reasoning_1", "encrypted_content": "opaque"}
        result, provider = self.run_agent(turn(reasoning, call()), report())
        self.assertIn(reasoning, provider.requests[1]["history"])
        self.assertNotIn("opaque", json.dumps(result))

    def test_direct_tools_validate_coordinates_and_return_copies(self):
        tools = EvidenceTools(0, 0)
        with self.assertRaises(ValueError):
            tools.get_combined_evidence(1, 0)
        data = tools.get_weather_evidence(0, 0)
        data["data"]["source"] = "changed"
        self.assertEqual(tools.get_weather_evidence(0, 0)["data"]["source"], "Open-Meteo")


class ProviderTests(unittest.TestCase):
    def test_real_sdk_serialization_and_native_tool_output_round_trip(self):
        import httpx
        from openai import OpenAI
        requests = []
        responses = [turn(call()), report()]

        def handle(request):
            requests.append(json.loads(request.content))
            response = responses[len(requests) - 1]
            response.update(id=f"resp_{len(requests)}", object="response",
                            created_at=0, model="test-model")
            return httpx.Response(200, json=response)

        with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as http:
            with OpenAI(api_key="unit-test-only", http_client=http, max_retries=0) as client:
                with patch("ai.tools.fetch_weather", return_value=weather_fixture()):
                    result = investigate(0, 0, provider=OpenAIProvider(client, "test-model"))
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(len(requests), 2)
        outputs = [item for item in requests[1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(outputs[0]["call_id"], "call_1")
        self.assertEqual(json.loads(outputs[0]["output"])["status"], "available")

    def test_missing_configuration_does_not_call_services(self):
        with patch.dict(os.environ, {}, clear=True), patch("ai.tools.fetch_weather") as fetch:
            result = investigate(0, 0)
        self.assertEqual(result["agent_status"], "configuration_error")
        self.assertEqual(result["tool_call_trace"], [])
        fetch.assert_not_called()

    def test_environment_settings_and_timeout_no_retries(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-only", "OPENAI_MODEL": "test-model"}, clear=True), patch("openai.OpenAI") as sdk:
            provider = OpenAIProvider.from_environment()
            self.assertEqual(provider.model, "test-model")
            self.assertEqual(sdk.call_args.kwargs["timeout"], 30.0)
            self.assertEqual(sdk.call_args.kwargs["max_retries"], 0)
            provider.close()
            sdk.return_value.close.assert_called_once()

    def test_native_responses_payload_and_structured_schema(self):
        client = Mock()
        client.responses.create.return_value.model_dump.return_value = report()
        provider = OpenAIProvider(client, "test-model")
        provider.respond(instructions=SYSTEM_INSTRUCTION, history=[], tools=tool_definitions(0, 0), allow_tools=True)
        args = client.responses.create.call_args.kwargs
        self.assertEqual(args["tool_choice"], "auto")
        self.assertFalse(args["store"])
        self.assertFalse(args["parallel_tool_calls"])
        self.assertEqual(args["text"]["format"]["type"], "json_schema")
        self.assertTrue(args["text"]["format"]["strict"])
        for tool in args["tools"]:
            self.assertTrue(tool["strict"])
            self.assertEqual(tool["parameters"]["properties"]["latitude"]["enum"], [0])
        provider.respond(instructions="", history=[], tools=[], allow_tools=False)
        self.assertEqual(client.responses.create.call_args.kwargs["tool_choice"], "none")

    def test_sdk_errors_are_sanitized(self):
        import httpx
        from openai import APIConnectionError, APITimeoutError, RateLimitError
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        for error, code in (
            (APITimeoutError(request=request), "provider_timeout"),
            (APIConnectionError(request=request), "provider_error"),
            (RateLimitError("secret", response=httpx.Response(429, request=request), body=None), "provider_rate_limit"),
        ):
            client = Mock()
            client.responses.create.side_effect = error
            with self.assertRaises(ProviderError) as caught:
                OpenAIProvider(client, "test-model").respond(instructions="", history=[], tools=[], allow_tools=True)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret", str(caught.exception))

    def test_custom_base_url_not_silently_used(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-only", "OPENAI_MODEL": "test-model",
                                    "OPENAI_BASE_URL": "https://example.test"}, clear=True):
            with self.assertRaises(ConfigurationError):
                OpenAIProvider.from_environment()


if __name__ == "__main__":
    unittest.main()
