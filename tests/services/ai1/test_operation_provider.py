"""Real OpenAI SDK serialization, synthetic counter and HTTP transport only."""

import asyncio
import copy
from dataclasses import replace
import importlib
import json
import logging
from pathlib import Path

import httpx
import pytest
import rfc8785

from deeptutor.services.ai1_contract import ContractError, iso_ms
from deeptutor.services.ai1_operation import OperationService
from deeptutor.services.ai1_store import OperationStore

GOLDEN = json.loads(Path(__file__).with_name("lms_wire_golden.json").read_text())
PROFILE = {
    "model": "synthetic-model",
    "modelProfileVersion": "synthetic-v1",
    "pricingProfileVersion": "synthetic-price-v1",
    "checkedAt": "2029-12-31T00:00:00.000Z",
    "expiresAt": "2030-01-01T01:00:00.000Z",
    "callWindowMs": 5000,
    "clockMarginMs": 1000,
    "inputPriceMicrosPerMillion": 1000000,
    "outputPriceMicrosPerMillion": 2000000,
}


def provider_type():
    assert importlib.util.find_spec("deeptutor.services.ai1_provider") is not None
    return importlib.import_module("deeptutor.services.ai1_provider").RestrictedCompletionProvider


def completion(**updates):
    value = {
        "id": "synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": "synthetic-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "6180"},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
    value.update(updates)
    return value


@pytest.fixture
def setup():
    cls = provider_type()
    body = copy.deepcopy(GOLDEN)
    profile = copy.deepcopy(PROFILE)
    clock = [1893456000000]
    calls, counted, credentials = [], [], []
    response = [httpx.Response(200, json=completion())]

    class SyntheticCounter:
        model_profile_version = profile["modelProfileVersion"]
        bound = 8192

        def upper_bound(self, wire):
            counted.append(wire)
            return self.bound

    counter = SyntheticCounter()

    async def handler(request):
        calls.append(request)
        if isinstance(response[0], Exception):
            raise response[0]
        return response[0]

    def key():
        credentials.append(True)
        return "synthetic-ai1-provider-token"

    def make(**options):
        return cls(
            profile=profile,
            key_loader=key,
            clock=lambda: clock[0],
            transport_factory=lambda: httpx.MockTransport(handler),
            **{"counter": counter, **options},
        )

    return make, body, profile, clock, calls, counted, credentials, response, counter


def test_prepare_is_pure_and_sdk_sends_exact_complete_request(setup):
    make, body, _, _, calls, counted, keys, _, _ = setup
    provider = make()
    prepared = provider.prepare(body)
    assert not calls and not keys
    expected = {
        "model": "synthetic-model",
        "messages": body["messages"],
        "max_tokens": 4096,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    assert counted == [rfc8785.dumps(expected)]
    assert asyncio.run(provider.send(prepared)) == (
        "6180",
        {"inputTokens": 100, "outputTokens": 50},
    )
    assert len(calls) == len(keys) == 1
    assert calls[0].url == "https://api.deepseek.com/chat/completions"
    assert calls[0].method == "POST"
    assert json.loads(calls[0].content) == expected
    assert calls[0].content == counted[0]
    assert int(calls[0].headers["content-length"]) == len(counted[0])
    assert all(wire == rfc8785.dumps(expected) for wire in counted)


@pytest.mark.parametrize("bound", [8193, -1, True, None, 1.5])
def test_invalid_or_over_limit_input_bound_never_resolves_key_or_sends(setup, bound):
    make, body, _, _, calls, _, keys, _, counter = setup
    counter.bound = bound
    with pytest.raises(ContractError):
        make().prepare(body)
    assert calls == keys == []


def test_missing_or_wrong_counter_profile_fails_closed(setup):
    make, body, _, _, calls, _, keys, _, counter = setup
    with pytest.raises(ContractError, match="PROFILE_UNAVAILABLE"):
        make(counter=None).prepare(body)
    counter.model_profile_version = "another-profile"
    with pytest.raises(ContractError, match="PROFILE_UNAVAILABLE"):
        make().prepare(body)
    assert calls == keys == []


@pytest.mark.parametrize("status", [301, 307, 400, 401, 429, 500, 503])
def test_sdk_errors_and_redirects_never_retry(setup, status):
    make, body, _, _, calls, _, _, response, _ = setup
    response[0] = httpx.Response(
        status,
        headers={
            "location": "https://other.invalid/secret",
            "retry-after": "0",
            "x-should-retry": "true",
        },
        json={"error": {"message": "synthetic private error"}},
    )
    provider = make()
    with pytest.raises(ContractError, match="AI_REMOTE_UNAVAILABLE") as error:
        asyncio.run(provider.send(provider.prepare(body)))
    assert "private" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("error", [httpx.ConnectError("synthetic"), httpx.ReadTimeout("synthetic")])
def test_transport_failures_never_retry(setup, error):
    make, body, _, _, calls, _, _, response, _ = setup
    response[0] = error
    provider = make()
    with pytest.raises(ContractError):
        asyncio.run(provider.send(provider.prepare(body)))
    assert len(calls) == 1


def test_expiry_after_prepare_or_during_key_loading_prevents_transport(setup):
    make, body, profile, clock, calls, _, _, _, counter = setup
    provider = make()
    prepared = provider.prepare(body)
    clock[0] = iso_ms(body["sendNotAfter"])
    with pytest.raises(ContractError):
        asyncio.run(provider.send(prepared))
    clock[0] -= 1

    def key():
        clock[0] += 1
        return "synthetic-ai1-provider-token"

    provider = provider_type()(
        profile=profile,
        key_loader=key,
        counter=counter,
        clock=lambda: clock[0],
        transport_factory=lambda: httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json=completion())
        ),
    )
    prepared = provider.prepare(body)
    with pytest.raises(ContractError):
        asyncio.run(provider.send(prepared))
    assert calls == []


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": "100", "completion_tokens": 1},
        {"prompt_tokens": 8193, "completion_tokens": 1},
        {"prompt_tokens": 100, "completion_tokens": 4097},
        {"prompt_tokens": 100},
        {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 102},
    ],
)
def test_raw_usage_rejects_coercion_overrun_and_inconsistent_totals(setup, usage):
    make, body, _, _, calls, _, _, response, _ = setup
    response[0] = httpx.Response(200, json=completion(usage=usage))
    provider = make()
    with pytest.raises(ContractError):
        asyncio.run(provider.send(provider.prepare(body)))
    assert len(calls) == 1


