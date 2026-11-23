"""Local synthetic AI3 boundary tests; real SQLite/ASGI, no model transport."""

import asyncio
import copy
import hashlib
import importlib
import json
import sqlite3

from fastapi import FastAPI
import httpx
import pytest
import rfc8785

from deeptutor.services.ai1_contract import ContractError, iso_ms, parse_request, request_hash
from deeptutor.services.ai1_http import create_operation_app as ai1_app
from deeptutor.services.ai1_operation import OperationService as Ai1Service
from deeptutor.services.ai1_store import OperationStore

ID = "11111111-1111-4111-8111-111111111111"
TOKEN = "synthetic-ai3-service-only-token-000000"
URL = "/internal/deeptutor/ai3/operations/" + ID
AI1_URL = "/internal/deeptutor/operations/" + ID
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
BINDINGS = {
    "providerProfile": "ai3-practice-v1",
    "promptVersion": "ai3-practice-prompt-v1",
    "outputSchemaVersion": "ai3-practice-content-v1",
}
OP_KEYS = {
    "remoteOperationId",
    "requestHash",
    "modelProfileVersion",
    "pricingProfileVersion",
    "sendNotAfter",
    "status",
    "result",
    "error",
    "usage",
    "sendClaimed",
    "updatedAt",
}


def module(name):
    path = "deeptutor.services.ai3_" + name
    assert importlib.util.find_spec(path) is not None, "AI3 " + name + " is not implemented"
    return importlib.import_module(path)


def body(ai3=True):
    value = {
        "schemaVersion": "ai3-provider-operation-v1" if ai3 else "ai1-provider-operation-v1",
        "modelProfileVersion": "synthetic-v1",
        "pricingProfileVersion": "synthetic-price-v1",
        "messages": [
            {"role": "system", "content": "Synthetic practice; parent must check."},
            {"role": "user", "content": '{"exerciseCount":2,"questionText":"206 * 30"}'},
        ],
        "requestOptions": {
            "model": "synthetic-model",
            "maxInputTokens": 8192,
            "maxOutputTokens": 4096,
            "thinking": "disabled",
            "tools": [],
            "attemptLimit": 1,
        },
        "reservedCeilingMicros": 16384,
        "sendNotAfter": "2030-01-01T00:59:54.000Z",
    }
    if ai3:
        value.update(BINDINGS)
    return hashed(value)


def hashed(value):
    value["requestHash"] = request_hash(value)
    return value


def raw(value):
    return json.dumps(value).encode()


class Substitute:
    def __init__(self):
        self.calls = []
        self.prepares = []
        self.result = ('{"instructions":"Synthetic draft","exercises":[]}', None)
        self.error = None
        self.prepare_error = None

    def prepare(self, snapshot):
        self.prepares.append(copy.deepcopy(snapshot))
        if self.prepare_error:
            raise self.prepare_error
        return rfc8785.dumps(snapshot)

    async def send(self, prepared):
        self.calls.append(prepared)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def local(tmp_path):
    cls = module("operation").OperationService
    store = OperationStore(tmp_path / "operations.sqlite", initialize=True)
    provider = Substitute()
    clock = [1893456000000]

    def make(ai3=True, **options):
        return (cls if ai3 else Ai1Service)(
            **{
                "store": OperationStore(store.path),
                "provider": provider,
                "profile": PROFILE,
                "mode": "enabled",
                "continuous": True,
                "clock": lambda: clock[0],
                **options,
            }
        )

    return make, store, provider, clock


def run(app, scenario):
    async def execute():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://synthetic",
            headers={"x-ai1-service-token": TOKEN},
            trust_env=False,
        ) as client:
            return await scenario(client)

    return asyncio.run(execute())


def application(service, ai1=None):
    app = module("http").create_operation_app(service=service, token=TOKEN)
    if ai1:
        app.mount("/internal/deeptutor", ai1_app(service=ai1, token=TOKEN))
    return app


