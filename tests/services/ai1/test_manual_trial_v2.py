"""Synthetic-only v2 fee protocol. No native settings or network credentials."""

import asyncio
import copy
import json

import httpx
import pytest
import rfc8785

from deeptutor.services.ai1_contract import ContractError, check_policy, request_hash
from deeptutor.services.ai1_provider import RestrictedCompletionProvider
from deeptutor.services.ai1_store import OperationStore
from tests.services.ai1.test_operation_runtime import ID, PAYLOAD, PROFILE


def trial(profile=False, namespace="ai1"):
    if profile:
        return {
            **PROFILE,
            "feeMode": "estimate_only",
            "sourceReference": "https://example.test/price",
            "estimateAcknowledged": True,
        }
    value = copy.deepcopy(PAYLOAD)
    value["schemaVersion"] = f"{namespace}-provider-operation-v2"
    value["estimatedReservationMicros"] = value.pop("reservedCeilingMicros")
    value.update(
        feeMode="estimate_only",
        estimateMethodVersion="reference-8192-4096-v1",
        referenceInputTokens=8192,
        referenceOutputTokens=4096,
        feeNoticeVersion="estimate-not-cap-v1",
    )
    value["requestOptions"].pop("maxInputTokens")
    value["requestOptions"]["maxRequestBytes"] = 32768
    if namespace == "ai3":
        value.update(
            providerProfile="ai3-practice-v1",
            promptVersion="ai3-practice-prompt-v1",
            outputSchemaVersion="ai3-practice-content-v1",
        )
    value["requestHash"] = request_hash(value)
    return value


@pytest.mark.parametrize("namespace", ["ai1", "ai3"])
def test_v2_explicit_contract_policy_and_counter_free_prepare(namespace):
    from importlib import import_module

    parser = import_module(f"deeptutor.services.{namespace}_contract").parse_request
    provider_type = import_module(
        f"deeptutor.services.{namespace}_provider"
    ).RestrictedCompletionProvider
    value = trial(namespace=namespace)
    assert parser(rfc8785.dumps(value)) == value
    assert check_policy(value, trial(True), 1893456000000) is None
    provider = provider_type(
        profile=trial(True),
        key_loader=lambda: pytest.fail("no key in prepare"),
        clock=lambda: 1893456000000,
    )
    prepared = provider.prepare(value)
    assert len(prepared.wire) <= 32768
    assert json.loads(prepared.wire)["max_tokens"] == 4096
    assert check_policy(value, PROFILE, 1893456000000) == "PROFILE_UNAVAILABLE"
    assert check_policy(PAYLOAD, trial(True), 1893456000000) == "PROFILE_UNAVAILABLE"


@pytest.mark.parametrize(
    "field,value",
    [
        ("feeMode", "strict"),
        ("feeNoticeVersion", "other"),
        ("referenceInputTokens", 8193),
        ("estimatedReservationMicros", 0),
    ],
)
def test_v2_refuses_changed_fee_confirmation(field, value):
    from deeptutor.services.ai1_contract import parse_request

    body = trial()
    body[field] = value
    body["requestHash"] = request_hash(body)
    if field == "estimatedReservationMicros":
        assert check_policy(body, trial(True), 1893456000000) == "PROFILE_UNAVAILABLE"
    else:
        with pytest.raises(ContractError):
            parse_request(rfc8785.dumps(body))


@pytest.mark.parametrize("namespace", ["ai1", "ai3"])
def test_v2_single_transport_full_usage_and_terminal_replay(tmp_path, namespace):
    from importlib import import_module

    service_type = import_module(f"deeptutor.services.{namespace}_operation").OperationService
    provider_type = import_module(
        f"deeptutor.services.{namespace}_provider"
    ).RestrictedCompletionProvider
    calls = []

    async def reply(request):
        calls.append(request.content)
        return httpx.Response(
            200,
            json={
                "model": PROFILE["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Synthetic draft"},
                    }
                ],
                "usage": {"prompt_tokens": 30000, "completion_tokens": 1, "total_tokens": 30001},
            },
        )

    provider = provider_type(
        profile=trial(True),
        key_loader=lambda: "synthetic-not-real",
        clock=lambda: 1893456000000,
        transport_factory=lambda: httpx.MockTransport(reply),
    )
    tmp_path.chmod(0o700)
    store = OperationStore(tmp_path / "operations.sqlite", initialize=True)
    service = service_type(
        store=store,
        provider=provider,
        profile=trial(True),
        mode="enabled",
        continuous=True,
        clock=lambda: 1893456000000,
    )
    body = trial(namespace=namespace)
    original, created = service.receive(ID, rfc8785.dumps(body))
    assert created and original["schemaVersion"] == body["schemaVersion"]
    asyncio.run(service.dispatch(ID))
    result = service.get(ID)
    assert result["status"] == "succeeded" and result["usage"]["inputTokens"] == 30000
    assert result["feeMode"] == "estimate_only" and len(calls) == 1
    query = service_type(
        store=store, provider=None, profile=None, mode="query-only", continuous=True
    )
    assert query.get(ID) == result
    assert service.receive(ID, rfc8785.dumps(body)) == (result, False)
    asyncio.run(service.dispatch(ID))
    assert len(calls) == 1


