"""Formal mounting helper and private startup settings; no real model."""

import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI

from deeptutor.services.ai1_contract import ContractError
from deeptutor.services.ai1_store import OperationStore


PROFILE = {
    "model": "synthetic-model", "modelProfileVersion": "synthetic-v1",
    "pricingProfileVersion": "synthetic-price-v1", "checkedAt": "2029-12-31T00:00:00.000Z",
    "expiresAt": "2030-01-01T01:00:00.000Z", "callWindowMs": 5000, "clockMarginMs": 1000,
    "inputPriceMicrosPerMillion": 1000000, "outputPriceMicrosPerMillion": 2000000,
}
ID = "11111111-1111-4111-8111-111111111111"
TOKEN = "synthetic-service-credential-00000001"
PAYLOAD = json.loads(Path(__file__).with_name("lms_wire_golden.json").read_text())


def runtime_module():
    assert importlib.util.find_spec("deeptutor.services.ai1_runtime") is not None
    return importlib.import_module("deeptutor.services.ai1_runtime")


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    store = OperationStore(tmp_path / "operations.sqlite", initialize=True)
    for name, value in [("service-token", TOKEN), ("provider-token", "synthetic-provider-credential-00001")]:
        (tmp_path / name).write_text(value)
        (tmp_path / name).chmod(0o600)
    config = {"mode": "enabled", "storePath": str(store.path), "storeContinuous": True,
              "serviceTokenFile": str(tmp_path / "service-token"), "providerKeyFile": str(tmp_path / "provider-token"),
              "profile": copy.deepcopy(PROFILE)}
    config_path = tmp_path / "ai1.json"
    def save():
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
    save()
    class Provider:
        calls = 0
        def prepare(self, snapshot):
            return copy.deepcopy(snapshot)
        async def send(self, snapshot):
            self.calls += 1
            return "6180", {"inputTokens": 100, "outputTokens": 50}
    provider = Provider()
    return config_path, config, save, store, provider


def mount(path=None, provider=None):
    app = FastAPI()
    runtime = runtime_module().mount_operation_runtime(app, config_path=path, provider=provider,
                                                      clock=lambda: 1893456000000)
    return app, runtime