def test_closed_contract_and_profile_bound_digest():
    contract = module("contract")
    value = body()
    assert contract.parse_request(raw(value)) == value
    assert (
        value["requestHash"]
        == hashlib.sha256(
            rfc8785.dumps({k: v for k, v in value.items() if k != "requestHash"})
        ).hexdigest()
    )
    with pytest.raises(ContractError):
        parse_request(raw(value))
    with pytest.raises(ContractError):
        contract.parse_request(raw(body(False)))
    for key in BINDINGS:
        changed = {**value, key: "wrong-version"}
        with pytest.raises(ContractError):
            contract.parse_request(raw(changed))
        with pytest.raises(ContractError):
            contract.parse_request(raw(hashed(changed)))
        missing = {k: v for k, v in value.items() if k != key}
        with pytest.raises(ContractError):
            contract.parse_request(raw(hashed(missing)))
    with pytest.raises(ContractError):
        contract.parse_request(raw(hashed({**value, "familyId": "forbidden"})))


@pytest.mark.parametrize("ai3_first", [True, False])
def test_same_id_cross_profile_conflict_survives_reopen(local, ai3_first):
    make, store, provider, clock = local
    original, _ = make(ai3_first).receive(ID, raw(body(ai3_first)))
    before = store.path.read_bytes()
    other = make(not ai3_first)
    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_REUSED"):
        other.receive(ID, raw(body(not ai3_first)))
    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_REUSED"):
        other.get(ID)
    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_REUSED"):
        asyncio.run(other.dispatch(ID))
    assert store.path.read_bytes() == before
    assert make(ai3_first).get(ID) == original
    assert provider.calls == []


def test_http_closed_envelope_and_namespace(local):
    make, store, provider, clock = local
    app = application(make(), make(False))

    async def scenario(client):
        first = await client.put(URL, json=body())
        assert first.status_code == 201
        payload = first.json()
        assert set(payload) == {"success", "data"} and payload["success"] is True
        assert set(payload["data"]) == {"operation"}
        operation = payload["data"]["operation"]
        assert set(operation) == OP_KEYS | {"providerProfile"}
        assert operation["providerProfile"] == "ai3-practice-v1"
        assert operation["status"] == "succeeded"
        assert set(operation["result"]) == {"text", "contentHash"}
        assert operation["result"]["text"] == provider.result[0]
        assert len(first.content) <= 65536
        assert first.headers["cache-control"] == "no-store"
        assert (await client.get(URL)).json() == payload
        assert (await client.put(URL, json=body())).json() == payload
        for response in [
            await client.get(AI1_URL),
            await client.put(AI1_URL, json=body(False)),
        ]:
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert (await client.put(AI1_URL, json=body())).status_code == 400
        assert (await client.put(URL, json=body(False))).status_code == 400

    run(app, scenario)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("failure", ["PREVIEW_STALE", "PROFILE_UNAVAILABLE", "VALIDATION_ERROR"])
def test_permanent_not_sent_refusal_replays_after_restart(local, failure):
    make, store, provider, clock = local
    provider.prepare_error = ContractError(failure)
    op, created = make().receive(ID, raw(body()))
    assert created
    assert (op["status"], op["error"], op["sendClaimed"]) == ("failed_not_sent", failure, False)
    assert op["result"] is None and op["usage"] is None
    provider.prepare_error = None
    before = store.path.read_bytes()
    assert make().receive(ID, raw(body())) == (op, False)
    asyncio.run(make().dispatch(ID))
    assert make().get(ID) == op and store.path.read_bytes() == before
    assert provider.calls == []


