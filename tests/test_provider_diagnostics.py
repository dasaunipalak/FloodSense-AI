"""Offline diagnostic coverage; all SDK errors and responses are synthetic."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import unittest
from unittest.mock import Mock, patch

import httpx
from google.genai import errors

from ai.agent import investigate
from ai.gemini import GeminiProvider
from ai.provider import ProviderError
from test_agent import weather_fixture
from test_gemini import function, native


def api_error(http_status, status=None, *, headers=None, message="synthetic-private-message"):
    body = {"error": {"message": message}}
    if status is not None:
        body["error"]["status"] = status
    response = httpx.Response(http_status, headers=headers or {})
    cls = errors.ClientError if http_status < 500 else errors.ServerError
    return cls(http_status, body, response=response)


class DiagnosticValidationTests(unittest.TestCase):
    def test_optional_fields_preserve_constructor_compatibility(self):
        for code in ("provider_error", "provider_rate_limit", "provider_authentication",
                     "provider_auth_error", "provider_timeout"):
            error = ProviderError(code, "Sanitized message")
            self.assertEqual(error.code, code)
            self.assertEqual(str(error), "Sanitized message")
            self.assertEqual(error.diagnostics, {"http_status": None, "provider_status": None,
                "exception_type": None, "retry_after_seconds": None})

    def test_only_allowed_statuses_and_class_names_survive(self):
        for status in ProviderError.PROVIDER_STATUSES:
            self.assertEqual(ProviderError("x", "", provider_status=status).provider_status, status)
        for name in ProviderError.EXCEPTION_TYPES:
            self.assertEqual(ProviderError("x", "", exception_type=name).exception_type, name)
        for value in ("secret-token", "invalid_argument", "INVALID_ARGUMENT private", [], {}, True):
            with self.subTest(value=value):
                error = ProviderError("x", "", provider_status=value, exception_type=value)
                self.assertIsNone(error.provider_status)
                self.assertIsNone(error.exception_type)

    def test_http_status_requires_valid_integer_and_rejects_bool(self):
        for value in (None, True, False, "400", 400.0, 99, 600, float("nan"), {"secret": "private"}):
            self.assertIsNone(ProviderError("x", "", http_status=value).http_status)
        self.assertEqual(ProviderError("x", "", http_status=400).http_status, 400)

    def test_retry_seconds_reject_nonfinite_negative_and_unvalidated_text(self):
        for value in (None, True, -1, float("nan"), float("inf"), -float("inf"),
                      "120", "secret-token", {}, 10 ** 500):
            self.assertIsNone(ProviderError("x", "", retry_after_seconds=value).retry_after_seconds)
        for value in (0, 120, 0.5):
            self.assertEqual(ProviderError("x", "", retry_after_seconds=value).retry_after_seconds, value)


class GeminiDiagnosticTests(unittest.TestCase):
    def run_error(self, error):
        client = Mock()
        client.models.generate_content.side_effect = error
        result = investigate(0, 0, provider=GeminiProvider(client, "unit-test-model"))
        client.models.generate_content.assert_called_once()
        self.assertEqual(result["agent_status"], "provider_error")
        self.assertIsNone(result["final_report"])
        self.assertEqual(result["tool_call_trace"], [])
        self.assertEqual(result["evidence"], {})
        json.dumps(result, allow_nan=False)
        return result

    def test_http_status_matrix_preserves_existing_categories(self):
        cases = (
            (400, "INVALID_ARGUMENT", "provider_error", "ClientError"),
            (401, "UNAUTHENTICATED", "provider_authentication", "ClientError"),
            (403, "PERMISSION_DENIED", "provider_authentication", "ClientError"),
            (404, "NOT_FOUND", "provider_error", "ClientError"),
            (429, "RESOURCE_EXHAUSTED", "provider_rate_limit", "ClientError"),
            (500, "INTERNAL", "provider_error", "ServerError"),
            (503, "UNAVAILABLE", "provider_error", "ServerError"),
        )
        for http_status, status, code, name in cases:
            with self.subTest(http_status=http_status):
                result = self.run_error(api_error(http_status, status))
                error = result["errors"][0]
                self.assertEqual(error["code"], code)
                self.assertEqual(error["http_status"], http_status)
                self.assertEqual(error["provider_status"], status)
                self.assertEqual(error["exception_type"], name)
                self.assertIsNone(error["retry_after_seconds"])
                self.assertNotIn("synthetic-private-message", json.dumps(result))

    def test_numeric_retry_after_is_validated_and_not_used_for_retry(self):
        for value, expected in (("120", 120.0), (" 0.5 ", 0.5), ("0", 0.0),
                                ("-2", None), ("nan", None), ("inf", None),
                                ("secret-token", None), ("1e20", None), ("9" * 130, None)):
            with self.subTest(value=value):
                result = self.run_error(api_error(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": value}))
                self.assertEqual(result["errors"][0]["retry_after_seconds"], expected)

    def test_http_date_retry_after_is_converted_to_seconds(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for seconds, expected in ((120, 120.0), (-120, 0.0)):
            date = format_datetime(now + timedelta(seconds=seconds), usegmt=True)
            with patch("ai.gemini.datetime") as clock:
                clock.now.return_value = now
                result = self.run_error(api_error(503, "UNAVAILABLE", headers={"Retry-After": date}))
            self.assertEqual(result["errors"][0]["retry_after_seconds"], expected)

    def test_missing_metadata_retains_none_and_http_code_fallback(self):
        result = self.run_error(errors.APIError(400, {}))
        self.assertEqual(result["errors"][0]["http_status"], 400)
        self.assertIsNone(result["errors"][0]["provider_status"])
        self.assertIsNone(result["errors"][0]["retry_after_seconds"])
        # No HTTP response and no body code/status.
        error = errors.APIError(0, {})
        result = self.run_error(error)
        self.assertIsNone(result["errors"][0]["http_status"])
        self.assertIsNone(result["errors"][0]["provider_status"])

    def test_response_http_status_preferred_without_changing_category(self):
        error = errors.APIError(400, {}, response=httpx.Response(503))
        result = self.run_error(error)
        self.assertEqual(result["errors"][0]["http_status"], 503)
        self.assertEqual(result["errors"][0]["code"], "provider_error")

    def test_untrusted_metadata_messages_payloads_and_headers_do_not_leak(self):
        sentinel = "unit-test-sensitive-token"
        error = errors.ClientError(400, {"error": {"message": sentinel,
            "status": sentinel, "details": [{"extra": sentinel}]}},
            response=httpx.Response(400, headers={"Retry-After": sentinel, "Authorization": sentinel}))
        result = self.run_error(error)
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertIsNone(result["errors"][0]["provider_status"])
        self.assertIsNone(result["errors"][0]["retry_after_seconds"])
        self.assertNotIn("Authorization", json.dumps(result))
        self.assertNotIn("details", result["errors"][0])

    def test_unknown_exception_class_name_is_not_exposed(self):
        custom_type = type("unit-test-sensitive-class", (errors.APIError,), {})
        result = self.run_error(custom_type(400, {}))
        self.assertIsNone(result["errors"][0]["exception_type"])
        self.assertNotIn("unit-test-sensitive-class", json.dumps(result))

    def test_invalid_key_category_unchanged(self):
        error = errors.ClientError(400, {"error": {"status": "INVALID_ARGUMENT",
            "details": [{"reason": "API_KEY_INVALID"}], "message": "private"}})
        result = self.run_error(error)
        self.assertEqual(result["errors"][0]["code"], "provider_authentication")
        self.assertEqual(result["errors"][0]["provider_status"], "INVALID_ARGUMENT")
        self.assertNotIn("private", json.dumps(result))

    def test_error_after_real_tool_result_preserves_evidence_and_trace(self):
        client = Mock()
        client.models.generate_content.side_effect = [native(function()), api_error(503, "UNAVAILABLE")]
        with patch("ai.tools.fetch_weather", return_value=weather_fixture()) as fetch:
            result = investigate(0, 0, provider=GeminiProvider(client, "unit-test-model"))
        fetch.assert_called_once()
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(result["evidence"]["weather"]["data"], weather_fixture())
        self.assertEqual(len(result["tool_call_trace"]), 1)
        self.assertEqual(result["tool_call_trace"][0]["status"], "available")
        self.assertEqual(result["errors"][0]["http_status"], 503)


if __name__ == "__main__":
    unittest.main()