def test_missing_usage_is_not_fabricated_as_zero(setup):
    make, body, _, _, _, _, _, response, _ = setup
    response[0] = httpx.Response(200, json=completion(usage=None))
    provider = make()
    assert asyncio.run(provider.send(provider.prepare(body))) == ("6180", None)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": "6180", "tool_calls": [{}]},
        {"role": "assistant", "content": "6180", "reasoning_content": "thought"},
        {"role": "assistant", "content": "6180", "function_call": {}},
        {"role": "assistant", "content": "\ufeff"},
    ],
)
def test_unexpected_tools_thinking_and_invalid_text_rejected(setup, message):
    make, body, _, _, _, _, _, response, _ = setup
    response[0] = httpx.Response(
        200, json=completion(choices=[{"index": 0, "finish_reason": "stop", "message": message}])
    )
    provider = make()
    with pytest.raises(ContractError):
        asyncio.run(provider.send(provider.prepare(body)))


@pytest.mark.parametrize(
    "change",
    [{"model": "another-model"}, {"choices": []}, {"choices": completion()["choices"] * 2}],
)
def test_ambiguous_provider_result_is_not_a_candidate(setup, change):
    make, body, _, _, _, _, _, response, _ = setup
    response[0] = httpx.Response(200, json=completion(**change))
    provider = make()
    with pytest.raises(ContractError):
        asyncio.run(provider.send(provider.prepare(body)))


def test_response_is_bounded_before_sdk_parsing(setup):
    make, body, _, _, calls, _, _, response, _ = setup
    response[0] = httpx.Response(200, content=b" " * 65537)
    provider = make()
    with pytest.raises(ContractError):
        asyncio.run(provider.send(provider.prepare(body)))
    assert len(calls) == 1