@pytest.mark.parametrize("state", ["accepted", "sending", "unknown", "succeeded"])
def test_get_only_restart_recovery_never_sends(local, state):
    make, store, provider, clock = local
    make().receive(ID, raw(body()))
    if state == "sending":
        store.claim(ID, lambda _: None)
    elif state in {"unknown", "succeeded"}:
        provider.error = (
            RuntimeError("synthetic lost model response") if state == "unknown" else None
        )
        asyncio.run(make().dispatch(ID))
    original = make().get(ID)
    before = store.path.read_bytes()
    count = len(provider.calls)

    async def scenario(client):
        for _ in range(3):
            response = await client.get(URL)
            assert response.status_code == 200 and response.json()["data"]["operation"] == original
        assert (await client.put(URL, json=body())).status_code == 503

    run(application(make(mode="query-only", provider=None, profile=None)), scenario)
    assert store.path.read_bytes() == before and len(provider.calls) == count


def test_old_accepted_put_is_not_a_recovery_send(local):
    make, store, provider, clock = local
    original, _ = make().receive(ID, raw(body()))
    before = store.path.read_bytes()

    async def scenario(client):
        response = await client.put(URL, json=body())
        assert response.status_code == 200
        assert response.json()["data"]["operation"] == original

    run(application(make()), scenario)
    assert provider.calls == [] and store.path.read_bytes() == before


def test_lost_http_response_recovers_by_get_after_reopen(local):
    make, store, provider, clock = local
    app = application(make())

    async def lose_response(scope, receive, send):
        async def drop(message):
            if message["type"] == "http.response.body":
                raise ConnectionResetError("synthetic loss after durable completion")
            await send(message)

        await app(scope, receive, drop)

    async def put(client):
        with pytest.raises(ConnectionResetError):
            await client.put(URL, json=body())

    run(lose_response, put)
    original = store.get(ID)
    assert original["status"] == "succeeded"

    async def get(client):
        assert (await client.get(URL)).json()["data"]["operation"] == original

    run(application(make(mode="query-only", profile=None, provider=None)), get)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("boundary", ["receive", "prepare", "claim", "after_claim"])
def test_deadline_at_each_send_boundary(local, boundary, monkeypatch):
    make, store, provider, clock = local
    service = make()
    if boundary == "receive":
        clock[0] = iso_ms(body()["sendNotAfter"])
    service.receive(ID, raw(body()))
    if boundary == "claim":
        clock[0] = iso_ms(body()["sendNotAfter"])
    elif boundary == "prepare":
        original = provider.prepare

        def expire(snapshot):
            result = original(snapshot)
            clock[0] = iso_ms(body()["sendNotAfter"])
            return result

        monkeypatch.setattr(provider, "prepare", expire)
    elif boundary == "after_claim":
        original = service.store.claim

        def expire(*args):
            owner = original(*args)
            clock[0] = iso_ms(body()["sendNotAfter"])
            return owner

        monkeypatch.setattr(service.store, "claim", expire)
    asyncio.run(service.dispatch(ID))
    op = service.get(ID)
    assert op["status"] == ("unknown" if boundary == "after_claim" else "failed_not_sent")
    assert op["sendClaimed"] is (boundary == "after_claim")
    assert provider.calls == []


def test_lost_claim_commit_ack_never_sends_or_reclaims(local, monkeypatch):
    make, store, provider, clock = local
    service = make()
    service.receive(ID, raw(body()))
    claim = service.store.claim

    def lost(*args):
        claim(*args)
        raise OSError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(service.store, "claim", lost)
    with pytest.raises(OSError):
        asyncio.run(service.dispatch(ID))
    assert store.get(ID)["status"] == "sending"
    assert store.get(ID)["sendClaimed"] is True
    asyncio.run(make().dispatch(ID))
    assert provider.calls == []


@pytest.mark.parametrize("rehash", [True, False])
@pytest.mark.parametrize(
    "field", ["providerProfile", "promptVersion", "outputSchemaVersion", "requestHash"]
)
def test_claim_revalidates_persisted_profile_digest(local, field, rehash):
    make, store, provider, clock = local
    service = make()
    service.receive(ID, raw(body()))
    corrupted = body()
    corrupted[field] = "wrong"
    if rehash and field != "requestHash":
        hashed(corrupted)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE operations SET snapshot=?,request_hash=? WHERE id=?",
            (
                json.dumps(corrupted),
                corrupted["requestHash"] if rehash else body()["requestHash"],
                ID,
            ),
        )
    asyncio.run(service.dispatch(ID))
    op = store.get(ID)
    assert op["status"] == "failed_not_sent" and op["sendClaimed"] is False
    assert provider.calls == []


