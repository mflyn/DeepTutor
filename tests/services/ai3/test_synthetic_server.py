"""Exercise the injectable fixture before using it in cross-language HTTP checks."""

import importlib
import json

import pytest

from tests.services.ai3.test_operations import ID, TOKEN, URL, body, hashed, run


def fixture_module():
    name = "tests.services.ai3.synthetic_server"
    assert importlib.util.find_spec(name) is not None, "Synthetic server fixture is missing"
    return importlib.import_module(name)


@pytest.mark.parametrize(
    "fault,status",
    [
        ("success", "succeeded"),
        ("missing-usage", "succeeded"),
        ("invalid-content", "succeeded"),
        ("provider-unknown", "unknown"),
        ("not-sent", "failed_not_sent"),
    ],
)
def test_fixture_durable_result_and_restart(tmp_path, fault, status):
    fixture = fixture_module()
    app, provider = fixture.build_fixture(tmp_path, fault=fault)
    value = body()
    value["requestOptions"]["model"] = "synthetic-only"
    value["reservedCeilingMicros"] = 184320
    value["sendNotAfter"] = "2026-11-04T23:58:55.000Z"
    hashed(value)

    async def write(client):
        response = await client.put(URL, json=value)
        assert response.status_code == 201
        operation = response.json()["data"]["operation"]
        assert operation["status"] == status
        assert operation["providerProfile"] == "ai3-practice-v1"
        if fault == "success":
            content = json.loads(operation["result"]["text"])
            assert [q["exerciseId"] for q in content["exercises"]] == ["q1", "q2"]
            assert content["exercises"][0]["answerForParent"] == "2852"
        return operation

    original = run(app, write)
    calls = tmp_path / "provider-calls.jsonl"
    assert (len(calls.read_text().splitlines()) if calls.exists() else 0) == (
        0 if fault == "not-sent" else 1
    )
    reopened, unused = fixture.build_fixture(tmp_path, mode="query-only")

    async def read(client):
        assert (await client.get(URL)).json()["data"]["operation"] == original
        assert (await client.put(URL, json=value)).status_code == 503

    run(reopened, read)
    assert unused is None


def test_fixture_refuses_non_synthetic_model_before_send(tmp_path):
    fixture = fixture_module()
    app, provider = fixture.build_fixture(tmp_path)
    value = body()
    value["requestOptions"]["model"] = "not-qualified"
    hashed(value)

    async def write(client):
        operation = (await client.put(URL, json=value)).json()["data"]["operation"]
        assert operation["status"] == "failed_not_sent"
        assert not operation["sendClaimed"]

    run(app, write)
    assert not (tmp_path / "provider-calls.jsonl").exists()