def test_v1_without_counter_still_refused():
    provider = RestrictedCompletionProvider(
        profile=PROFILE, key_loader=lambda: pytest.fail("no key"), clock=lambda: 1893456000000
    )
    with pytest.raises(ContractError, match="PROFILE_UNAVAILABLE"):
        provider.prepare(PAYLOAD)


def test_formal_trial_profile_old_v1_accepted_is_read_only(tmp_path):
    from tests.services.ai1.test_operation_runtime import TOKEN, formal_probe, setup

    path, config, save, store, _ = setup.__wrapped__(tmp_path)
    config["profile"] = trial(True)
    save()
    store.accept(ID, PAYLOAD)
    original = store.get(ID)
    before = store.path.read_bytes()
    formal_probe(
        tmp_path,
        path,
        f"""
runtime = main.app.state.ai1_runtime
runtime._clock = lambda: 1893456000000
with TestClient(main.app) as client:
    headers = {{'x-ai1-service-token':{TOKEN!r}}}
    url = '/internal/deeptutor/operations/{ID}'
    def forbidden(_): raise AssertionError('historical replay must not dispatch')
    runtime.service.dispatch = forbidden
    result = client.put(url, headers=headers, json={PAYLOAD!r})
    assert result.status_code == 200, result.text
    assert result.json()['data']['operation'] == {original!r}
    assert client.get(url, headers=headers).json() == result.json()
print('FORMAL_PROBE_PASS')
""",
    )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("namespace", ["ai1", "ai3"])
@pytest.mark.parametrize("state", ["accepted", "sending", "unknown"])
def test_formal_v2_restart_replay_never_sends(tmp_path, namespace, state):
    from tests.services.ai1.test_operation_runtime import TOKEN, formal_probe, setup

    path, config, save, store, _ = setup.__wrapped__(tmp_path)
    config["profile"] = trial(True)
    save()
    body = trial(namespace=namespace)
    store.accept(ID, body)
    if state != "accepted":
        owner = store.claim(ID, lambda _: None)
        if state == "unknown":
            store.finish(ID, owner, "unknown")
    before = store.path.read_bytes()
    url = f"/internal/deeptutor{'/ai3' if namespace == 'ai3' else ''}/operations/{ID}"
    source = f"""
runtime = main.app.state.{namespace}_runtime
runtime._clock = lambda: 1893456000000
with TestClient(main.app) as client:
    assert runtime._provider is None
    assert runtime.service.provider._counter is None
    headers = {{'x-ai1-service-token': {TOKEN!r}}}
    before = client.get({url!r}, headers=headers)
    assert before.status_code == 200 and before.json()['data']['operation']['feeMode'] == 'estimate_only'
    after = client.put({url!r}, headers=headers, json={body!r})
    assert after.status_code == 200 and after.json() == before.json()
    assert client.get({url!r}).status_code == 401
assert runtime.service is None
print('FORMAL_PROBE_PASS')
"""
    formal_probe(
        tmp_path,
        path if namespace == "ai1" else None,
        source,
        ai3_config_path=path if namespace == "ai3" else None,
    )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("value", [False, None, "true"])
def test_trial_requires_server_estimate_acknowledgement(value):
    profile = trial(True)
    profile["estimateAcknowledged"] = value
    provider = RestrictedCompletionProvider(
        profile=profile, key_loader=lambda: pytest.fail("no key"), clock=lambda: 1893456000000
    )
    with pytest.raises(ContractError, match="PROFILE_UNAVAILABLE"):
        provider.prepare(trial())


