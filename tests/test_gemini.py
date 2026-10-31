"""Offline Google SDK/agent integration. No credentials or live LLM requests."""

import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import httpx
from google.genai import errors, types

from ai.agent import investigate
from ai.contracts import tool_definitions
from ai.gemini import GeminiProvider
from ai.provider import ConfigurationError, provider_from_environment
from test_agent import weather_fixture, terrain_fixture, report


def native(*parts, finish="STOP"):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=list(parts)), finish_reason=finish,
    )])


def function(name="get_weather_evidence", args=None, call_id=None, signature=None):
    return types.Part(function_call=types.FunctionCall(
        name=name, args={"latitude": 0, "longitude": 0} if args is None else args, id=call_id,
    ), thought_signature=signature)


def final(ids=("weather",)):
    return native(types.Part(text=report(ids)["output"][0]["content"][0]["text"]))


class GeminiAgentTests(unittest.TestCase):
    def setUp(self):
        self.fw = self.enterContext(patch("ai.tools.fetch_weather", return_value=weather_fixture()))
        self.ft = self.enterContext(patch("ai.tools.fetch_terrain", return_value=terrain_fixture()))
        # No test may fall through to an actual aggregator request.
        self.aw = self.enterContext(patch("analysis.evidence.fetch_weather", return_value=weather_fixture()))
        self.at = self.enterContext(patch("analysis.evidence.fetch_terrain", return_value=terrain_fixture()))

    def run_agent(self, *responses):
        client = Mock()
        client.models.generate_content.side_effect = list(responses)
        provider = GeminiProvider(client, "unit-test-model")
        result = investigate(0, 0, provider=provider)
        json.dumps(result, allow_nan=False)
        return result, client, provider

    def test_native_tool_dispatch_response_and_source_grounding(self):
        result, client, _ = self.run_agent(native(function(call_id="native-id")), final())
        self.assertEqual(result["agent_status"], "completed")
        self.fw.assert_called_once_with(0.0, 0.0)
        self.ft.assert_not_called()
        self.assertEqual(result["tools_used"], ["get_weather_evidence"])
        self.assertEqual(result["tool_call_trace"][0]["tool"], "get_weather_evidence")
        self.assertEqual(json.loads(result["tool_call_trace"][0]["arguments"]), {"latitude": 0, "longitude": 0})
        self.assertEqual(result["tool_call_trace"][0]["status"], "available")
        sent = client.models.generate_content.call_args.kwargs["contents"]
        response = sent[-1].parts[0].function_response
        self.assertEqual(response.name, "get_weather_evidence")
        self.assertEqual(response.id, "native-id")
        self.assertEqual(response.response["evidence"]["data"], weather_fixture())
        self.assertEqual(result["final_report"]["sources_and_retrieval_timestamps"], result["evidence_source_references"])
        self.assertEqual(result["evidence_source_references"][0]["source_url"], weather_fixture()["source_url"])
        self.assertEqual(result["evidence_source_references"][0]["fetched_at"], weather_fixture()["fetched_at"])

    def test_timing_grounding_reaches_model_without_rewriting_evidence(self):
        # Guard against losing timing guidance on the post-tool reporting turn.
        result, client, _ = self.run_agent(native(function()), final())
        self.assertEqual(result["agent_status"], "completed")
        for call in client.models.generate_content.call_args_list:
            instruction = call.kwargs["config"].system_instruction
            self.assertIn("timestamps and timezone", instruction)
            self.assertIn("Do not describe an event as early, middle, or late", instruction)
            self.assertIn("timing is unavailable rather than inferring it", instruction)
        response = client.models.generate_content.call_args.kwargs["contents"][-1].parts[0].function_response
        self.assertEqual(response.response["evidence"]["data"], weather_fixture())

    def test_multiple_rounds_preserve_native_content_and_thought_signature(self):
        first = native(types.Part(text="private thought", thought=True),
                       function(call_id="same-id", signature=b"unit-test-signature"))
        second = native(function("get_terrain_evidence", call_id="same-id"))
        result, client, _ = self.run_agent(first, second, final(("weather", "terrain")))
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(result["tool_rounds"], 2)
        self.assertEqual(len(result["tool_call_trace"]), 2)
        trace_ids = [t["call_id"] for t in result["tool_call_trace"]]
        self.assertEqual(len(set(trace_ids)), 2)
        contents = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIs(contents[1], first.candidates[0].content)
        self.assertEqual(contents[1].parts[1].thought_signature, b"unit-test-signature")
        self.assertNotIn("private thought", json.dumps(result))
        self.assertNotIn("unit-test-signature", json.dumps(result))

    def test_parallel_calls_group_actual_results_in_one_user_turn(self):
        result, client, _ = self.run_agent(native(function(), function("get_terrain_evidence")),
                                            final(("weather", "terrain")))
        self.assertEqual(result["agent_status"], "completed")
        contents = client.models.generate_content.call_args.kwargs["contents"]
        self.assertEqual([c.role for c in contents], ["user", "model", "user"])
        responses = [p.function_response for p in contents[-1].parts]
        self.assertEqual([r.name for r in responses], ["get_weather_evidence", "get_terrain_evidence"])
        self.assertTrue(all(r.response["status"] == "available" for r in responses))

    def test_repeated_calls_use_existing_cache(self):
        result, _, _ = self.run_agent(native(function()), native(function()), final())
        self.fw.assert_called_once()
        self.assertEqual(result["tool_call_trace"][1]["result"]["reused_sources"], ["weather"])

    def test_combined_tool_uses_unchanged_engine(self):
        result, _, _ = self.run_agent(native(function("get_combined_evidence")), final(("weather", "terrain")))
        self.assertEqual(result["agent_status"], "completed")
        self.aw.assert_called_once()
        self.at.assert_called_once()
        self.fw.assert_not_called()
        self.ft.assert_not_called()

    def test_invalid_coordinates_and_unknown_tools_are_rejected(self):
        for part in (function(args={"latitude": 1, "longitude": 0}), function("unsupported")):
            result, client, _ = self.run_agent(native(part), native(types.Part(text="{}")))
            self.assertEqual(result["tool_call_trace"][0]["status"], "rejected")
            self.assertEqual(result["agent_status"], "invalid_response")
            response = client.models.generate_content.call_args.kwargs["contents"][-1].parts[0].function_response
            self.assertEqual(response.response["status"], "rejected")
        self.fw.assert_not_called()

    def test_malformed_function_args_reach_shared_validator(self):
        malformed = types.FunctionCall.model_construct(name="get_weather_evidence", args="bad", id=None)
        result, _, _ = self.run_agent(native(types.Part(function_call=malformed)), final(()))
        self.assertEqual(result["tool_call_trace"][0]["status"], "rejected")
        self.fw.assert_not_called()

    def test_empty_response_is_not_a_fabricated_report(self):
        for response in (types.GenerateContentResponse(), native()):
            result, _, _ = self.run_agent(response)
            self.assertEqual(result["agent_status"], "invalid_response")
            self.assertEqual(result["tool_call_trace"], [])
            self.assertIsNone(result["final_report"])

    def test_incomplete_response_keeps_trace_but_does_not_execute(self):
        result, _, _ = self.run_agent(native(function(), finish="MAX_TOKENS"))
        self.assertEqual(result["agent_status"], "invalid_response")
        self.assertEqual(result["tool_call_trace"][0]["status"], "blocked")
        self.fw.assert_not_called()

    def test_safety_refusal(self):
        result, _, _ = self.run_agent(native(types.Part(text="blocked"), finish="SAFETY"))
        self.assertEqual(result["agent_status"], "refused")
        self.assertIsNone(result["final_report"])

    def test_excessive_calls_are_limited_by_shared_loop(self):
        result, _, _ = self.run_agent(native(*(function() for _ in range(8))))
        self.assertEqual(result["agent_status"], "limit_reached")
        self.assertEqual(result["tool_executions"], 6)
        self.assertEqual(len(result["tool_call_trace"]), 8)
        self.fw.assert_called_once()

    def test_round_limit_uses_final_json_mode_without_tools(self):
        result, client, _ = self.run_agent(*(native(function()) for _ in range(5)), final())
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(result["tool_rounds"], 5)
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsNone(config.tools)
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIsNotNone(config.response_json_schema)

    def test_no_tool_execution_by_sdk_or_json_mode_combination(self):
        _, client, _ = self.run_agent(native(function()), final())
        config = client.models.generate_content.call_args_list[0].kwargs["config"]
        self.assertTrue(config.automatic_function_calling.disable)
        self.assertEqual(config.tool_config.function_calling_config.mode, "AUTO")
        self.assertIsNone(config.response_mime_type)
        self.assertIsNone(config.response_json_schema)
        self.assertEqual([f.name for f in config.tools[0].function_declarations],
                         [t["name"] for t in tool_definitions(0, 0)])

    def test_invented_sources_are_rejected(self):
        result, _, _ = self.run_agent(native(function()), final(("invented",)))
        self.assertEqual(result["agent_status"], "invalid_response")
        self.assertIsNone(result["final_report"])
        self.assertEqual(result["evidence"]["weather"]["data"], weather_fixture())

    def test_missing_evidence_stays_unknown(self):
        self.fw.return_value = weather_fixture(missing=True)
        result, _, _ = self.run_agent(native(function()), final())
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(result["tool_call_trace"][0]["status"], "unavailable")
        self.assertIsNone(result["evidence"]["weather"]["data"]["totals"]["24h"]["precipitation"])

    def test_sdk_errors_are_sanitized_and_never_retried(self):
        cases = [
            (errors.ClientError(400, {"error": {"message": "private", "details": [{"reason": "API_KEY_INVALID"}]}}), "provider_authentication"),
            (errors.ClientError(403, {"error": {"message": "private"}}), "provider_authentication"),
            (errors.ClientError(429, {"error": {"message": "private"}}), "provider_rate_limit"),
            (errors.ServerError(503, {"error": {"message": "private"}}), "provider_error"),
            (httpx.ReadTimeout("private"), "provider_timeout"),
            (httpx.ConnectError("private"), "provider_network_error"),
        ]
        for error, code in cases:
            with self.subTest(code=code):
                result, client, _ = self.run_agent(error)
                self.assertEqual(result["agent_status"], "provider_error")
                self.assertEqual(result["errors"][0]["code"], code)
                self.assertNotIn("private", json.dumps(result))
                client.models.generate_content.assert_called_once()
                self.assertEqual(result["tool_call_trace"], [])

    def test_provider_failure_after_tool_keeps_evidence(self):
        result, _, _ = self.run_agent(native(function()), httpx.ReadTimeout("private"))
        self.assertEqual(result["agent_status"], "provider_error")
        self.assertEqual(result["evidence"]["weather"]["data"], weather_fixture())
        self.assertEqual(len(result["tool_call_trace"]), 1)

    def test_google_sdk_http_serialization_and_signature_round_trip(self):
        from google import genai
        requests = []
        responses = [
            {"candidates": [{"content": {"role": "model", "parts": [{
                "functionCall": {"name": "get_weather_evidence", "args": {"latitude": 0, "longitude": 0}},
                "thoughtSignature": "dW5pdC10ZXN0",
            }]}, "finishReason": "STOP"}]},
            {"candidates": [{"content": {"role": "model", "parts": [{
                "text": report()["output"][0]["content"][0]["text"],
            }]}, "finishReason": "STOP"}]},
        ]

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=responses[len(requests) - 1])

        with genai.Client(api_key="unit-test-only", vertexai=False,
                http_options=types.HttpOptions(
                    client_args={"transport": httpx.MockTransport(handle), "trust_env": False},
                    retry_options=types.HttpRetryOptions(attempts=1),
                )) as client:
            result = investigate(0, 0, provider=GeminiProvider(client, "unit-test-model"))
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(len(requests), 2)
        declarations = requests[0]["tools"][0]["functionDeclarations"]
        self.assertEqual(declarations[0]["name"], "get_weather_evidence")
        contents = requests[1]["contents"]
        self.assertEqual(contents[1]["parts"][0]["thoughtSignature"], "dW5pdC10ZXN0")
        response = contents[2]["parts"][0]["functionResponse"]
        self.assertEqual(response["name"], "get_weather_evidence")
        self.assertEqual(response["response"]["evidence"]["data"], weather_fixture())