def test_proxy_and_openai_environment_cannot_change_fixed_request(setup, monkeypatch):
    make, body, _, _, calls, _, _, _, _ = setup
    for name in [
        "HTTPS_PROXY",
        "ALL_PROXY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
    ]:
        monkeypatch.setenv(name, "https://unexpected.invalid")
    provider = make()
    asyncio.run(provider.send(provider.prepare(body)))
    assert len(calls) == 1 and calls[0].url.host == "api.deepseek.com"
    assert calls[0].headers.get("OpenAI-Organization") != "https://unexpected.invalid"
    assert calls[0].headers.get("OpenAI-Project") != "https://unexpected.invalid"


def test_provider_error_after_durable_claim_remains_unknown_on_replay(setup, tmp_path):
    make, body, profile, clock, calls, _, _, response, _ = setup
    response[0] = httpx.Response(429, json={"error": "synthetic rate limit"})
    store = OperationStore(tmp_path / "operations.sqlite", initialize=True)
    service = OperationService(
        store=store,
        provider=make(),
        profile=profile,
        mode="enabled",
        continuous=True,
        clock=lambda: clock[0],
    )
    operation_id = "11111111-1111-4111-8111-111111111111"
    raw = json.dumps(body).encode()
    service.receive(operation_id, raw)
    asyncio.run(service.dispatch(operation_id))
    result = service.get(operation_id)
    assert result["status"] == "unknown" and result["sendClaimed"] and result["usage"] is None
    assert service.receive(operation_id, raw) == (result, False)
    asyncio.run(service.dispatch(operation_id))
    assert len(calls) == 1


def test_rotated_credentials_cannot_resend_durable_unknown(setup, tmp_path):
    _, body, profile, clock, _, _, _, _, counter = setup
    calls, keys = [], []
    credential = ["synthetic-old-token"]

    async def handler(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(401, json={"error": "synthetic expired credential"})

    def key():
        keys.append(credential[0])
        return credential[0]

    path = tmp_path / "rotation.sqlite"

    def make(initialize=False):
        provider = provider_type()(
            profile=profile,
            counter=counter,
            clock=lambda: clock[0],
            key_loader=key,
            transport_factory=lambda: httpx.MockTransport(handler),
        )
        return OperationService(
            store=OperationStore(path, initialize=initialize),
            provider=provider,
            profile=profile,
            mode="enabled",
            continuous=True,
            clock=lambda: clock[0],
        )

    operation_id = "11111111-1111-4111-8111-111111111111"
    raw = json.dumps(body).encode()
    first = make(True)
    first.receive(operation_id, raw)
    asyncio.run(first.dispatch(operation_id))
    terminal = first.get(operation_id)
    assert terminal["status"] == "unknown" and terminal["sendClaimed"]
    before = path.read_bytes()
    credential[0] = "synthetic-replacement-token"
    restarted = make()
    assert restarted.receive(operation_id, raw) == (terminal, False)
    asyncio.run(restarted.dispatch(operation_id))
    assert restarted.get(operation_id) == terminal
    assert calls == ["Bearer synthetic-old-token"] and keys == ["synthetic-old-token"]
    assert path.read_bytes() == before


def test_prepared_snapshot_is_detached_and_tampered_wire_cannot_send(setup):
    make, body, _, _, calls, _, keys, _, _ = setup
    provider = make()
    prepared = provider.prepare(body)
    body["messages"][0]["content"] = "changed after preparation"
    wire = json.loads(prepared.wire)
    wire["thinking"] = {"type": "enabled"}
    with pytest.raises(ContractError):
        asyncio.run(provider.send(replace(prepared, wire=rfc8785.dumps(wire))))
    assert calls == keys == []
    asyncio.run(provider.send(prepared))
    assert len(calls) == 1 and "changed after preparation" not in calls[0].content.decode()


@pytest.mark.parametrize("change", ["target", "method", "body", "duplicate"])
def test_last_transport_guard_rejects_wire_drift_and_a_second_attempt(setup, change):
    make, body, _, _, calls, _, _, _, _ = setup
    provider = make()
    prepared = provider.prepare(body)
    module = importlib.import_module("deeptutor.services.ai1_provider")

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json=completion())

    guard = module._SingleRequestTransport(
        httpx.MockTransport(handler), prepared, provider._validate
    )
    raw = prepared.wire
    url = module.ENDPOINT
    method = "POST"
    if change == "target":
        url += "?hidden=true"
    elif change == "method":
        method = "GET"
    elif change == "body":
        value = json.loads(raw)
        value["tools"] = [{"type": "function"}]
        raw = rfc8785.dumps(value)
    else:
        raw = raw[:-1] + b',"stream":false}'

    async def run():
        try:
            with pytest.raises(Exception):
                await guard.handle_async_request(httpx.Request(method, url, content=raw))
            with pytest.raises(ContractError):
                await guard.handle_async_request(
                    httpx.Request("POST", module.ENDPOINT, content=prepared.wire)
                )
        finally:
            await guard.aclose()

    asyncio.run(run())
    assert calls == []