def test_claim_checks_row_digest_not_only_snapshot(local):
    make, store, provider, clock = local
    service = make()
    service.receive(ID, raw(body()))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE operations SET request_hash=? WHERE id=?", ("0" * 64, ID))
    asyncio.run(service.dispatch(ID))
    assert store.get(ID)["status"] == "failed_not_sent"
    assert store.get(ID)["sendClaimed"] is False
    assert provider.calls == []


@pytest.mark.parametrize(
    "result,success",
    [
        (("x" * 12000, None), True),
        (("\U0001f600" * 12000, None), True),
        (("x" * 12001, None), False),
        (("\x01" * 12000, None), False),
        (("", None), False),
        (("not content JSON", None), True),
        (("draft", {"inputTokens": True, "outputTokens": 1}), False),
        (("draft", {"inputTokens": 8192, "outputTokens": 4096}), True),
    ],
)
def test_raw_text_bounds_usage_and_lms_content_validation_boundary(local, result, success):
    make, store, provider, clock = local
    provider.result = result
    service = make()
    service.receive(ID, raw(body()))
    asyncio.run(service.dispatch(ID))
    op = service.get(ID)
    assert op["status"] == ("succeeded" if success else "unknown")
    if success:
        assert op["result"]["text"] == result[0]
        assert op["result"]["contentHash"] == hashlib.sha256(result[0].encode()).hexdigest()
        assert op["usage"] == result[1]
        assert (
            len(
                json.dumps(
                    {"success": True, "data": {"operation": op}}, ensure_ascii=False
                ).encode()
            )
            <= 65536
        )
    else:
        assert op["result"] is None and op["usage"] is None
    asyncio.run(make().dispatch(ID))
    assert len(provider.calls) == 1


def test_ai1_real_provider_is_never_reused_for_ai3(local):
    from deeptutor.services.ai1_provider import RestrictedCompletionProvider

    make, store, provider, clock = local

    def forbidden():
        pytest.fail("AI3 must not load an AI1 key or create an external transport")

    class Counter:
        model_profile_version = "synthetic-v1"

        def upper_bound(self, wire):
            pytest.fail("AI1 counter is not an AI3 qualification")

    real = RestrictedCompletionProvider(
        profile=PROFILE,
        key_loader=forbidden,
        counter=Counter(),
        transport_factory=forbidden,
        clock=lambda: clock[0],
    )
    service = make(provider=real)
    op, _ = service.receive(ID, raw(body()))
    assert op["status"] == "failed_not_sent" and op["sendClaimed"] is False
    asyncio.run(service.dispatch(ID))
    assert provider.calls == []


