"""Explicit cross-checkout acceptance: real LMS client, main and TCP; no model.

Run explicitly with pytest and AI1_LMS_ROOT, AI1_NODE, AI1_NODE_PATH set.
Missing checkouts/dependencies fail rather than skip. Not part of standalone
DeepTutor discovery because that checkout does not ship LMS or Node modules.
"""

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest

from deeptutor.services.ai1_contract import request_hash
from deeptutor.services.ai1_store import OperationStore

REPO = Path(__file__).resolve().parents[3]
TOKEN = "synthetic-service-credential-00000001"
ID = "11111111-1111-4111-8111-111111111111"
NOW = 1893456000000
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
PAYLOAD = json.loads(Path(__file__).with_name("lms_wire_golden.json").read_text())
NODE_SOURCE = r"""
const fs = require('fs');
const {createOperationClient,buildRequest} = require(process.env.AI1_LMS_ROOT +
  '/backend/services/homework-service/services/ai1Operation');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const client=createOperationClient({baseUrl:input.origin,token:input.token,allowLoopback:true,timeout:3000});
const job=input.job;
job.remoteRequestHash=buildRequest(job).requestHash;
(async()=>{try{console.log(JSON.stringify({operation:await client[input.method](job)}));}
catch(e){console.log(JSON.stringify({error:e.code,status:e.statusCode}));}})().catch(()=>process.exit(2));
"""


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def local(tmp_path):
    for name in ("AI1_LMS_ROOT", "AI1_NODE", "AI1_NODE_PATH"):
        assert os.environ.get(name), "Explicit cross-checkout prerequisite missing: " + name
    assert (
        Path(os.environ["AI1_LMS_ROOT"])
        / "backend/services/homework-service/services/ai1Operation.js"
    ).is_file()
    tmp_path.chmod(0o700)
    home = tmp_path / "home"
    system = home / "data/system"
    system.mkdir(parents=True)
    selected = {
        json.loads(p.read_text())["id"]: False
        for p in (REPO / "deeptutor/modules/manifests").glob("*.json")
    }
    selected.update({"core": True, "server": True})
    private_json(system / "modules.json", {"schema_version": 1, "enabled": selected})
    settings = home / "data/user/settings"
    settings.mkdir(parents=True)
    private_json(settings / "auth.json", {"version": 1, "enabled": True})
    for name, value in (
        ("service-token", TOKEN),
        ("provider-token", "synthetic-provider-credential-00001"),
    ):
        (tmp_path / name).write_text(value)
        (tmp_path / name).chmod(0o600)
    store = OperationStore(tmp_path / "operations.sqlite", initialize=True)
    return tmp_path, store


