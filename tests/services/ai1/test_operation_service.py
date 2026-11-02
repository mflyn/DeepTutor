"""Actual SQLite and orchestration; only the model transport is a counted double."""

import asyncio
import copy
import importlib
import json
import shutil

import pytest
import rfc8785

from deeptutor.services.ai1_contract import ContractError, iso_ms, request_hash
from deeptutor.services.ai1_store import OperationStore


ID = "11111111-1111-4111-8111-111111111111"
PROFILE = {
    "model": "synthetic-model", "modelProfileVersion": "synthetic-v1",
    "pricingProfileVersion": "synthetic-price-v1", "checkedAt": "2029-12-31T00:00:00.000Z",
    "expiresAt": "2030-01-01T01:00:00.000Z", "callWindowMs": 5000, "clockMarginMs": 1000,
    "inputPriceMicrosPerMillion": 1000000, "outputPriceMicrosPerMillion": 2000000,
}


def service_type():
    assert importlib.util.find_spec("deeptutor.services.ai1_operation") is not None
    return importlib.import_module("deeptutor.services.ai1_operation").OperationService


@pytest.fixture
def setup(tmp_path):
    cls = service_type()
    body = {
        "schemaVersion": "ai1-provider-operation-v1", "modelProfileVersion": "synthetic-v1",
        "pricingProfileVersion": "synthetic-price-v1", "messages": [
            {"role": "system", "content": "Synthetic system"}, {"role": "user", "content": "206 * 30"}],
        "requestOptions": {"model": "synthetic-model", "maxInputTokens": 8192, "maxOutputTokens": 4096,
                           "thinking": "disabled", "tools": [], "attemptLimit": 1},
        "reservedCeilingMicros": 16384, "sendNotAfter": "2030-01-01T00:59:54.000Z",
    }
    body["requestHash"] = request_hash(body)
    store = OperationStore(tmp_path / "operations.sqlite", initialize=True)
    clock = [1893456000000]
    class Provider:
        def __init__(self):
            self.calls = []
            self.prepares = []
            self.result = ("6180", {"inputTokens": 100, "outputTokens": 50})
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
    provider = Provider()
    def make(**options):
        return cls(store=store, provider=provider, profile=PROFILE, clock=lambda: clock[0],
                   **{"mode": "enabled", "continuous": True, **options})
    return make, store, body, provider, clock


def put(service, body, operation_id=ID):
    return service.receive(operation_id, json.dumps(body).encode())


def test_persistent_success_replay_reads_without_model(setup):
    make, store, body, provider, clock = setup
    service = make()
    op, created = put(service, body)
    assert created and op["status"] == "accepted"
    assert provider.calls == []
    asyncio.run(service.dispatch(ID))
    terminal = service.get(ID)
    assert terminal["status"] == "succeeded" and terminal["sendClaimed"]
    assert len(provider.calls) == 1
    clock[0] = iso_ms(PROFILE["expiresAt"]) + 1
    assert put(make(), body) == (terminal, False)
    assert make().get(ID) == terminal
    asyncio.run(make().dispatch(ID))
    assert len(provider.calls) == 1


@pytest.mark.parametrize("state", ["accepted", "sending", "succeeded", "failed_not_sent", "empty"])
def test_old_backup_or_unknown_continuity_is_always_isolated(setup, state):
    make, store, body, provider, clock = setup
    if state != "empty":
        put(make(), body)
        if state == "sending":
            store.claim(ID, lambda _: None)
        elif state == "succeeded":
            asyncio.run(make().dispatch(ID))
        elif state == "failed_not_sent":
            store.claim(ID, lambda _: "PREVIEW_STALE")
    before = store.path.read_bytes()
    calls = len(provider.calls)
    for service in [make(mode="store-isolated"), make(continuous=False)]:
        for action in [lambda: service.get(ID), lambda: put(service, body), lambda: asyncio.run(service.dispatch(ID))]:
            with pytest.raises(ContractError, match="AI_STORE_ISOLATED"):
                action()
    assert store.path.read_bytes() == before
    assert len(provider.calls) == calls


