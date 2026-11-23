"""Opt-in real Node/LMS -> loopback HTTP -> AI3/SQLite acceptance.

Requires explicit AI3_LMS_ROOT, AI3_NODE and AI3_NODE_PATH. Not an external
service test: the only listener is the local synthetic subprocess.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

from deeptutor.services.ai3_contract import parse_request

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = Path(__file__).with_name("lms_wire_golden.json")
ID = "11111111-1111-4111-8111-111111111111"
NODE_SOURCE = r"""
const fs = require('fs');
const assert = require('node:assert/strict');
const root = process.env.AI3_LMS_ROOT;
const preview = JSON.parse(fs.readFileSync(root + '/docs/development/evidence/ai3-c-20261104/synthetic-preview.json', 'utf8')).preview;
const {buildRequest, createOperationClient} = require(root + '/backend/services/homework-service/services/ai3Operation.js');
const request = buildRequest({preview});
const golden = JSON.parse(fs.readFileSync(process.env.AI3_GOLDEN, 'utf8'));
assert.deepEqual(request, golden);
const job = {preview, remoteOperationId: '11111111-1111-4111-8111-111111111111', remoteRequestHash: request.requestHash};
const client = createOperationClient({
  baseUrl: process.env.AI3_ORIGIN,
  token: 'synthetic-ai3-service-only-token-000000', allowLoopback: true
});
(async () => {
  let written = null, putError = null;
  if (process.env.AI3_ACTION === 'put-get') {
    try { written = await client.put(job); }
    catch (error) { putError = error.code; }
  }
  const operation = await client.get(job);
  if (written) assert.deepEqual(operation, written);
  console.log(JSON.stringify({request, operation, putError}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.fixture
def configuration():
    for key in ("AI3_LMS_ROOT", "AI3_NODE", "AI3_NODE_PATH"):
        assert os.environ.get(key), "Explicit cross-checkout prerequisite missing: " + key
    return {key: os.environ[key] for key in ("AI3_LMS_ROOT", "AI3_NODE", "AI3_NODE_PATH")}


@contextmanager
def server(root, *, mode="enabled", fault="success"):
    root.mkdir(mode=0o700, exist_ok=True)
    log = root / ("server-" + mode + ".log")
    with log.open("w") as error_log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "tests.services.ai3.synthetic_server",
                "--store-dir",
                str(root),
                "--port",
                "0",
                "--mode",
                mode,
                "--fault",
                fault,
            ],
            cwd=ROOT,
            env={
                "PATH": os.defpath,
                "HOME": str(root),
                "DEEPTUTOR_HOME": str(root),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdout=subprocess.PIPE,
            stderr=error_log,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            assert ready, "Synthetic fixture startup timed out"
            line = process.stdout.readline()
            assert line, log.read_text()
            yield json.loads(line)["origin"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            assert process.returncode in (0, -15), log.read_text()


def node(config, origin, action):
    result = subprocess.run(
        [config["AI3_NODE"], "-e", NODE_SOURCE],
        cwd=ROOT,
        env={
            "PATH": os.defpath,
            "NODE_PATH": config["AI3_NODE_PATH"],
            "AI3_LMS_ROOT": config["AI3_LMS_ROOT"],
            "AI3_GOLDEN": str(GOLDEN),
            "AI3_ORIGIN": origin,
            "AI3_ACTION": action,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert parse_request(json.dumps(value["request"]).encode()) == json.loads(GOLDEN.read_text())
    return value


@pytest.mark.parametrize(
    "fault,status,calls",
    [
        ("success", "succeeded", 1),
        ("missing-usage", "succeeded", 1),
        ("invalid-content", "succeeded", 1),
        ("provider-unknown", "unknown", 1),
        ("not-sent", "failed_not_sent", 0),
        ("lost-response", "succeeded", 1),
    ],
)
def test_actual_lms_http_and_process_restart(configuration, tmp_path, fault, status, calls):
    root = tmp_path / "private-journal"
    with server(root, fault=fault) as origin:
        first = node(configuration, origin, "put-get")
        assert first["putError"] == ("AI_REMOTE_UNAVAILABLE" if fault == "lost-response" else None)
        assert first["operation"]["status"] == status
        assert first["operation"]["sendClaimed"] is (calls == 1)
        if fault == "missing-usage":
            assert first["operation"]["usage"] is None
    with server(root, mode="query-only") as origin:
        recovered = node(configuration, origin, "get")
        assert recovered["operation"] == first["operation"]
    call_log = root / "provider-calls.jsonl"
    assert (len(call_log.read_text().splitlines()) if call_log.exists() else 0) == calls
