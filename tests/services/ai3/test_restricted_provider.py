"""Synthetic counter/transport only; never evidence of real token qualification."""

import asyncio
import copy
from dataclasses import replace

import httpx
import pytest
import rfc8785

from deeptutor.services.ai1_contract import ContractError, iso_ms, request_hash
from deeptutor.services.ai1_provider import _SingleRequestTransport
from deeptutor.services.ai3_provider import RestrictedCompletionProvider
from tests.services.ai3.test_operations import PROFILE, body


@pytest.fixture
def prepared():
    clock = [1893456000000]
    calls, counted, keys = [], [], []

    class Counter:
        model_profile_version = PROFILE["modelProfileVersion"]
        bound = 8192

        def upper_bound(self, wire):
            counted.append(wire)
            return self.bound

    counter = Counter()

    async def transport(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "synthetic-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"instructions":"Synthetic","exercises":[]}',
                        },
                    }
                ],
            },
        )

    def key():
        keys.append(True)
        return "synthetic-test-only-key"

    provider = RestrictedCompletionProvider(
        profile=PROFILE,
        key_loader=key,
        counter=counter,
        clock=lambda: clock[0],
        transport_factory=lambda: httpx.MockTransport(transport),
    )
    return provider, counter, clock, calls, counted, keys


@pytest.mark.parametrize("invalid", ["missing", "version", "counter-error"])
def test_counter_rejection_before_secret_sdk_or_transport(prepared, invalid):
    provider, counter, _, calls, _, keys = prepared
    if invalid == "missing":
        provider._counter = None
    elif invalid == "version":
        counter.model_profile_version = "wrong-version"
    else:

        def reject(_):
            raise ValueError("synthetic expired counter qualification")

        counter.upper_bound = reject
    with pytest.raises(ContractError, match="PROFILE_UNAVAILABLE"):
        provider.prepare(body())
    assert calls == keys == []


@pytest.mark.parametrize("bound", [8191, 8192, 8193])
def test_complete_bound_boundary_and_zero_send_on_rejection(prepared, bound):
    provider, counter, _, calls, counted, keys = prepared
    counter.bound = bound
    if bound > 8192:
        with pytest.raises(ContractError, match="VALIDATION_ERROR"):
            provider.prepare(body())
        assert calls == keys == []
    else:
        result = asyncio.run(provider.send(provider.prepare(body())))
        assert result[0] == '{"instructions":"Synthetic","exercises":[]}'
        assert len(calls) == len(keys) == 1
        assert calls[0].content == counted[0]


@pytest.mark.parametrize("content", ["中文数位🙂", 'line\nquote"slash\\\t'])
def test_complete_system_user_options_and_unicode_wire_are_counted(prepared, content):
    provider, _, _, calls, counted, keys = prepared
    payload = body()
    payload["messages"][1]["content"] = content
    payload["requestHash"] = request_hash(payload)
    ready = provider.prepare(payload)
    expected = rfc8785.dumps(
        {
            "model": "synthetic-model",
            "messages": payload["messages"],
            "max_tokens": 4096,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
    )
    assert counted == [expected] and ready.wire == expected and not calls and not keys
    asyncio.run(provider.send(ready))
    assert len(calls) == 1 and calls[0].content == expected
    assert all(wire == expected for wire in counted)


@pytest.mark.parametrize("mutation", ["wire", "snapshot", "expiry", "counter-after-prepare"])
def test_prepared_request_and_qualification_rechecked_before_send(prepared, mutation):
    provider, counter, clock, calls, _, keys = prepared
    payload = body()
    ready = provider.prepare(payload)
    if mutation == "wire":
        ready = replace(ready, wire=ready.wire.replace(b"Synthetic", b"Changed"))
    elif mutation == "snapshot":
        changed = copy.deepcopy(payload)
        changed["messages"][1]["content"] = "changed after counting"
        changed["requestHash"] = request_hash(changed)
        ready = replace(ready, snapshot=rfc8785.dumps(changed))
    elif mutation == "expiry":
        clock[0] = iso_ms(payload["sendNotAfter"])
    else:
        counter.model_profile_version = "expired-version"
    with pytest.raises(ContractError):
        asyncio.run(provider.send(ready))
    assert calls == keys == []


def test_clock_expiry_during_counter_is_rejected_without_send(prepared):
    provider, counter, clock, calls, _, keys = prepared
    payload = body()

    def slow_count(_):
        clock[0] = iso_ms(payload["sendNotAfter"])
        return 8192

    counter.upper_bound = slow_count
    with pytest.raises(ContractError):
        provider.prepare(payload)
    assert calls == keys == []


def test_sdk_changed_wire_rejected_before_inner_transport(prepared):
    provider, _, _, calls, _, keys = prepared
    ready = provider.prepare(body())
    transport = _SingleRequestTransport(
        httpx.MockTransport(lambda request: calls.append(request)), ready, provider._validate
    )

    async def run():
        try:
            with pytest.raises(ContractError, match="AI_REMOTE_CONTRACT_INVALID"):
                await transport.handle_async_request(
                    httpx.Request(
                        "POST", "https://api.deepseek.com/chat/completions", content=b"{}"
                    )
                )
        finally:
            await transport.aclose()

    asyncio.run(run())
    assert calls == keys == []