def test_transport_guard_blocks_second_valid_request(setup):
    make, body, _, _, calls, _, _, _, _ = setup
    provider = make()
    prepared = provider.prepare(body)
    module = importlib.import_module("deeptutor.services.ai1_provider")
    guard = module._SingleRequestTransport(
        httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json=completion())
        ),
        prepared,
        provider._validate,
    )

    async def run():
        try:
            await guard.handle_async_request(
                httpx.Request("POST", module.ENDPOINT, content=prepared.wire)
            )
            with pytest.raises(ContractError):
                await guard.handle_async_request(
                    httpx.Request("POST", module.ENDPOINT, content=prepared.wire)
                )
        finally:
            await guard.aclose()

    asyncio.run(run())
    assert len(calls) == 1


def test_transport_construction_crossing_deadline_does_not_send(setup):
    _, body, profile, clock, calls, _, _, _, counter = setup

    def factory():
        clock[0] = iso_ms(body["sendNotAfter"])
        return httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json=completion())
        )

    provider = provider_type()(
        profile=profile,
        counter=counter,
        clock=lambda: clock[0],
        key_loader=lambda: "synthetic-token",
        transport_factory=factory,
    )
    prepared = provider.prepare(body)
    with pytest.raises(ContractError):
        asyncio.run(provider.send(prepared))
    assert calls == []


def test_default_transport_requires_tls_ignores_environment_and_disables_connect_retry(
    setup, monkeypatch
):
    _, _, profile, _, _, _, _, _, _ = setup
    observed = []
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: observed.append(kwargs))
    provider = provider_type()(profile=profile, key_loader=lambda: "synthetic-token")
    provider._transport_factory()
    assert observed == [{"verify": True, "trust_env": False, "retries": 0}]


def test_absent_counter_is_persisted_not_sent_without_claim(setup, tmp_path):
    make, body, profile, clock, calls, _, keys, _, _ = setup
    service = OperationService(
        store=OperationStore(tmp_path / "operations.sqlite", initialize=True),
        provider=make(counter=None),
        profile=profile,
        mode="enabled",
        continuous=True,
        clock=lambda: clock[0],
    )
    operation_id = "11111111-1111-4111-8111-111111111111"
    result, _ = service.receive(operation_id, json.dumps(body).encode())
    assert (result["status"], result["sendClaimed"], result["error"]) == (
        "failed_not_sent",
        False,
        "PROFILE_UNAVAILABLE",
    )
    asyncio.run(service.dispatch(operation_id))
    assert calls == keys == []


def test_whole_call_timeout_closes_transport_and_does_not_retry(setup):
    _, body, profile, clock, calls, _, _, _, counter = setup
    profile["callWindowMs"] = 20
    closed = []

    class Stalled(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request)
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    provider = provider_type()(
        profile=profile,
        counter=counter,
        clock=lambda: clock[0],
        key_loader=lambda: "synthetic-token",
        transport_factory=Stalled,
    )

    async def run():
        with pytest.raises(ContractError):
            await asyncio.wait_for(provider.send(provider.prepare(body)), 2)

    asyncio.run(run())
    assert len(calls) == 1 and closed


