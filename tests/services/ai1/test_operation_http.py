"""Real ASGI/SQLite layer with a counted synthetic Provider, never model I/O."""

import asyncio
import copy
import importlib
import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from deeptutor.services.ai1_contract import request_hash
from deeptutor.services.ai1_operation import OperationService
from deeptutor.services.ai1_store import OperationStore


TOKEN = "synthetic-ai1-service-only-token-000000"
ID = "11111111-1111-4111-8111-111111111111"
URL = "/operations/" + ID
PROFILE = {"model": "synthetic-model", "modelProfileVersion": "synthetic-v1",
           "pricingProfileVersion": "synthetic-price-v1", "checkedAt": "2029-12-31T00:00:00.000Z",
           "expiresAt": "2030-01-01T01:00:00.000Z", "callWindowMs": 5000, "clockMarginMs": 1000,
           "inputPriceMicrosPerMillion": 1000000, "outputPriceMicrosPerMillion": 2000000}


@pytest.fixture
def fixture(tmp_path):
    assert importlib.util.find_spec("deeptutor.services.ai1_http") is not None
    factory = importlib.import_module("deeptutor.services.ai1_http").create_operation_app
    body = {"schemaVersion": "ai1-provider-operation-v1", "modelProfileVersion": "synthetic-v1",
            "pricingProfileVersion": "synthetic-price-v1", "messages": [
                {"role": "system", "content": "Synthetic system"}, {"role": "user", "content": "206 * 30"}],
            "requestOptions": {"model": "synthetic-model", "maxInputTokens": 8192, "maxOutputTokens": 4096,
                               "thinking": "disabled", "tools": [], "attemptLimit": 1},
            "reservedCeilingMicros": 16384, "sendNotAfter": "2030-01-01T00:59:54.000Z"}
    body["requestHash"] = request_hash(body)
    store = OperationStore(tmp_path / "operations.sqlite", initialize=True)
    class Provider:
        def __init__(self):
            self.calls = 0
        def prepare(self, snapshot):
            return copy.deepcopy(snapshot)
        async def send(self, prepared):
            self.calls += 1
            return "6180", {"inputTokens": 100, "outputTokens": 50}
    provider = Provider()
    service = OperationService(store=store, provider=provider, profile=PROFILE,
                               mode="enabled", continuous=True, clock=lambda: 1893456000000)
    return factory(service=service, token=TOKEN), service, store, body, provider


def run(app, scenario):
    async def test():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            return await scenario(client)
    return asyncio.run(test())


def test_auth_precedes_parsing_no_leak_no_send(fixture):
    app, service, store, body, provider = fixture
    async def scenario(client):
        for headers in [{}, {"x-ai1-service-token": "wrong"},
                        {"authorization": "Bearer " + TOKEN}]:
            response = await client.put(URL, content=b"x" * 40000, headers=headers)
            assert response.status_code == 401
            assert response.json() == {"success": False, "error": {
                "code": "UNAUTHENTICATED", "message": "UNAUTHENTICATED", "details": []}}
            assert response.headers["cache-control"] == "no-store"
    run(app, scenario)
    assert provider.calls == 0 and store.get(ID) is None


def test_put_get_replay_and_conflict(fixture):
    app, service, store, body, provider = fixture
    async def scenario(client):
        client.headers["x-ai1-service-token"] = TOKEN
        first = await client.put(URL, json=body)
        assert first.status_code == 201
        assert first.json()["data"]["operation"]["status"] == "succeeded"
        second = await client.put(URL, json=body)
        assert second.status_code == 200 and second.json() == first.json()
        read = await client.get(URL)
        assert read.json() == first.json()
        changed = copy.deepcopy(body)
        changed["messages"][1]["content"] = "changed"
        changed["requestHash"] = request_hash(changed)
        conflict = await client.put(URL, json=changed)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert provider.calls == 1
    run(app, scenario)