def test_trial_prepared_mutation_and_expiry_refuse_before_key_or_transport():
    from dataclasses import replace

    clock = [1893456000000]
    provider = RestrictedCompletionProvider(
        profile=trial(True),
        key_loader=lambda: pytest.fail("no key"),
        clock=lambda: clock[0],
        transport_factory=lambda: pytest.fail("no transport"),
    )
    prepared = provider.prepare(trial())
    with pytest.raises(ContractError):
        asyncio.run(provider.send(replace(prepared, wire=prepared.wire.replace(b"4096", b"4095"))))
    clock[0] = 1893459600000
    with pytest.raises(ContractError):
        asyncio.run(provider.send(prepared))


@pytest.mark.parametrize("boundary", ["connect", "tls", "headers"])
def test_trial_actual_httpcore_final_write_deadline(tmp_path, boundary):
    from tests.services.ai1 import test_operation_provider as provider_cases

    state = list(provider_cases.setup.__wrapped__())
    state[1] = trial()
    state[2] = trial(True)
    state[8] = None
    provider_cases.test_actual_httpcore_checks_deadline_after_connection_and_before_body(
        tuple(state), boundary
    )


@pytest.mark.parametrize("extra", [0, 1])
def test_trial_raw_json_exact_size_boundary(extra):
    from deeptutor.services.ai1_contract import parse_request

    body = trial()
    raw = rfc8785.dumps(body)
    raw += b" " * (32768 + extra - len(raw))
    if extra:
        with pytest.raises(ContractError, match="AI_INPUT_TOO_LARGE"):
            parse_request(raw)
    else:
        assert parse_request(raw) == body


@pytest.mark.parametrize("namespace", ["ai1", "ai3"])
@pytest.mark.parametrize("reported_usage", [None, 9000])
def test_formal_default_trial_provider_send_auth_and_query_only(
    tmp_path, namespace, reported_usage
):
    from tests.services.ai1.test_operation_runtime import TOKEN, formal_probe, setup

    path, config, save, store, _ = setup.__wrapped__(tmp_path)
    config["profile"] = trial(True)
    save()
    body = trial(namespace=namespace)
    body["messages"][1]["content"] = (
        'Synthetic \u6570\u5b66 "quote" \\ slash \n newline' + "\u6570" * 10000
    )
    body["requestHash"] = request_hash(body)
    url = f"/internal/deeptutor{'/ai3' if namespace == 'ai3' else ''}/operations/{ID}"
    source = f"""
runtime = main.app.state.{namespace}_runtime
runtime._clock = lambda: 1893456000000
factory = runtime._default_provider
calls = []
async def reply(request):
    import json
    calls.append(request.content)
    assert request.content == prepared[0].wire
    assert 30000 <= len(request.content) <= 32768
    assert int(request.headers['content-length']) == len(request.content)
    assert json.loads(request.content)['max_tokens'] == 4096
    assert json.loads(request.content)['messages'] == {body["messages"]!r}
    return httpx.Response(200, json={{'model':'synthetic-model', 'choices':[{{'index':0,
      'finish_reason':'stop','message':{{'role':'assistant','content':'Synthetic draft'}}}}],
      'usage':{None if reported_usage is None else {"prompt_tokens": 9000, "completion_tokens": 1, "total_tokens": 9001}!r}}})
prepared = []
def make(profile, key):
    provider = factory(profile, key)
    assert provider._counter is None
    provider._transport_factory = lambda: httpx.MockTransport(reply)
    prepare = provider.prepare
    def capture(snapshot):
        value = prepare(snapshot)
        prepared[:] = [value]
        return value
    provider.prepare = capture
    return provider
runtime._default_provider = make
with TestClient(main.app) as client:
    headers = {{'x-ai1-service-token':{TOKEN!r}}}
    for h in [{{}}, {{'x-ai1-service-token':'wrong'}}, {{'Authorization':'Bearer '+{TOKEN!r}}}]:
        assert client.put({url!r}, headers=h, json={body!r}).status_code == 401
    first = client.put({url!r}, headers=headers, json={body!r})
    assert first.status_code == 201, first.text
    operation = first.json()['data']['operation']
    assert operation['status'] == 'succeeded'
    assert operation['usage'] == {None if reported_usage is None else {"inputTokens": 9000, "outputTokens": 1}!r}
    assert operation['schemaVersion'] == {body["schemaVersion"]!r}
    assert first.headers['cache-control'] == 'no-store'
    assert client.put({url!r}, headers=headers, json={body!r}).json() == first.json()
    assert len(calls) == 1
assert runtime.service is None
print('FORMAL_PROBE_PASS')
"""
    formal_probe(
        tmp_path,
        path if namespace == "ai1" else None,
        source,
        ai3_config_path=path if namespace == "ai3" else None,
    )
    original = store.get(ID)
    before = store.path.read_bytes()
    config["mode"] = "query-only"
    config.pop("profile")
    config.pop("providerKeyFile")
    save()
    formal_probe(
        tmp_path,
        path if namespace == "ai1" else None,
        f"""
with TestClient(main.app) as client:
    headers = {{'x-ai1-service-token':{TOKEN!r}}}
    assert client.get({url!r}, headers=headers).json()['data']['operation'] == {original!r}
    assert client.put({url!r}, headers=headers, json={body!r}).status_code == 503
print('FORMAL_PROBE_PASS')
""",
        ai3_config_path=path if namespace == "ai3" else None,
    )
    assert store.path.read_bytes() == before