def test_restore_accepted_snapshot_cannot_send_second_time(setup, tmp_path):
    make, store, body, provider, clock = setup
    service = make()
    put(service, body)
    backup = tmp_path / "accepted.snapshot"
    shutil.copyfile(store.path, backup)
    asyncio.run(service.dispatch(ID))
    assert len(provider.calls) == 1
    shutil.copyfile(backup, store.path)
    isolated = make(mode="store-isolated")
    with pytest.raises(ContractError, match="AI_STORE_ISOLATED"):
        asyncio.run(isolated.dispatch(ID))
    assert store.get(ID)["status"] == "accepted"
    assert len(provider.calls) == 1


def test_default_off_and_query_only_never_send(setup):
    make, store, body, provider, clock = setup
    service = make()
    put(service, body)
    for mode in ["off", "query-only"]:
        stopped = make(mode=mode)
        with pytest.raises(ContractError, match="AI_DISABLED"):
            put(stopped, body)
        with pytest.raises(ContractError, match="AI_DISABLED"):
            asyncio.run(stopped.dispatch(ID))
    assert make(mode="query-only").get(ID)["status"] == "accepted"
    assert provider.calls == []


def test_receive_and_claim_expiry_persist_not_sent(setup):
    make, store, body, provider, clock = setup
    service = make()
    put(service, body)
    clock[0] = iso_ms(body["sendNotAfter"])
    asyncio.run(service.dispatch(ID))
    assert service.get(ID)["status"] == "failed_not_sent"
    assert not service.get(ID)["sendClaimed"]
    second = "22222222-2222-4222-8222-222222222222"
    op, created = put(service, body, second)
    assert created and op["status"] == "failed_not_sent"
    assert op["error"] == "PREVIEW_STALE"
    assert provider.calls == []


@pytest.mark.parametrize("change", ["revoked", "increased"])
def test_accepted_profile_change_rejects_before_claim_and_replays_exactly(setup, change):
    make, store, body, provider, clock = setup
    assert put(make(), body)[0]["status"] == "accepted"
    profile = copy.deepcopy(PROFILE)
    if change == "revoked":
        profile["pricingProfileVersion"] = "revoked-price"
    else:
        profile["inputPriceMicrosPerMillion"] *= 2
    service = service_type()(store=store, provider=provider, profile=profile,
                             mode="enabled", continuous=True, clock=lambda: clock[0])
    asyncio.run(service.dispatch(ID))
    rejected = service.get(ID)
    assert rejected["status"] == "failed_not_sent" and rejected["sendClaimed"] is False
    assert rejected["usage"] is None and rejected["result"] is None
    assert rejected["error"] in {"PREVIEW_STALE", "PROFILE_UNAVAILABLE"}
    before = store.path.read_bytes()
    assert put(make(), body) == (rejected, False)
    asyncio.run(make().dispatch(ID))
    assert make().get(ID) == rejected and store.path.read_bytes() == before
    assert provider.calls == []


def test_expiry_after_claim_before_transport_is_unknown_no_call(setup):
    make, store, body, provider, clock = setup
    service = make()
    put(service, body)
    original = store.claim
    def claim_then_expire(*args):
        owner = original(*args)
        clock[0] = iso_ms(body["sendNotAfter"])
        return owner
    store.claim = claim_then_expire
    asyncio.run(service.dispatch(ID))
    op = service.get(ID)
    assert (op["status"], op["sendClaimed"], op["usage"]) == ("unknown", True, None)
    assert provider.calls == []