class SelectionTests(unittest.TestCase):
    def test_default_is_gemini_and_openai_is_explicit(self):
        for env, expected in (({}, "gemini"), ({"LLM_PROVIDER": "gemini"}, "gemini"),
                              ({"LLM_PROVIDER": "openai"}, "openai")):
            with patch.dict(os.environ, env, clear=True), \
                    patch("ai.gemini.GeminiProvider.from_environment") as gemini, \
                    patch("ai.provider.OpenAIProvider.from_environment") as openai:
                provider = provider_from_environment()
                if expected == "gemini":
                    self.assertIs(provider, gemini.return_value)
                    openai.assert_not_called()
                else:
                    self.assertIs(provider, openai.return_value)
                    gemini.assert_not_called()

    def test_invalid_selection_has_no_fallback(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "unknown"}, clear=True):
            with self.assertRaises(ConfigurationError):
                provider_from_environment()

    def test_missing_gemini_key_or_model(self):
        for env in ({}, {"GEMINI_MODEL": "unit-test-model"}, {"GEMINI_API_KEY": "unit-test-only"}):
            with patch.dict(os.environ, env, clear=True), patch("google.genai.Client") as client:
                with self.assertRaises(ConfigurationError):
                    GeminiProvider.from_environment()
                client.assert_not_called()

    def test_key_timeout_and_retry_configuration(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-only", "GEMINI_MODEL": "unit-test-model"}, clear=True), \
                patch("google.genai.Client") as client:
            provider = GeminiProvider.from_environment()
            self.assertEqual(provider.model, "unit-test-model")
            self.assertFalse(client.call_args.kwargs["vertexai"])
            options = client.call_args.kwargs["http_options"]
            self.assertEqual(options.timeout, 30_000)
            self.assertEqual(options.retry_options.attempts, 1)
            provider.close()
            client.return_value.close.assert_called_once()

    def test_agent_selects_gemini_without_rewriting_loop(self):
        client = Mock()
        client.models.generate_content.side_effect = [native(function()), final()]
        with patch.dict(os.environ, {}, clear=True), \
                patch("ai.gemini.GeminiProvider.from_environment", return_value=GeminiProvider(client, "unit-test-model")), \
                patch("ai.tools.fetch_weather", return_value=weather_fixture()):
            result = investigate(0, 0)
        self.assertEqual(result["agent_status"], "completed")
        self.assertEqual(result["tools_used"], ["get_weather_evidence"])
        client.close.assert_called_once()

    def test_root_env_path_and_no_override_are_explicit(self):
        # Re-execute only the config loader code, with dotenv mocked so the real
        # ignored .env cannot inject credentials into an offline test.
        import importlib.util
        import ai.provider
        path = Path(ai.provider.__file__)
        spec = importlib.util.spec_from_file_location("_config_test", path)
        module = importlib.util.module_from_spec(spec)
        with patch("dotenv.load_dotenv") as loader:
            spec.loader.exec_module(module)
        loader.assert_called_once_with(path.resolve().parents[1] / ".env", override=False)


if __name__ == "__main__":
    unittest.main()