@pytest.mark.parametrize("content,headers,expected", [
    (b" " * 32769, {"content-type": "application/json"}, 413),
    (b'"\xff"', {"content-type": "application/json"}, 400),
    (b"{}", {"content-type": "application/json; charset=latin1"}, 400),
    (b"{}", {"content-type": "text/plain"}, 400),
    (b"{}", {"content-type": "application/json", "content-encoding": "gzip"}, 400),
])
def test_raw_body_contract_before_side_effects(fixture, content, headers, expected):
    app, service, store, body, provider = fixture
    async def scenario(client):
        response = await client.put(URL, content=content, headers={**headers, "x-ai1-service-token": TOKEN})
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
    run(app, scenario)
    assert provider.calls == 0 and store.get(ID) is None


def test_credential_has_no_admin_routes_or_redirects(fixture):
    app, service, store, body, provider = fixture
    async def scenario(client):
        client.headers["x-ai1-service-token"] = TOKEN
        for path in ["/settings", "/docs", "/openapi.json", URL + "/"]:
            response = await client.get(path)
            assert response.status_code == 404
            assert "location" not in response.headers
        response = await client.delete(URL)
        assert response.status_code == 405
    run(app, scenario)
    assert provider.calls == 0


def test_internal_storage_failure_is_sanitized(fixture):
    app, service, store, body, provider = fixture
    def fail(_):
        raise OSError("private path and credentials must not escape")
    store.get = fail
    async def scenario(client):
        response = await client.get(URL, headers={"x-ai1-service-token": TOKEN})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AI_REMOTE_UNAVAILABLE"
        assert "private" not in response.text
    run(app, scenario)


def test_duplicate_credentials_rejected(fixture):
    app, service, store, body, provider = fixture
    async def scenario(client):
        response = await client.put(URL, json=body, headers=[
            ("x-ai1-service-token", TOKEN), ("x-ai1-service-token", TOKEN)])
        assert response.status_code == 401
    run(app, scenario)
    assert provider.calls == 0


@contextmanager
def live_server(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "HTTP fixture failed to start"
            time.sleep(0.01)
        yield "http://127.0.0.1:" + str(listener.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive(), "HTTP fixture did not stop"


def test_real_http_lost_response_replays_durable_success_once(fixture):
    app, service, store, body, provider = fixture
    class LoseFirstBody:
        dropped = False
        async def __call__(self, scope, receive, send):
            drop = scope["type"] == "http" and scope["method"] == "PUT" and not self.dropped
            async def send_or_drop(message):
                if drop and message["type"] == "http.response.body":
                    self.dropped = True
                    raise ConnectionResetError("synthetic response loss after committed operation")
                await send(message)
            await app(scope, receive, send_or_drop)
    with live_server(LoseFirstBody()) as base:
        with httpx.Client(base_url=base, trust_env=False, timeout=5,
                          headers={"x-ai1-service-token": TOKEN}) as client:
            with pytest.raises(httpx.RemoteProtocolError):
                client.put(URL, json=body)
            original = store.get(ID)
            assert original["status"] == "succeeded" and provider.calls == 1
            read = client.get(URL)
            assert read.status_code == 200 and read.json()["data"]["operation"] == original
            replay = client.put(URL, json=body)
            assert replay.status_code == 200 and replay.json() == read.json()
            assert provider.calls == 1


def test_real_http_query_only_rebuild_does_not_dispatch_accepted(fixture):
    app, service, store, body, provider = fixture
    service.receive(ID, json.dumps(body).encode())
    query_only = OperationService(store=OperationStore(store.path), provider=provider, profile=None,
                                  mode="query-only", continuous=True)
    factory = importlib.import_module("deeptutor.services.ai1_http").create_operation_app
    with live_server(factory(service=query_only, token=TOKEN)) as base:
        with httpx.Client(base_url=base, trust_env=False, timeout=5,
                          headers={"x-ai1-service-token": TOKEN}) as client:
            read = client.get(URL)
            assert read.status_code == 200 and read.json()["data"]["operation"]["status"] == "accepted"
            denied = client.put(URL, json=body)
            assert denied.status_code == 503 and denied.json()["error"]["code"] == "AI_DISABLED"
    assert provider.calls == 0 and store.get(ID)["status"] == "accepted"
