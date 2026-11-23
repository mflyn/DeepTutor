"""Actual main/lifespan coverage; isolated homes, synthetic providers, no network."""

import copy
import json
from pathlib import Path

import pytest

from deeptutor.services.ai1_contract import request_hash
from tests.services.ai1 import test_operation_runtime as ai1_tests
from tests.services.ai1.test_operation_runtime import ID, PAYLOAD, TOKEN, formal_probe

AI3_TOKEN = "synthetic-ai3-separate-credential-00001"
URL = "/internal/deeptutor/ai3/operations/" + ID
PAYLOAD3 = copy.deepcopy(PAYLOAD)
PAYLOAD3.update(
    schemaVersion="ai3-provider-operation-v1",
    providerProfile="ai3-practice-v1",
    promptVersion="ai3-practice-prompt-v1",
    outputSchemaVersion="ai3-practice-content-v1",
)
PAYLOAD3["requestHash"] = request_hash(PAYLOAD3)


@pytest.fixture
def setup(tmp_path):
    return ai1_tests.setup.__wrapped__(tmp_path)


def test_formal_main_ai3_off_is_separate_and_zero_store_io(tmp_path):
    formal_probe(
        tmp_path,
        None,
        """
assert main.app.state.ai3_runtime is not main.app.state.ai1_runtime
with TestClient(main.app) as client:
    for prefix in ['/internal/deeptutor', '/internal/deeptutor/ai3']:
        r = client.get(prefix + '/operations/11111111-1111-4111-8111-111111111111')
        assert r.status_code == 503 and r.json()['error']['code'] == 'AI_DISABLED'
        assert r.headers['cache-control'] == 'no-store'
    assert main.app.state.ai3_runtime.service is None
assert not main.app.state.ai3_runtime._started
print('FORMAL_PROBE_PASS')
""",
    )


def test_formal_ai3_enabled_replay_auth_and_query_restart(setup, tmp_path):
    path, config, save, store, _ = setup
    Path(config["serviceTokenFile"]).write_text(AI3_TOKEN)
    source = """
import copy
import rfc8785
from deeptutor.services.ai3_contract import parse_request
class Provider:
    calls = 0
    def prepare(self, snapshot):
        return parse_request(rfc8785.dumps(snapshot))
    async def send(self, prepared):
        self.calls += 1
        return '{"instructions":"Synthetic only","exercises":[]}', None
runtime = main.app.state.ai3_runtime
runtime._clock = lambda: 1893456000000
runtime._provider = provider = Provider()
with TestClient(main.app) as client:
    assert client.get('/health/ready').status_code == 200
    for h in [{}, {'Authorization':'Bearer '+TOKEN3}, {'x-ai1-service-token':TOKEN1}]:
        assert client.get(URL, headers=h).status_code == 401
    headers = {'x-ai1-service-token':TOKEN3}
    assert client.put(URL, headers=headers, json=BODY1).status_code == 400
    assert runtime.service.store.get(ID_VALUE) is None
    first = client.put(URL, headers=headers, json=BODY3)
    assert first.status_code == 201, first.text
    original = first.json()
    assert original['data']['operation']['status'] == 'succeeded'
    assert client.put(URL, headers=headers, json=BODY3).json() == original
    assert client.get(URL, headers=headers).json() == original
    assert provider.calls == 1
    assert client.get('/internal/deeptutor/operations/'+ID_VALUE, headers=headers).status_code == 503
    assert client.get('/api/settings', headers=headers).status_code == 401
assert runtime.service is None and not runtime._started
print('FORMAL_PROBE_PASS')
"""
    substitutions = {
        "TOKEN3": AI3_TOKEN,
        "TOKEN1": TOKEN,
        "URL": URL,
        "BODY1": PAYLOAD,
        "BODY3": PAYLOAD3,
        "ID_VALUE": ID,
    }
    for key, value in substitutions.items():
        source = source.replace(key, repr(value))
    formal_probe(tmp_path, None, source, ai3_config_path=path)
    original = store.get(ID)
    config["mode"] = "query-only"
    config.pop("profile")
    Path(config.pop("providerKeyFile")).unlink()
    save()
    before = store.path.read_bytes()
    formal_probe(
        tmp_path,
        None,
        """
with TestClient(main.app) as client:
    headers = {'x-ai1-service-token': TOKEN_VALUE}
    response = client.get(URL_VALUE, headers=headers)
    assert response.json()['data']['operation'] == ORIGINAL_VALUE
    assert client.put(URL_VALUE, headers=headers, json=BODY_VALUE).status_code == 503
assert main.app.state.ai3_runtime.service is None
print('FORMAL_PROBE_PASS')
""".replace("TOKEN_VALUE", repr(AI3_TOKEN))
        .replace("URL_VALUE", repr(URL))
        .replace("ORIGINAL_VALUE", repr(original))
        .replace("BODY_VALUE", repr(PAYLOAD3)),
        ai3_config_path=path,
    )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("state", ["accepted", "sending", "unknown"])
