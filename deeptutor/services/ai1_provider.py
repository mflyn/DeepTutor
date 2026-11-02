"""Single-shot SDK path for AI-1, separate from chat retries and key rotation.

No counter is supplied by default: a model-profile-qualified bound on the whole
wire body is an enabling prerequisite. Synthetic counters prove plumbing only.
This module never reads runtime settings, credentials or environment at import.
"""

import asyncio
from contextvars import ContextVar
import copy
from dataclasses import dataclass
import json
import logging
import time

import httpx
import rfc8785

from deeptutor.services.ai1_contract import (
    ContractError,
    check_policy,
    integer,
    parse_request,
    validate_result,
)

ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_RESPONSE_BYTES = 65536
_private_sdk_call = ContextVar("ai1_private_sdk_call", default=False)


class _PrivateSdkFilter(logging.Filter):
    def filter(self, record):
        return not _private_sdk_call.get()


# The SDK logs complete request options at DEBUG. Restrict only this async
# context; other providers keep their configured logging levels and handlers.
_private_filter = _PrivateSdkFilter()
for _name in ("openai._base_client", "openai._response", "openai._legacy_response"):
    logging.getLogger(_name).addFilter(_private_filter)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError()

    return json.loads(
        raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=nonfinite
    )


@dataclass(frozen=True)
class PreparedCompletion:
    snapshot: bytes
    wire: bytes


class _SingleRequestTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner, prepared, validate):
        self.inner = inner
        self.prepared = prepared
        self.validate = validate
        self.attempted = False

    async def handle_async_request(self, request):
        # Count before validation as well: no SDK recovery may produce a second try.
        if self.attempted:
            raise ContractError("AI_REMOTE_UNAVAILABLE", 503)
        self.attempted = True
        if (
            request.method != "POST"
            or str(request.url) != ENDPOINT
            or rfc8785.dumps(_json(request.content)) != self.prepared.wire
        ):
            raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
        self.validate(self.prepared)

        async def before_write(event, _info):
            if event in {
                "http11.send_request_headers.started",
                "http11.send_request_body.started",
                "http2.send_request_headers.started",
                "http2.send_request_body.started",
            }:
                self.validate(self.prepared)

        # SDK JSON ordering is not the counted representation. Send those exact
        # immutable bytes, and re-check after connection/TLS before HTTP writes.
        headers = request.headers.copy()
        headers.pop("content-length", None)
        request = httpx.Request(
            request.method,
            request.url,
            headers=headers,
            content=self.prepared.wire,
            extensions={**request.extensions, "trace": before_write},
        )
        response = await self.inner.handle_async_request(request)
        try:
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
            size = 0
            chunks = []
            async for chunk in response.stream:
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
                chunks.append(chunk)
            return httpx.Response(
                response.status_code, headers=response.headers, content=b"".join(chunks)
            )
        finally:
            await response.aclose()

    async def aclose(self):
        await self.inner.aclose()


class RestrictedCompletionProvider:
    def __init__(self, *, profile, key_loader, counter=None, clock=None, transport_factory=None):
        self._profile = copy.deepcopy(profile)
        self._key_loader = key_loader
        self._counter = counter
        self._clock = clock or (lambda: time.time_ns() // 1000000)
        self._transport_factory = transport_factory or (
            lambda: httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
        )

    def _policy(self, snapshot):
        failure = check_policy(snapshot, self._profile, self._clock())
        if failure:
            raise ContractError(failure, 503)

    def prepare(self, snapshot):
        snapshot = parse_request(rfc8785.dumps(snapshot))
        self._policy(snapshot)
        if (
            self._counter is None
            or getattr(self._counter, "model_profile_version", None)
            != self._profile["modelProfileVersion"]
        ):
            raise ContractError("PROFILE_UNAVAILABLE", 503)
        wire = rfc8785.dumps(
            {
                "model": snapshot["requestOptions"]["model"],
                "messages": snapshot["messages"],
                "max_tokens": 4096,
                "stream": False,
                "thinking": {"type": "disabled"},
            }
        )
        try:
            bound = self._counter.upper_bound(wire)
        except Exception:
            raise ContractError("PROFILE_UNAVAILABLE", 503) from None
        if not integer(bound, 8192):
            raise ContractError("VALIDATION_ERROR")
        self._policy(snapshot)
        return PreparedCompletion(rfc8785.dumps(snapshot), wire)

    def _validate(self, prepared):
        if (
            type(prepared) is not PreparedCompletion
            or self.prepare(parse_request(prepared.snapshot)) != prepared
        ):
            raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)

    @staticmethod
    def _result(raw, model):
        value = _json(raw)
        if not isinstance(value, dict) or value.get("model") != model:
            raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
        choices = value.get("choices")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
        choice = choices[0]
        message = choice.get("message")
        if (
            type(choice.get("index")) is not int
            or choice["index"] != 0
            or choice.get("finish_reason") != "stop"
            or type(message) is not dict
            or message.get("role") != "assistant"
            or message.get("tool_calls") not in (None, [])
            or message.get("function_call") is not None
            or message.get("reasoning_content") not in (None, "")
        ):
            raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
        usage = value.get("usage")
        if usage is not None:
            if (
                type(usage) is not dict
                or not integer(usage.get("prompt_tokens"), 8192)
                or not integer(usage.get("completion_tokens"), 4096)
            ):
                raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
            if "total_tokens" in usage and (
                not integer(usage["total_tokens"])
                or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
            ):
                raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
            usage = {
                "inputTokens": usage["prompt_tokens"],
                "outputTokens": usage["completion_tokens"],
            }
        result, usage = validate_result(message.get("content"), usage)
        return result["text"], usage

    async def send(self, prepared):
        # The operation caller must have durably claimed before entering this method.
        # Do not use generic complete/chat: those paths can rotate keys or fall back.
        self._validate(prepared)
        private_token = _private_sdk_call.set(True)
        try:
            from openai import AsyncOpenAI

            key = self._key_loader()
            if not isinstance(key, str) or not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ContractError("PROFILE_UNAVAILABLE", 503)
            self._validate(prepared)
            seconds = self._profile["callWindowMs"] / 1000
            transport = _SingleRequestTransport(self._transport_factory(), prepared, self._validate)
            async with httpx.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False, timeout=seconds
            ) as http:
                async with AsyncOpenAI(
                    api_key=key,
                    base_url="https://api.deepseek.com",
                    organization="",
                    project="",
                    max_retries=0,
                    http_client=http,
                    timeout=seconds,
                ) as client:
                    wire = _json(prepared.wire)
                    async with asyncio.timeout(seconds):
                        response = await client.chat.completions.with_raw_response.create(
                            model=wire["model"],
                            messages=wire["messages"],
                            max_tokens=4096,
                            stream=False,
                            extra_body={"thinking": {"type": "disabled"}},
                            extra_headers={"Accept-Encoding": "identity"},
                        )
                    return self._result(response.http_response.content, wire["model"])
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ContractError("AI_REMOTE_UNAVAILABLE", 503) from None
        finally:
            _private_sdk_call.reset(private_token)
