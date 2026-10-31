"""Provider selection and the official OpenAI Responses adapter."""

import os
import math
from pathlib import Path

from dotenv import load_dotenv

from ai.contracts import REPORT_SCHEMA

# Resolve from this module, never from the current shell directory. Existing
# process variables take precedence (including empty values); do not overwrite.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


class ConfigurationError(Exception):
    """Missing provider configuration; messages never include secret values."""


class ProviderError(Exception):
    """Sanitized provider failure with a machine-readable code."""

    PROVIDER_STATUSES = frozenset({
        "INVALID_ARGUMENT", "NOT_FOUND", "PERMISSION_DENIED", "UNAUTHENTICATED",
        "RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL",
    })
    EXCEPTION_TYPES = frozenset({"APIError", "ClientError", "ServerError"})

    def __init__(self, code, message, *, http_status=None, provider_status=None,
                 exception_type=None, retry_after_seconds=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.provider_status = (provider_status if type(provider_status) is str
                                and provider_status in self.PROVIDER_STATUSES else None)
        self.exception_type = (exception_type if type(exception_type) is str
                               and exception_type in self.EXCEPTION_TYPES else None)
        self.retry_after_seconds = None
        if type(retry_after_seconds) in (int, float):
            try:
                if math.isfinite(retry_after_seconds) and retry_after_seconds >= 0:
                    self.retry_after_seconds = retry_after_seconds
            except OverflowError:
                pass

    @property
    def diagnostics(self):
        """Only validated scalars; never SDK objects, headers, or payloads."""
        return {"http_status": self.http_status, "provider_status": self.provider_status,
                "exception_type": self.exception_type,
                "retry_after_seconds": self.retry_after_seconds}


def provider_from_environment():
    """Choose one provider explicitly; errors never trigger paid auto-fallback."""
    name = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
    if name == "gemini":
        from ai.gemini import GeminiProvider
        return GeminiProvider.from_environment()
    if name == "openai":
        return OpenAIProvider.from_environment()
    raise ConfigurationError("LLM_PROVIDER must be gemini or openai.")


class OpenAIProvider:
    """Use native Responses function calls and strict structured final outputs."""

    def __init__(self, client, model):
        self.client = client
        self.model = model

    @classmethod
    def from_environment(cls):
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("OPENAI_MODEL", "").strip()
        if not key or not model:
            raise ConfigurationError("Set OPENAI_API_KEY and OPENAI_MODEL before investigating.")
        # The default adapter targets OpenAI, not an implicitly configured proxy.
        if os.environ.get("OPENAI_BASE_URL"):
            raise ConfigurationError("OPENAI_BASE_URL is not supported by the default OpenAI adapter.")
        try:
            from openai import OpenAI
        except ImportError:
            raise ConfigurationError("Install the project requirements to use the OpenAI adapter.") from None
        return cls(OpenAI(api_key=key, timeout=30.0, max_retries=0), model)

    def close(self):
        self.client.close()

    def respond(self, *, instructions, history, tools, allow_tools):
        from openai import APIError, APITimeoutError, RateLimitError

        try:
            response = self.client.responses.create(
                model=self.model, instructions=instructions, input=history,
                tools=tools, tool_choice="auto" if allow_tools else "none",
                parallel_tool_calls=False, store=False,
                include=["reasoning.encrypted_content"], max_output_tokens=4000,
                text={"format": {"type": "json_schema", "name": "flood_evidence_report",
                                 "schema": REPORT_SCHEMA, "strict": True}},
            )
            return response.model_dump(mode="json", exclude_none=True)
        except APITimeoutError:
            raise ProviderError("provider_timeout", "The LLM request timed out; no automatic retry was made.") from None
        except RateLimitError:
            raise ProviderError("provider_rate_limit", "The LLM provider rate limited this investigation.") from None
        except APIError:
            raise ProviderError("provider_error", "The LLM request failed; check configuration, access and connectivity.") from None