def calls(root):
    path = root / "attempts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def client(origin, method="put", *, payload=PAYLOAD, token=TOKEN, operation_id=ID):
    preview = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key not in {"schemaVersion", "requestHash"}
    }
    preview["pricing"] = copy.deepcopy(PROFILE)
    data = {
        "origin": origin,
        "method": method,
        "token": token,
        "job": {"remoteOperationId": operation_id, "preview": preview},
    }
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "NODE_PATH": os.environ["AI1_NODE_PATH"],
        "AI1_LMS_ROOT": os.environ["AI1_LMS_ROOT"],
    }
    result = subprocess.run(
        [os.environ["AI1_NODE"], "-e", NODE_SOURCE],
        input=json.dumps(data),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@contextmanager
def serve(root, *, mode="enabled", fault="none", profile=PROFILE, now=NOW, store_continuous=True):
    config = {
        "mode": mode,
        "storeContinuous": store_continuous,
        "storePath": str(root / "operations.sqlite"),
        "serviceTokenFile": str(root / "service-token"),
    }
    if store_continuous is None:
        config.pop("storeContinuous")
    if mode == "enabled":
        config.update({"providerKeyFile": str(root / "provider-token"), "profile": profile})
    private_json(root / "runtime.json", config)
    private_json(root / "test-clock.json", {"now": now, "profile": profile})
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    env = {
        "PATH": os.defpath,
        "HOME": str(root / "home"),
        "DEEPTUTOR_HOME": str(root / "home"),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DEEPTUTOR_AI1_CONFIG_FILE": str(root / "runtime.json"),
    }
    process = None
    with (root / ("server-" + str(time.time_ns()) + ".log")).open("w") as log:
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).resolve()),
                    "--serve",
                    str(listener.fileno()),
                    str(root),
                    fault,
                ],
                cwd=REPO,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                pass_fds=(listener.fileno(),),
            )
            listener.close()
            with httpx.Client(trust_env=False, timeout=0.3) as probe:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, (
                        "Formal main exited before readiness; see local server log"
                    )
                    try:
                        if probe.get(origin + "/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "Formal main readiness timed out"
                    time.sleep(0.05)
            yield origin, process
        finally:
            listener.close()
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                    raise AssertionError("Formal main did not shut down within fixture bound")


def test_real_lms_client_success_replay_and_readonly_restart(local):
    root, store = local
    with serve(root) as (origin, _):
        result = client(origin)
        assert result["operation"]["status"] == "succeeded"
        assert result["operation"]["result"]["text"] == "6180"
        assert result["operation"]["usage"] == {"inputTokens": 100, "outputTokens": 50}
        assert client(origin) == result and client(origin, "get") == result
        assert len(calls(root)) == 1
        assert (
            client(origin, "get", token="wrong-synthetic-service-token-000001")["error"]
            == "AI_REMOTE_UNAVAILABLE"
        )
        changed = copy.deepcopy(PAYLOAD)
        changed["messages"][1]["content"] += " changed"
        changed["requestHash"] = request_hash(changed)
        assert client(origin, payload=changed)["error"] == "AI_REMOTE_UNAVAILABLE"
        assert len(calls(root)) == 1 and store.get(ID) == result["operation"]
    before = store.path.read_bytes()
    with serve(root, mode="query-only") as (origin, _):
        assert client(origin, "get") == result
        assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
    assert store.path.read_bytes() == before and len(calls(root)) == 1
    with serve(root, mode="store-isolated") as (origin, _):
        assert client(origin, "get")["error"] == "AI_STORE_ISOLATED"
        assert client(origin)["error"] == "AI_STORE_ISOLATED"
    assert store.path.read_bytes() == before and len(calls(root)) == 1


def test_real_lms_client_lost_success_response_preserves_single_terminal(local):
    root, store = local
    with serve(root, fault="drop-response") as (origin, _):
        assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
        original = store.get(ID)
        assert original["status"] == "succeeded" and len(calls(root)) == 1
    with serve(root) as (origin, _):
        assert client(origin, "get")["operation"] == original
        assert client(origin)["operation"] == original
    assert len(calls(root)) == 1 and store.get(ID) == original


@pytest.mark.parametrize("fault,expected_calls", [("after-claim", 0), ("after-provider", 1)])
def test_real_process_crash_preserves_claim_and_never_resends(local, fault, expected_calls):
    root, store = local
    with serve(root, fault=fault) as (origin, process):
        assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
        assert process.wait(timeout=3) == 73
        original = store.get(ID)
        assert original["status"] == "sending" and original["sendClaimed"] is True
        assert len(calls(root)) == expected_calls
    with serve(root) as (origin, _):
        assert client(origin, "get")["operation"] == original
        assert client(origin)["operation"] == original
    assert len(calls(root)) == expected_calls and store.get(ID) == original


def test_accepted_restart_readonly_then_explicit_original_put_once(local):
    root, store = local
    with serve(root, fault="after-accept") as (origin, process):
        assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
        assert process.wait(timeout=3) == 73
        original = store.get(ID)
        assert original["status"] == "accepted" and original["sendClaimed"] is False
        assert calls(root) == []
    before = store.path.read_bytes()
    with serve(root, mode="query-only") as (origin, _):
        assert client(origin, "get")["operation"] == original
        assert client(origin)["error"] == "AI_REMOTE_UNAVAILABLE"
    assert store.path.read_bytes() == before and calls(root) == []
    with serve(root) as (origin, _):
        assert client(origin, "get")["operation"] == original
        assert calls(root) == []
        result = client(origin)
        assert result["operation"]["status"] == "succeeded"
        assert client(origin) == result
    assert len(calls(root)) == 1


def server_main():
    # A test-only process entry, never an application runtime option.
    fd, root, fault = int(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
    original_connect = socket.socket.connect

    def no_external(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("Outbound network forbidden in synthetic server")
        return original_connect(self, address)

    socket.socket.connect = no_external
    import uvicorn

    from deeptutor.api import main
    from deeptutor.services.ai1_provider import RestrictedCompletionProvider

    test_settings = json.loads((root / "test-clock.json").read_text())
    profile, now = test_settings["profile"], test_settings["now"]

    class Counter:
        model_profile_version = profile["modelProfileVersion"]

        def upper_bound(self, _wire):
            return 8192

    def response(request):
        with (root / "attempts.jsonl").open("a") as output:
            output.write(json.dumps({"method": request.method, "path": request.url.path}) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": profile["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "6180"},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    class Provider(RestrictedCompletionProvider):
        async def send(self, prepared):
            if fault == "after-claim":
                os._exit(73)
            result = await super().send(prepared)
            if fault == "after-provider":
                os._exit(73)
            return result

    runtime = main.app.state.ai1_runtime
    runtime._clock = lambda: now
    runtime._provider = Provider(
        profile=profile,
        counter=Counter(),
        clock=runtime._clock,
        key_loader=lambda: "synthetic-provider-credential-00001",
        transport_factory=lambda: httpx.MockTransport(response),
    )
    if fault == "after-accept":
        original_accept = OperationStore.accept

        def accept(self, *args, **kwargs):
            result = original_accept(self, *args, **kwargs)
            if result[1]:
                os._exit(73)
            return result

        OperationStore.accept = accept
    app = main.app
    if fault == "drop-response":

        async def dropped(scope, receive, send):
            async def intercepted(message):
                if (
                    scope.get("method") == "PUT"
                    and scope.get("path", "").startswith("/internal/deeptutor/operations/")
                    and message["type"] == "http.response.body"
                ):
                    raise RuntimeError("Synthetic response loss after durable terminal")
                await send(message)

            await app(scope, receive, intercepted)

        serving = dropped
    else:
        serving = app
    inner = serving

    async def recorded(scope, receive, send):
        prefix = "/internal/deeptutor/operations/"
        if scope.get("type") == "http" and scope.get("path", "").startswith(prefix):
            with (root / "requests.jsonl").open("a") as output:
                output.write(
                    json.dumps({"method": scope["method"], "id": scope["path"][len(prefix) :]})
                    + "\n"
                )
                output.flush()
                os.fsync(output.fileno())
        await inner(scope, receive, send)

    uvicorn.Server(
        uvicorn.Config(recorded, lifespan="on", access_log=False, log_level="error")
    ).run(sockets=[socket.socket(fileno=fd)])


if __name__ == "__main__":
    assert len(sys.argv) == 5 and sys.argv[1] == "--serve"
    server_main()