def call(app, method="GET", suffix="operations/" + ID, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            return await client.request(method, "/internal/deeptutor/" + suffix, **kwargs)
    return asyncio.run(run())


def test_default_off_is_zero_io_and_rejects_before_start_and_after_stop(monkeypatch):
    monkeypatch.delenv("DEEPTUTOR_AI1_CONFIG_FILE", raising=False)
    app, runtime = mount()
    assert call(app).status_code == 503
    runtime.start()
    assert call(app).json()["error"]["code"] == "AI_DISABLED"
    assert runtime.service is None
    runtime.stop()
    assert call(app).status_code == 503


def test_mounted_enabled_path_auth_replay_and_narrow_credential(setup):
    path, _, _, store, provider = setup
    app, runtime = mount(path, provider)
    assert call(app).status_code == 503
    runtime.start()
    assert call(app).status_code == 401
    assert call(app, headers={"authorization": "Bearer " + TOKEN}).status_code == 401
    headers = {"x-ai1-service-token": TOKEN}
    response = call(app, "PUT", headers=headers, json=PAYLOAD)
    assert response.status_code == 201 and response.json()["data"]["operation"]["status"] == "succeeded"
    assert call(app, "PUT", headers=headers, json=PAYLOAD).status_code == 200
    assert provider.calls == 1
    for suffix in ["docs", "openapi.json", "settings", "operations/" + ID + "/"]:
        assert call(app, suffix=suffix, headers=headers).status_code == 404
    assert store.get(ID)["sendClaimed"] is True
    runtime.stop()
    assert call(app, headers=headers).status_code == 503


def test_query_only_reads_without_quote_or_provider_credential(setup):
    path, config, save, store, provider = setup
    store.accept(ID, PAYLOAD)
    before = store.path.read_bytes()
    config["mode"] = "query-only"
    config.pop("profile")
    os.unlink(config.pop("providerKeyFile"))
    save()
    app, runtime = mount(path)
    runtime.start()
    headers = {"x-ai1-service-token": TOKEN}
    assert call(app, headers=headers).json()["data"]["operation"]["status"] == "accepted"
    assert call(app, "PUT", headers=headers, json=PAYLOAD).json()["error"]["code"] == "AI_DISABLED"
    assert store.path.read_bytes() == before and provider.calls == 0


def test_isolated_does_not_open_missing_store_or_load_provider(setup):
    path, config, save, store, _ = setup
    config["mode"] = "store-isolated"
    config["storeContinuous"] = False
    config["storePath"] = str(store.path.parent / "missing.sqlite")
    config["providerKeyFile"] = str(store.path.parent / "missing-key")
    save()
    app, runtime = mount(path)
    runtime.start()
    headers = {"x-ai1-service-token": TOKEN}
    for method in ["GET", "PUT"]:
        response = call(app, method, headers=headers, json=PAYLOAD if method == "PUT" else None)
        assert response.json()["error"]["code"] == "AI_STORE_ISOLATED"
    assert not Path(config["storePath"]).exists()


@pytest.mark.parametrize("change", ["mode", "continuity", "missing-store", "missing-key", "same-key", "profile", "extra"])
def test_bad_enabled_configuration_stops_without_write_or_send(setup, change):
    path, config, save, store, provider = setup
    if change == "mode":
        config["mode"] = "ENABLE"
    elif change == "continuity":
        config["storeContinuous"] = "true"
    elif change == "missing-store":
        config["storePath"] += ".missing"
    elif change == "missing-key":
        config["providerKeyFile"] += ".missing"
    elif change == "same-key":
        config["providerKeyFile"] = config["serviceTokenFile"]
    elif change == "profile":
        config["profile"]["expiresAt"] = "2020-01-01T00:00:00.000Z"
    else:
        config["allowUnknown"] = True
    save()
    before = store.path.read_bytes()
    app, runtime = mount(path, provider)
    with pytest.raises(ContractError, match="AI_PROFILE_UNAVAILABLE"):
        runtime.start()
    assert call(app).status_code == 503
    assert store.path.read_bytes() == before and provider.calls == 0


@pytest.mark.parametrize("unsafe", ["config-mode", "token-mode", "parent-mode", "symlink", "hardlink", "duplicate-json", "oversize"])
def test_private_configuration_and_secret_files_reject_unsafe_inputs(setup, unsafe):
    path, config, _, store, provider = setup
    if unsafe == "config-mode":
        path.chmod(0o644)
    elif unsafe == "token-mode":
        Path(config["serviceTokenFile"]).chmod(0o644)
    elif unsafe == "parent-mode":
        path.parent.chmod(0o755)
    elif unsafe == "symlink":
        target = path.with_suffix(".real")
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(path, path.with_suffix(".link"))
    elif unsafe == "duplicate-json":
        path.write_text('{"mode":"off","mode":"enabled"}')
    else:
        path.write_bytes(b" " * 8193)
    app, runtime = mount(path, provider)
    with pytest.raises(ContractError, match="AI_PROFILE_UNAVAILABLE") as error:
        runtime.start()
    assert str(path) not in str(error.value) and TOKEN not in str(error.value)
    assert call(app).status_code == 503 and provider.calls == 0


def test_real_provider_missing_counter_rejects_enabled_start(setup):
    path, _, _, store, _ = setup
    before = store.path.read_bytes()
    app, runtime = mount(path)
    with pytest.raises(ContractError, match="AI_PROFILE_UNAVAILABLE"):
        runtime.start()
    assert call(app).status_code == 503 and store.path.read_bytes() == before


def test_startup_rejects_store_without_unique_id_index(setup):
    path, _, _, store, provider = setup
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE operations RENAME TO old_operations")
        db.execute("CREATE TABLE operations AS SELECT * FROM old_operations")
        db.execute("DROP TABLE old_operations")
    before = store.path.read_bytes()
    app, runtime = mount(path, provider)
    with pytest.raises(ContractError, match="AI_PROFILE_UNAVAILABLE"):
        runtime.start()
    assert call(app).status_code == 503 and store.path.read_bytes() == before and provider.calls == 0


@pytest.mark.parametrize("target", ["config", "serviceTokenFile", "providerKeyFile"])
def test_fifo_configuration_or_secret_is_rejected_without_blocking(setup, target):
    path, config, _, _, _ = setup
    fifo = path if target == "config" else Path(config[target])
    fifo.unlink()
    os.mkfifo(fifo, 0o600)
    script = """
from deeptutor.services.ai1_runtime import OperationRuntime
from deeptutor.services.ai1_contract import ContractError
try:
    OperationRuntime(config_path=CONFIG_PATH, clock=lambda:1893456000000).start()
except ContractError as error:
    assert error.code == 'AI_PROFILE_UNAVAILABLE'
else:
    raise AssertionError('FIFO must be rejected')
""".replace("CONFIG_PATH", repr(str(path)))
    result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=2)
    assert result.returncode == 0, result.stdout + result.stderr


