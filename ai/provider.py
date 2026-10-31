"""Official OpenAI Responses adapter. Importing this module makes no requests."""

import os

from ai.contracts import REPORT_SCHEMA


class ConfigurationError(Exception):
    """Missing provider configuration; messages never include secret values."""


class ProviderError(Exception):
    """Sanitized provider failure with a machine-readable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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