def test_formal_ai3_restart_never_redispatches_old_claim_or_accepted(setup, tmp_path, state):
    path, _, _, store, _ = setup
    store.accept(ID, PAYLOAD3)
    if state != "accepted":
        owner = store.claim(ID, lambda _: None)
        if state == "unknown":
            store.finish(ID, owner, "unknown")
    before = store.path.read_bytes()
    formal_probe(
        tmp_path,
        None,
        """
from deeptutor.services.ai3_contract import parse_request
import rfc8785
class Provider:
    def prepare(self, snapshot): return parse_request(rfc8785.dumps(snapshot))
    async def send(self, prepared): raise AssertionError('No resend')
r = main.app.state.ai3_runtime
r._clock = lambda:1893456000000
r._provider = Provider()
with TestClient(main.app) as client:
    headers = {'x-ai1-service-token':TOKEN_VALUE}
    before = client.get(URL_VALUE, headers=headers).json()
    after = client.put(URL_VALUE, headers=headers, json=BODY_VALUE)
    assert after.status_code == 200 and after.json() == before
assert r.service is None
print('FORMAL_PROBE_PASS')
""".replace("TOKEN_VALUE", repr(TOKEN))
        .replace("URL_VALUE", repr(URL))
        .replace("BODY_VALUE", repr(PAYLOAD3)),
        ai3_config_path=path,
    )
    assert store.path.read_bytes() == before


def test_formal_both_profiles_same_id_cannot_read_or_overwrite(setup, tmp_path):
    path, config, save, store, _ = setup
    store.accept(ID, PAYLOAD)
    config["mode"] = "query-only"
    save()
    ai3 = path.with_name("ai3.json")
    ai3.write_text(path.read_text())
    ai3.chmod(0o600)
    before = store.path.read_bytes()
    formal_probe(
        tmp_path,
        path,
        """
with TestClient(main.app) as client:
    headers = {'x-ai1-service-token':TOKEN_VALUE}
    assert client.get('/internal/deeptutor/operations/'+ID_VALUE, headers=headers).status_code == 200
    r = client.get('/internal/deeptutor/ai3/operations/'+ID_VALUE, headers=headers)
    assert r.status_code == 409 and r.json()['error']['code'] == 'IDEMPOTENCY_KEY_REUSED'
print('FORMAL_PROBE_PASS')
""".replace("TOKEN_VALUE", repr(TOKEN)).replace("ID_VALUE", repr(ID)),
        ai3_config_path=ai3,
    )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["ai3-start", "main-start"])
def test_formal_start_failure_closes_both_runtimes(setup, tmp_path, failure):
    path, config, save, _, _ = setup
    config["mode"] = "query-only"
    save()
    formal_probe(
        tmp_path,
        path,
        """
def fail(): raise RuntimeError('synthetic startup failure')
if FAILURE == 'ai3-start': main.app.state.ai3_runtime.start = fail
else: main.validate_tool_consistency = fail
try:
    with TestClient(main.app): raise AssertionError('must fail')
except RuntimeError as error:
    assert str(error) == 'synthetic startup failure'
for r in [main.app.state.ai1_runtime, main.app.state.ai3_runtime]:
    assert not r._started and r.service is None
assert main.app.state.ready is False
print('FORMAL_PROBE_PASS')
""".replace("FAILURE", repr(failure)),
        ai3_config_path=path,
    )