def test_preparation_crosses_deadline_before_claim_is_not_sent(setup):
    make, store, body, provider, clock = setup
    service = make()
    put(service, body)
    original = provider.prepare
    def prepare_then_expire(snapshot):
        prepared = original(snapshot)
        clock[0] = iso_ms(body["sendNotAfter"])
        return prepared
    provider.prepare = prepare_then_expire
    asyncio.run(service.dispatch(ID))
    operation = service.get(ID)
    assert operation["status"] == "failed_not_sent"
    assert operation["error"] == "PREVIEW_STALE"
    assert operation["sendClaimed"] is False
    assert provider.calls == []


def test_transport_error_unknown_no_retry_or_refund(setup):
    make, store, body, provider, clock = setup
    provider.error = RuntimeError("synthetic private provider error")
    service = make()
    put(service, body)
    asyncio.run(service.dispatch(ID))
    op = service.get(ID)
    assert op["status"] == "unknown" and op["error"] == "RESULT_UNKNOWN"
    assert "private" not in json.dumps(op)
    asyncio.run(make().dispatch(ID))
    assert len(provider.calls) == 1


@pytest.mark.parametrize("result", [("", None), ("\ufeff", None), ("\ufeff \t\n", None),
                                     ("6180", {"inputTokens": 8193, "outputTokens": 0}),
                                     ("x" * 12001, None), ("6180", {"inputTokens": True, "outputTokens": 0})])
def test_invalid_provider_result_is_unknown_not_false_success(setup, result):
    make, store, body, provider, clock = setup
    provider.result = result
    service = make()
    put(service, body)
    asyncio.run(service.dispatch(ID))
    assert service.get(ID)["status"] == "unknown"
    assert service.get(ID)["result"] is None
    assert service.get(ID)["usage"] is None


def test_missing_usage_is_preserved_not_zero(setup):
    make, store, body, provider, clock = setup
    provider.result = ("6180", None)
    service = make()
    put(service, body)
    asyncio.run(service.dispatch(ID))
    assert service.get(ID)["status"] == "succeeded"
    assert service.get(ID)["usage"] is None


def test_concurrent_dispatch_claims_once(setup):
    make, store, body, provider, clock = setup
    service = make()
    put(service, body)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        original = provider.send
        async def held_send(prepared):
            result = await original(prepared)
            entered.set()
            await release.wait()
            return result
        provider.send = held_send
        first = asyncio.create_task(service.dispatch(ID))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.gather(*(make().dispatch(ID) for _ in range(8)))
            assert not first.done() and len(provider.calls) == 1
            assert store.get(ID)["status"] == "sending"
        finally:
            release.set()
            await asyncio.wait_for(first, 2)
    asyncio.run(run())
    assert len(provider.calls) == 1


def test_cancellation_during_send_marks_unknown_and_propagates(setup):
    make, store, body, provider, clock = setup
    service = make()
    put(service, body)
    async def run():
        entered = asyncio.Event()
        async def cancelled_send(prepared):
            provider.calls.append(prepared)
            entered.set()
            await asyncio.Event().wait()
        provider.send = cancelled_send
        task = asyncio.create_task(service.dispatch(ID))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await make().dispatch(ID)
    asyncio.run(run())
    operation = service.get(ID)
    assert (operation["status"], operation["error"], operation["sendClaimed"], operation["usage"]) == (
        "unknown", "RESULT_UNKNOWN", True, None)
    assert len(provider.calls) == 1


def test_invalid_preparation_blocks_without_send(setup):
    make, store, body, provider, clock = setup
    provider.prepare_error = ContractError("VALIDATION_ERROR")
    service = make()
    op, _ = put(service, body)
    assert op["status"] == "failed_not_sent"
    assert not op["sendClaimed"]
    assert provider.calls == []


def test_uuid_and_unknown_mode_are_rejected(setup):
    make, store, body, provider, clock = setup
    with pytest.raises(ContractError):
        make(mode="ENABLED")
    with pytest.raises(ContractError):
        put(make(), body, "../config")
    assert provider.calls == []