def test_formal_v2_cross_namespace_same_id_isolation(tmp_path, monkeypatch):
    from tests.services.ai1 import test_ai3_formal_runtime as formal_cases
    from tests.services.ai1.test_operation_runtime import setup

    state = setup.__wrapped__(tmp_path)
    _, config, save, _, _ = state
    config["profile"] = trial(True)
    save()
    monkeypatch.setattr(formal_cases, "PAYLOAD", trial())
    monkeypatch.setattr(formal_cases, "PAYLOAD3", trial(namespace="ai3"))
    formal_cases.test_formal_both_enabled_cross_profile_put_cannot_overwrite(state, tmp_path)


@pytest.mark.parametrize("namespace", ["ai1", "ai3"])
def test_trial_429_is_one_attempt_durable_unknown_no_resend(tmp_path, namespace):
    from importlib import import_module

    service_type = import_module(f"deeptutor.services.{namespace}_operation").OperationService
    provider_type = import_module(
        f"deeptutor.services.{namespace}_provider"
    ).RestrictedCompletionProvider
    calls = []

    async def reply(request):
        calls.append(request.content)
        return httpx.Response(429, json={"error": {"message": "synthetic rate limit"}})

    provider = provider_type(
        profile=trial(True),
        key_loader=lambda: "synthetic-not-real",
        clock=lambda: 1893456000000,
        transport_factory=lambda: httpx.MockTransport(reply),
    )
    tmp_path.chmod(0o700)
    store = OperationStore(tmp_path / "operations.sqlite", initialize=True)
    service = service_type(
        store=store,
        provider=provider,
        profile=trial(True),
        mode="enabled",
        continuous=True,
        clock=lambda: 1893456000000,
    )
    body = trial(namespace=namespace)
    service.receive(ID, rfc8785.dumps(body))
    asyncio.run(service.dispatch(ID))
    original = service.get(ID)
    assert original["status"] == "unknown" and original["usage"] is None
    assert len(calls) == 1
    restarted = service_type(
        store=OperationStore(store.path),
        provider=provider,
        profile=trial(True),
        mode="enabled",
        continuous=True,
        clock=lambda: 1893456000000,
    )
    assert restarted.receive(ID, rfc8785.dumps(body)) == (original, False)
    asyncio.run(restarted.dispatch(ID))
    assert restarted.get(ID) == original and len(calls) == 1


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": 8193, "completion_tokens": 4096, "total_tokens": 12289},
        {"prompt_tokens": 8193, "completion_tokens": 4097},
        {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3},
    ],
)
def test_trial_usage_unknown_full_or_invalid_not_zero(usage):
    raw = json.dumps(
        {
            "model": "synthetic-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Synthetic"},
                }
            ],
            "usage": usage,
        }
    ).encode()
    if usage is not None and (usage["completion_tokens"] > 4096 or usage.get("total_tokens") == 3):
        with pytest.raises(ContractError):
            RestrictedCompletionProvider._result(raw, "synthetic-model", trial=True)
    else:
        _, actual = RestrictedCompletionProvider._result(raw, "synthetic-model", trial=True)
        assert actual == (None if usage is None else {"inputTokens": 8193, "outputTokens": 4096})