@pytest.mark.parametrize("mode", ["store-isolated", "enabled"])
def test_formal_isolation_and_missing_real_counter_fail_closed(setup, tmp_path, mode):
    path, config, save, store, _ = setup
    config["mode"] = mode
    if mode == "store-isolated":
        config["storePath"] += ".missing"
        config["providerKeyFile"] += ".missing"
    save()
    before = store.path.read_bytes()
    formal_probe(
        tmp_path,
        None,
        """
if MODE == 'enabled':
    from deeptutor.services.ai1_contract import ContractError
    runtime = main.app.state.ai3_runtime
    runtime._clock = lambda:1893456000000
    original_factory = runtime._default_provider
    created = []
    def observe(profile, key):
        provider = original_factory(profile, key)
        assert provider._counter is None
        created.append(provider)
        return provider
    runtime._default_provider = observe
    try:
        with TestClient(main.app): raise AssertionError('Missing counter must block startup')
    except ContractError as error:
        assert error.code == 'AI_PROFILE_UNAVAILABLE'
    assert len(created) == 1
else:
    with TestClient(main.app) as client:
        assert client.get(URL_VALUE).status_code == 401
        for method in ['GET','PUT']:
            r = client.request(method, URL_VALUE, headers={'x-ai1-service-token':TOKEN_VALUE}, json=BODY_VALUE)
            assert r.status_code == 503 and r.json()['error']['code'] == 'AI_STORE_ISOLATED'
assert main.app.state.ai3_runtime.service is None
print('FORMAL_PROBE_PASS')
""".replace("MODE", repr(mode))
        .replace("URL_VALUE", repr(URL))
        .replace("TOKEN_VALUE", repr(TOKEN))
        .replace("BODY_VALUE", repr(PAYLOAD3)),
        ai3_config_path=path,
    )
    assert store.path.read_bytes() == before
    assert not Path(str(store.path) + ".missing").exists()


def test_formal_both_enabled_cross_profile_put_cannot_overwrite(setup, tmp_path):
    path, config, _, store, _ = setup
    token3 = path.with_name("ai3-token")
    token3.write_text(AI3_TOKEN)
    token3.chmod(0o600)
    config3 = {**config, "serviceTokenFile": str(token3)}
    ai3 = path.with_name("ai3.json")
    ai3.write_text(json.dumps(config3))
    ai3.chmod(0o600)
    formal_probe(
        tmp_path,
        path,
        """
import copy
class Provider:
    calls = 0
    def prepare(self, snapshot): return copy.deepcopy(snapshot)
    async def send(self, prepared):
        self.calls += 1
        return 'synthetic', None
p1, p3 = Provider(), Provider()
for runtime, provider in [(main.app.state.ai1_runtime,p1),(main.app.state.ai3_runtime,p3)]:
    runtime._clock = lambda:1893456000000
    runtime._provider = provider
with TestClient(main.app) as client:
    u1 = '/internal/deeptutor/operations/'+ID_VALUE
    u3 = '/internal/deeptutor/ai3/operations/'+ID_VALUE
    h1, h3 = {'x-ai1-service-token':TOKEN1}, {'x-ai1-service-token':TOKEN3}
    assert client.put(u3, headers=h1, json=BODY3).status_code == 401
    assert client.put(u1, headers=h3, json=BODY1).status_code == 401
    assert client.put(u1, headers=h1, json=BODY3).status_code == 400
    first = client.put(u1, headers=h1, json=BODY1)
    assert first.status_code == 201
    r = client.put(u3, headers=h3, json=BODY3)
    assert r.status_code == 409 and r.json()['error']['code'] == 'IDEMPOTENCY_KEY_REUSED'
    assert client.get(u3, headers=h3).status_code == 409
    assert client.get(u1, headers=h1).json() == first.json()
    assert p1.calls == 1 and p3.calls == 0
print('FORMAL_PROBE_PASS')
""".replace("ID_VALUE", repr(ID))
        .replace("TOKEN1", repr(TOKEN))
        .replace("TOKEN3", repr(AI3_TOKEN))
        .replace("BODY1", repr(PAYLOAD))
        .replace("BODY3", repr(PAYLOAD3)),
        ai3_config_path=ai3,
    )
    assert store.snapshot(ID) == PAYLOAD


def test_formal_missing_ai3_service_secret_stops_before_store_access(setup, tmp_path):
    path, config, save, store, _ = setup
    config["mode"] = "query-only"
    config["serviceTokenFile"] += ".missing"
    save()
    before = store.path.read_bytes()
    formal_probe(
        tmp_path,
        None,
        """
from deeptutor.services.ai1_contract import ContractError
from deeptutor.services import ai1_runtime
opened = []
def forbidden(*args, **kwargs):
    opened.append(True)
    raise AssertionError('No store before authentication settings')
ai1_runtime.OperationStore = forbidden
try:
    with TestClient(main.app): raise AssertionError('missing token must fail')
except ContractError as error:
    assert error.code == 'AI_PROFILE_UNAVAILABLE'
assert opened == []
assert main.app.state.ai3_runtime.service is None
print('FORMAL_PROBE_PASS')
""",
        ai3_config_path=path,
    )
    assert store.path.read_bytes() == before