def test_cancellation_in_actual_sdk_keeps_durable_unknown_and_closes_transport(setup, tmp_path):
    _, body, profile, clock, calls, _, _, _, counter = setup
    closed = []

    async def run():
        entered = asyncio.Event()

        class Stalled(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                calls.append(request)
                entered.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.append(True)

        provider = provider_type()(
            profile=profile,
            counter=counter,
            clock=lambda: clock[0],
            key_loader=lambda: "synthetic-token",
            transport_factory=Stalled,
        )
        service = OperationService(
            store=OperationStore(tmp_path / "operations.sqlite", initialize=True),
            provider=provider,
            profile=profile,
            clock=lambda: clock[0],
            mode="enabled",
            continuous=True,
        )
        operation_id = "11111111-1111-4111-8111-111111111111"
        service.receive(operation_id, json.dumps(body).encode())
        task = asyncio.create_task(service.dispatch(operation_id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await service.dispatch(operation_id)
        operation = service.get(operation_id)
        assert (operation["status"], operation["sendClaimed"], operation["usage"]) == (
            "unknown",
            True,
            None,
        )

    asyncio.run(run())
    assert len(calls) == 1 and closed


@pytest.mark.parametrize("status", [200, 429])
def test_sdk_debug_logging_does_not_expose_ai1_body_or_private_error(setup, caplog, status):
    make, body, _, _, calls, _, _, response, _ = setup
    caplog.set_level(logging.DEBUG, logger="openai")
    if status == 429:
        response[0] = httpx.Response(
            429, json={"error": {"message": "synthetic-private-provider-error"}}
        )
    provider = make()
    prepared = provider.prepare(body)
    if status == 200:
        asyncio.run(provider.send(prepared))
    else:
        with pytest.raises(ContractError):
            asyncio.run(provider.send(prepared))
    assert len(calls) == 1
    assert "Request options" not in caplog.text
    assert "synthetic-private-provider-error" not in caplog.text
    assert "synthetic-ai1-provider-token" not in caplog.text
    assert "Provide one short plain-text explanation" not in caplog.text
    logging.getLogger("openai._base_client").debug("unrelated-sdk-request-remains-visible")
    assert "unrelated-sdk-request-remains-visible" in caplog.text


@pytest.mark.parametrize("boundary", ["connect", "tls", "headers"])
def test_actual_httpcore_checks_deadline_after_connection_and_before_body(setup, boundary):
    _, body, profile, clock, _, _, _, _, counter = setup
    writes, closed = [], []
    payload = json.dumps(completion()).encode()
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(payload)).encode()
        + b"\r\n\r\n"
        + payload
    )

    class Stream:
        async def start_tls(self, *args, **kwargs):
            if boundary == "tls":
                clock[0] = iso_ms(body["sendNotAfter"])
            return self

        async def write(self, buffer, timeout=None):
            writes.append(bytes(buffer))
            if boundary == "headers" and b"POST /chat/completions" in buffer:
                clock[0] = iso_ms(body["sendNotAfter"])

        async def read(self, max_bytes, timeout=None):
            return response

        def get_extra_info(self, info):
            return None

        async def aclose(self):
            closed.append(True)

    class Network:
        async def connect_tcp(self, *args, **kwargs):
            if boundary == "connect":
                clock[0] = iso_ms(body["sendNotAfter"])
            return Stream()

    def transport():
        result = httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
        # Real HTTPX/httpcore serialization and trace; only network I/O is synthetic.
        result._pool._network_backend = Network()
        return result

    provider = provider_type()(
        profile=profile,
        counter=counter,
        clock=lambda: clock[0],
        key_loader=lambda: "synthetic-token",
        transport_factory=transport,
    )
    with pytest.raises(ContractError):
        asyncio.run(provider.send(provider.prepare(body)))
    assert closed
    if boundary == "headers":
        assert len(writes) == 1 and b"POST /chat/completions" in writes[0]
    else:
        assert writes == []
