"""Closed AI-1 wire objects; no Provider, settings, credentials or network."""

import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest
import rfc8785


def contract():
    assert importlib.util.find_spec("deeptutor.services.ai1_contract") is not None
    return importlib.import_module("deeptutor.services.ai1_contract")


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


def payload():
    body = {
        "schemaVersion": "ai1-provider-operation-v1",
        "modelProfileVersion": "synthetic-v1",
        "pricingProfileVersion": "synthetic-price-v1",
        "messages": [
            {"role": "system", "content": "Synthetic system"},
            {"role": "user", "content": "206 * 30 = ?"},
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
    return signed(body)


def signed(body):
    body = copy.deepcopy(body)
    body.pop("requestHash", None)
    body["requestHash"] = hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    return body


def raw(body):
    return json.dumps(body, ensure_ascii=False).encode()


def test_exact_snapshot_and_jcs_preserved():
    c = contract()
    body = payload()
    assert c.parse_request(raw(body)) == body
    assert c.check_policy(body, PROFILE, NOW) is None
    assert c.request_hash(body) == body["requestHash"]


def test_lms_javascript_generated_wire_hash_matches_python():
    c = contract()
    body = c.parse_request(Path(__file__).with_name("lms_wire_golden.json").read_bytes())
    assert body["requestHash"] == "0d9e946218f8ff56476c697637947b87bc9f3bea29571d483ddf5e1ba42abdbb"
    assert c.check_policy(body, PROFILE, NOW) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(familyId="not-allowed"),
        lambda b: b["messages"][0].update(name="hidden"),
        lambda b: b["messages"].append({"role": "user", "content": "hidden"}),
        lambda b: b["requestOptions"].update(temperature=1),
        lambda b: b["requestOptions"].update(attemptLimit=True),
        lambda b: b["requestOptions"].update(thinking="enabled"),
        lambda b: b["requestOptions"].update(maxOutputTokens=4097),
        lambda b: b["requestOptions"].update(tools=[{"name": "exec"}]),
        lambda b: b.update(reservedCeilingMicros=True),
        lambda b: b.update(sendNotAfter="2030-01-01"),
        lambda b: b["messages"][1].update(content="bad\ud800"),
        lambda b: b["messages"][1].update(content="a\r\nb"),
    ],
)
def test_closed_shape_rejects_extra_or_wrong_types(mutate):
    c = contract()
    body = payload()
    mutate(body)
    try:
        body = signed(body)
    except rfc8785.CanonicalizationError:
        pass
    # Validation must run before any consumer normalizes or silently drops fields.
    with pytest.raises(c.ContractError):
        c.parse_request(json.dumps(body).encode())


def test_raw_size_duplicate_keys_and_invalid_utf8():
    c = contract()
    data = raw(payload())
    for data in [
        b" " * 32769 + data,
        b'{"schemaVersion":"x","schemaVersion":"y"}',
        b'"\xff"',
        b'{"n":NaN}',
    ]:
        with pytest.raises(c.ContractError):
            c.parse_request(data)


def test_hash_is_verified_not_trusted():
    c = contract()
    body = payload()
    body["messages"][1]["content"] = "different"
    with pytest.raises(c.ContractError, match="VALIDATION_ERROR"):
        c.parse_request(raw(body))


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"model": "other"}, "PROFILE_UNAVAILABLE"),
        ({"modelProfileVersion": "other"}, "PROFILE_UNAVAILABLE"),
        ({"pricingProfileVersion": "other"}, "PROFILE_UNAVAILABLE"),
        ({"inputPriceMicrosPerMillion": 1000001}, "PROFILE_UNAVAILABLE"),
        ({"inputPriceMicrosPerMillion": True}, "PROFILE_UNAVAILABLE"),
        ({"clockMarginMs": 0}, "PROFILE_UNAVAILABLE"),
        ({"checkedAt": "2030-01-02T00:00:00.000Z"}, "PROFILE_UNAVAILABLE"),
    ],
)
def test_profile_change_rejects_before_claim(change, expected):
    c = contract()
    assert c.check_policy(payload(), {**PROFILE, **change}, NOW) == expected


def test_deadline_and_ceiling_exact_boundaries():
    c = contract()
    body = payload()
    deadline = c.iso_ms(body["sendNotAfter"])
    assert c.check_policy(body, PROFILE, deadline - 1) is None
    assert c.check_policy(body, PROFILE, deadline) == "PREVIEW_STALE"
    for amount in [16383, 16385]:
        assert (
            c.check_policy(signed({**body, "reservedCeilingMicros": amount}), PROFILE, NOW)
            == "PROFILE_UNAVAILABLE"
        )
    extended = signed({**body, "sendNotAfter": "2030-01-01T00:59:55.000Z"})
    assert c.check_policy(extended, PROFILE, NOW) == "PREVIEW_STALE"


def test_complete_text_hash_and_usage_are_validated():
    c = contract()
    result, usage = c.validate_result("6180", {"inputTokens": 8192, "outputTokens": 4096})
    assert result == {"text": "6180", "contentHash": hashlib.sha256(b"6180").hexdigest()}
    assert usage == {"inputTokens": 8192, "outputTokens": 4096}
    assert c.validate_result("a\r\nb", None)[0]["text"] == "a\nb"
    assert c.validate_result("6180", None)[1] is None


@pytest.mark.parametrize(
    "text,usage",
    [
        (" ", None),
        ("\ufeff", None),
        ("\ufeff \t\n", None),
        ("x" * 12001, None),
        ("bad\ud800", None),
        ("6180", {"inputTokens": 8193, "outputTokens": 0}),
        ("6180", {"inputTokens": 0, "outputTokens": 4097}),
        ("6180", {"inputTokens": True, "outputTokens": 0}),
        ("6180", {"inputTokens": -1, "outputTokens": 0}),
        ("6180", {"inputTokens": 1, "outputTokens": 0, "extra": 1}),
        ("6180", {}),
    ],
)
def test_invalid_result_never_becomes_success(text, usage):
    c = contract()
    with pytest.raises(c.ContractError):
        c.validate_result(text, usage)