def formal_probe(tmp_path, config_path, source):
    home = tmp_path / "formal-home"
    home.mkdir(exist_ok=True)
    modules = home / "data" / "system"
    modules.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[3]
    selected = {json.loads(p.read_text())["id"]: False for p in (repo / "deeptutor/modules/manifests").glob("*.json")}
    selected.update({"core": True, "server": True})
    (modules / "modules.json").write_text(json.dumps({"schema_version": 1, "enabled": selected}))
    settings = home / "data" / "user" / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    auth = settings / "auth.json"
    auth.write_text(json.dumps({"version": 1, "enabled": True}))
    auth.chmod(0o600)
    env = {"PATH": os.defpath, "HOME": str(home), "DEEPTUTOR_HOME": str(home),
           "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    if config_path is not None:
        env["DEEPTUTOR_AI1_CONFIG_FILE"] = str(config_path)
    # No credentials/settings inherit from the developer's process or real home.
    result = subprocess.run([sys.executable, "-B", "-c", """
import socket
original_connect = socket.socket.connect
def deny_external(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError('Network forbidden in formal ASGI probe')
    return original_connect(self, address)
socket.socket.connect = deny_external
import asyncio
import httpx
from deeptutor.api import main
from fastapi.testclient import TestClient
""" + source], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FORMAL_PROBE_PASS" in result.stdout
    return result.stdout


def test_actual_formal_main_default_off_lifecycle(tmp_path):
    formal_probe(tmp_path, None, """
with TestClient(main.app) as client:
    assert client.get('/health/live').status_code == 200
    assert client.get('/internal/deeptutor/operations/11111111-1111-4111-8111-111111111111').status_code == 503
    assert main.app.state.ai1_runtime.service is None
assert not main.app.state.ai1_runtime._started
print('FORMAL_PROBE_PASS')
""")


def test_actual_main_startup_failure_closes_prepared_runtime(setup, tmp_path):
    path, config, save, _, _ = setup
    config["mode"] = "query-only"
    save()
    formal_probe(tmp_path, path, """
def fail_start():
    raise RuntimeError('synthetic unrelated startup failure')
main.validate_tool_consistency = fail_start
try:
    with TestClient(main.app):
        raise AssertionError('startup should have failed')
except RuntimeError as error:
    assert str(error) == 'synthetic unrelated startup failure'
assert main.app.state.ai1_runtime.service is None
assert not main.app.state.ai1_runtime._started
assert main.app.state.ready is False
print('FORMAL_PROBE_PASS')
""")


def test_actual_main_enabled_sdk_narrow_auth_and_query_only_restart(setup, tmp_path):
    path, _, _, store, _ = setup
    source = """
import json
from pathlib import Path
from deeptutor.services.ai1_provider import RestrictedCompletionProvider
from deeptutor.services.ai1_runtime import OperationRuntime
profile = PROFILE_VALUE
payload = PAYLOAD_VALUE
requests = []
class Counter:
    model_profile_version = 'synthetic-v1'
    def upper_bound(self, wire):
        return 8192
def response(request):
    requests.append(request)
    return httpx.Response(200, json={'id':'synthetic','object':'chat.completion','created':1,
        'model':'synthetic-model','choices':[{'index':0,'finish_reason':'stop',
        'message':{'role':'assistant','content':'6180'}}],
        'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}})
runtime = main.app.state.ai1_runtime
runtime._clock = lambda: 1893456000000
runtime._provider = RestrictedCompletionProvider(profile=profile, counter=Counter(),
    clock=runtime._clock, key_loader=lambda:'synthetic-provider-credential-00001',
    transport_factory=lambda:httpx.MockTransport(response))
headers = {'x-ai1-service-token': TOKEN_VALUE}
url = '/internal/deeptutor/operations/11111111-1111-4111-8111-111111111111'
with TestClient(main.app) as client:
    assert client.get('/health/ready').status_code == 200
    assert client.get(url).status_code == 401
    assert client.get(url, headers={'Authorization':'Bearer '+TOKEN_VALUE}).status_code == 401
    assert client.get('/api/settings', headers=headers).status_code == 401
    first = client.put(url, headers=headers, json=payload)
    assert first.status_code == 201, first.text
    original = first.json()
    assert original['data']['operation']['status'] == 'succeeded'
    assert client.put(url, headers=headers, json=payload).json() == original
    assert client.get(url, headers=headers).json() == original
    assert len(requests) == 1
    assert first.headers['cache-control'] == 'no-store'
    print('FORMAL_FIRST_OPERATION=' + json.dumps(original['data']['operation']))
assert runtime.service is None
config_path = Path(CONFIG_PATH_VALUE)
config = json.loads(config_path.read_text())
config['mode'] = 'query-only'
config.pop('profile')
config.pop('providerKeyFile')
config_path.write_text(json.dumps(config))
print('FORMAL_PROBE_PASS')
""".replace("PROFILE_VALUE", repr(PROFILE)).replace("PAYLOAD_VALUE", repr(PAYLOAD)).replace(
        "TOKEN_VALUE", repr(TOKEN)).replace("CONFIG_PATH_VALUE", repr(str(path)))
    output = formal_probe(tmp_path, path, source)
    original = json.loads(next(line.removeprefix("FORMAL_FIRST_OPERATION=") for line in output.splitlines()
                               if line.startswith("FORMAL_FIRST_OPERATION=")))
    assert store.get(ID) == original
    assert original["status"] == "succeeded"
    before = store.path.read_bytes()
    formal_probe(tmp_path, path, """
headers = {'x-ai1-service-token': TOKEN_VALUE}
url = '/internal/deeptutor/operations/11111111-1111-4111-8111-111111111111'
with TestClient(main.app) as client:
    assert client.get(url, headers=headers).json() == {'success': True, 'data': {'operation': ORIGINAL_VALUE}}
    assert client.put(url, headers=headers, json=PAYLOAD_VALUE).status_code == 503
assert main.app.state.ai1_runtime.service is None
print('FORMAL_PROBE_PASS')
""".replace("TOKEN_VALUE", repr(TOKEN)).replace("ORIGINAL_VALUE", repr(original)).replace("PAYLOAD_VALUE", repr(PAYLOAD)))
    assert store.path.read_bytes() == before
