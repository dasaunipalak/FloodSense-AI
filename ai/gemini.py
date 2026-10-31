"""Google Gen AI native function calling adapted to the existing agent loop."""

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re

from ai.contracts import REPORT_SCHEMA, strict_json
from ai.provider import ConfigurationError, ProviderError


def _retry_after_seconds(value):
    """Read only Retry-After: numeric seconds or an aware HTTP-date."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        seconds = float(value)
    else:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    # Reuse the same numeric validation applied to every ProviderError.
    return ProviderError("provider_error", "", retry_after_seconds=seconds).retry_after_seconds


def _api_diagnostics(exc):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = getattr(exc, "code", None)
    headers = getattr(response, "headers", None)
    retry = headers.get("Retry-After") if headers is not None else None
    return {"http_status": status, "provider_status": getattr(exc, "status", None),
            "exception_type": type(exc).__name__,
            "retry_after_seconds": _retry_after_seconds(retry)}


class GeminiProvider:
    """Translate transport messages only; evidence selection stays with the LLM.

    The loop's Responses-shaped dictionaries are a provider-neutral internal
    contract. Google receives native Content/FunctionDeclaration/FunctionResponse
    objects. Full model Content objects are retained privately to preserve thought
    signatures and original function IDs; only real calls enter the public trace.
    """

    def __init__(self, client, model):
        self.client = client
        self.model = model
        self._turns = {}
        self._calls = {}
        self._sequence = 0

    @classmethod
    def from_environment(cls):
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        model = os.environ.get("GEMINI_MODEL", "").strip()
        if not key or not model:
            raise ConfigurationError("Set GEMINI_API_KEY and GEMINI_MODEL before investigating.")
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ConfigurationError("Install the project requirements to use the Gemini adapter.") from None
        return cls(genai.Client(
            api_key=key, vertexai=False,
            http_options=types.HttpOptions(timeout=30_000,
                retry_options=types.HttpRetryOptions(attempts=1)),
        ), model)

    def close(self):
        try:
            self.client.close()
        finally:
            self._turns.clear()
            self._calls.clear()

    def _contents(self, history):
        from google.genai import types

        contents = []
        for item in history:
            if item.get("role") == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=item["content"])]))
            elif item.get("type") == "reasoning":
                # The marker references the original Google Content object; it
                # is never a fabricated reasoning step or exposed in the trace.
                contents.append(self._turns[item["id"]])
            elif item.get("type") == "function_call":
                if item["call_id"] not in self._calls:
                    raise ValueError("Unknown native call context")
                # Already present in the full model Content above.
            elif item.get("type") == "function_call_output":
                name, native_id = self._calls[item["call_id"]]
                part = types.Part(function_response=types.FunctionResponse(
                    name=name, id=native_id, response=strict_json(item["output"]),
                ))
                # Parallel model calls must receive their results in one turn.
                if contents and contents[-1].role == "user":
                    contents[-1].parts.append(part)
                else:
                    contents.append(types.Content(role="user", parts=[part]))
            elif item.get("type") != "message":
                raise ValueError("Unsupported conversation item")
        return contents

    def _normalize(self, response):
        candidates = response.candidates or []
        if not candidates:
            blocked = response.prompt_feedback and response.prompt_feedback.block_reason
            return self._refusal() if blocked else {"status": "completed", "output": []}
        candidate = candidates[0]
        reason = candidate.finish_reason
        reason = getattr(reason, "value", reason)
        if reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
            return self._refusal()
        content = candidate.content
        if content is None or not content.parts:
            return {"status": "completed", "output": []}
        self._sequence += 1
        token = f"gemini_turn_{self._sequence}"
        self._turns[token] = content
        output = [{"type": "reasoning", "id": token}]
        text = []
        unsupported = False
        for index, part in enumerate(content.parts):
            if part.function_call is not None:
                native = part.function_call
                # IDs are local correlation handles for actual model calls,
                # including models that omit IDs or reuse them across rounds.
                call_id = f"{token}_call_{index}"
                self._calls[call_id] = (native.name, native.id)
                output.append({"type": "function_call", "call_id": call_id,
                               "name": native.name,
                               "arguments": json.dumps(native.args)})
            elif part.text is not None and not part.thought:
                text.append(part.text)
            elif not part.thought and part.thought_signature is None:
                # Unknown multimodal output is not an evidence report.
                unsupported = True
        if text:
            output.append({"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "".join(text)}]})
        return {"status": "completed" if reason == "STOP" and not unsupported else "incomplete", "output": output}

    @staticmethod
    def _refusal():
        return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "refusal", "refusal": "Provider blocked the response."}]}]}

    def respond(self, *, instructions, history, tools, allow_tools):
        import httpx
        from google.genai import errors, types

        try:
            config = {
                "system_instruction": instructions + "\nWhen finished, return ONLY a JSON object matching this schema, with no markdown fences:\n" + json.dumps(REPORT_SCHEMA),
                "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
                "candidate_count": 1, "max_output_tokens": 4000,
            }
            if allow_tools:
                declarations = []
                for tool in tools:
                    schema = deepcopy(tool["parameters"])
                    # Gemini's schema support differs from OpenAI strict mode;
                    # numeric coordinate enums are enforced by the shared loop.
                    for argument in schema["properties"].values():
                        if "enum" in argument:
                            argument["description"] = f"Use exactly {argument.pop('enum')[0]} for the selected location."
                    declarations.append(types.FunctionDeclaration(name=tool["name"],
                        description=tool["description"], parameters_json_schema=schema))
                config["tools"] = [types.Tool(function_declarations=declarations)]
                config["tool_config"] = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="AUTO"))
                # Gemini 2.5 does not support combining structured-output mode
                # with tools. The shared loop validates final JSON locally.
            else:
                config.update(response_mime_type="application/json", response_json_schema=REPORT_SCHEMA)
            response = self.client.models.generate_content(
                model=self.model, contents=self._contents(history),
                config=types.GenerateContentConfig(**config),
            )
            return self._normalize(response)
        except errors.APIError as exc:
            diagnostics = _api_diagnostics(exc)
            if exc.code == 429:
                raise ProviderError("provider_rate_limit", "Gemini rate or quota limit reached; no automatic retry was made.", **diagnostics) from None
            details = str(getattr(exc, "details", ""))
            if exc.code in (401, 403) or "API_KEY_INVALID" in details or "API key not valid" in details:
                raise ProviderError("provider_authentication", "Gemini authentication or access failed; check the configured key and permissions.", **diagnostics) from None
            raise ProviderError("provider_error", "Gemini rejected the request or was unavailable; raw provider details were withheld.", **diagnostics) from None
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderError("provider_timeout", "The Gemini request timed out; no automatic retry was made.") from None
        except (httpx.RequestError, OSError):
            raise ProviderError("provider_network_error", "The Gemini request could not reach the provider; no automatic retry was made.") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderError("invalid_response", "Gemini returned malformed output or incompatible message context.") from None