def test_no_provider_never_falls_back_to_real_dispatch(local):
    make, store, provider, clock = local
    service = make(provider=None)
    op, _ = service.receive(ID, raw(body()))
    assert (op["status"], op["error"], op["sendClaimed"]) == (
        "failed_not_sent",
        "PROFILE_UNAVAILABLE",
        False,
    )
    asyncio.run(service.dispatch(ID))
    assert provider.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-ai1-service-token": "wrong"},
        {"authorization": "Bearer " + TOKEN},
        [("x-ai1-service-token", TOKEN), ("x-ai1-service-token", TOKEN)],
    ],
)
def test_auth_precedes_body_and_storage(local, headers):
    make, store, provider, clock = local

    async def scenario(client):
        client.headers.clear()
        response = await client.put(URL, content=b"x" * 40000, headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"
        assert response.headers["cache-control"] == "no-store"

    run(application(make()), scenario)
    assert store.get(ID) is None and provider.prepares == [] and provider.calls == []


@pytest.mark.parametrize(
    "content,headers,status",
    [
        (b" " * 32769, {"content-type": "application/json"}, 413),
        (b'{"x":1,"x":2}', {"content-type": "application/json"}, 400),
        (b'{"x":NaN}', {"content-type": "application/json"}, 400),
        (b'"\xff"', {"content-type": "application/json"}, 400),
        (b"{}", {"content-type": "text/plain"}, 400),
        (b"{}", {"content-type": "application/json", "content-encoding": "gzip"}, 400),
    ],
)
def test_malformed_raw_requests_never_persist(local, content, headers, status):
    make, store, provider, clock = local

    async def scenario(client):
        response = await client.put(URL, content=content, headers=headers)
        assert response.status_code == status

    run(application(make()), scenario)
    assert store.get(ID) is None and provider.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("thinking", "enabled"),
        ("tools", ["search"]),
        ("attemptLimit", 2),
        ("attemptLimit", True),
        ("maxInputTokens", 8193),
        ("maxOutputTokens", 4097),
    ],
)
def test_forbidden_request_options_are_not_downgraded(local, field, value):
    make, store, provider, clock = local
    request = body()
    request["requestOptions"][field] = value
    with pytest.raises(ContractError):
        make().receive(ID, raw(hashed(request)))
    assert store.get(ID) is None and provider.calls == []


@pytest.mark.parametrize(
    "mode,continuous,code",
    [
        ("off", True, "AI_DISABLED"),
        ("query-only", True, "AI_DISABLED"),
        ("enabled", False, "AI_STORE_ISOLATED"),
        ("store-isolated", True, "AI_STORE_ISOLATED"),
    ],
)
def test_default_off_and_continuity_gate_deny_writes(local, mode, continuous, code):
    make, store, provider, clock = local
    service = make(mode=mode, continuous=continuous)
    with pytest.raises(ContractError, match=code):
        service.receive(ID, raw(body()))
    with pytest.raises(ContractError, match=code):
        asyncio.run(service.dispatch(ID))
    assert store.get(ID) is None and provider.calls == []


def test_concurrent_process_connections_only_one_sender(local):
    make, store, provider, clock = local
    make().receive(ID, raw(body()))

    async def race():
        entered, release = asyncio.Event(), asyncio.Event()
        send = provider.send

        async def hold(prepared):
            result = await send(prepared)
            entered.set()
            await release.wait()
            return result

        provider.send = hold
        first = asyncio.create_task(make().dispatch(ID))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.gather(*(make().dispatch(ID) for _ in range(16)))
            assert store.get(ID)["status"] == "sending"
            assert len(provider.calls) == 1
        finally:
            release.set()
            await first

    asyncio.run(race())
    assert store.get(ID)["status"] == "succeeded" and len(provider.calls) == 1


def test_cancelled_send_never_reclaims(local):
    make, store, provider, clock = local
    make().receive(ID, raw(body()))

    async def cancel():
        entered = asyncio.Event()

        async def hold(prepared):
            provider.calls.append(prepared)
            entered.set()
            await asyncio.Event().wait()

        provider.send = hold
        task = asyncio.create_task(make().dispatch(ID))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await make().dispatch(ID)

    asyncio.run(cancel())
    op = make().get(ID)
    assert op["status"] == "unknown" and op["sendClaimed"] and op["usage"] is None
    assert len(provider.calls) == 1


def test_lms_golden_hash_and_exact_profile_contract():
    from pathlib import Path

    golden = json.loads(Path(__file__).with_name("lms_wire_golden.json").read_text())
    expected = "2856039624ffc5127ce782bbcc43d2b076bb13ad4ae1fa7238667a52b81e5fb4"
    assert golden["requestHash"] == expected
    assert request_hash(golden) == expected
    assert module("contract").parse_request(raw(golden)) == golden
