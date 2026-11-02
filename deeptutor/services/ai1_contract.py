"""Closed, versioned AI-1 wire contract; never load application credentials."""

from datetime import datetime, timezone
import hashlib
import json
import re

import rfc8785

MAX_SAFE_INTEGER = 9007199254740991
# Match the LMS JavaScript String.trim definition, including BOM but not NEL.
ECMASCRIPT_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
REQUEST_KEYS = {
    "schemaVersion",
    "requestHash",
    "modelProfileVersion",
    "pricingProfileVersion",
    "messages",
    "requestOptions",
    "reservedCeilingMicros",
    "sendNotAfter",
}
PROFILE_KEYS = {
    "model",
    "modelProfileVersion",
    "pricingProfileVersion",
    "checkedAt",
    "expiresAt",
    "callWindowMs",
    "clockMarginMs",
    "inputPriceMicrosPerMillion",
    "outputPriceMicrosPerMillion",
}


class ContractError(ValueError):
    def __init__(self, code="VALIDATION_ERROR", status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def integer(value, maximum=MAX_SAFE_INTEGER):
    return type(value) is int and 0 <= value <= maximum


def exact(value, keys):
    return type(value) is dict and value.keys() == keys


def text(value, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip(ECMASCRIPT_WHITESPACE))
    ):
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    return "\r\n" not in value


def iso_ms(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value
    ):
        raise ContractError()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        delta = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
    except ValueError:
        raise ContractError() from None


def request_hash(body):
    try:
        return hashlib.sha256(
            rfc8785.dumps({k: v for k, v in body.items() if k != "requestHash"})
        ).hexdigest()
    except (ValueError, TypeError):
        raise ContractError() from None


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ContractError()
        obj[key] = value
    return obj


def _nonfinite(_):
    raise ContractError()


def parse_request(raw):
    if not isinstance(raw, bytes):
        raise ContractError()
    if len(raw) > 32768:
        raise ContractError("AI_INPUT_TOO_LARGE", 413)
    try:
        body = json.loads(
            raw.decode("utf-8", "strict"), object_pairs_hook=_object, parse_constant=_nonfinite
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ContractError() from None
    if not exact(body, REQUEST_KEYS) or body["schemaVersion"] != "ai1-provider-operation-v1":
        raise ContractError()
    if not all(text(body[k], 256) for k in ("modelProfileVersion", "pricingProfileVersion")):
        raise ContractError()
    if not isinstance(body["requestHash"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", body["requestHash"]
    ):
        raise ContractError()
    if not integer(body["reservedCeilingMicros"]):
        raise ContractError()
    iso_ms(body["sendNotAfter"])
    messages = body["messages"]
    if type(messages) is not list or len(messages) != 2:
        raise ContractError()
    for message, role in zip(messages, ("system", "user"), strict=True):
        if (
            not exact(message, {"role", "content"})
            or message["role"] != role
            or not text(message["content"], 32000)
        ):
            raise ContractError()
    options = body["requestOptions"]
    if not exact(
        options, {"model", "maxInputTokens", "maxOutputTokens", "thinking", "tools", "attemptLimit"}
    ):
        raise ContractError()
    if (
        not text(options["model"], 256)
        or options["thinking"] != "disabled"
        or options["tools"] != []
        or any(
            type(options[key]) is not int or options[key] != value
            for key, value in (
                ("maxInputTokens", 8192),
                ("maxOutputTokens", 4096),
                ("attemptLimit", 1),
            )
        )
    ):
        raise ContractError()
    if request_hash(body) != body["requestHash"]:
        raise ContractError()
    return body


def check_policy(body, profile, now_ms):
    """Return a safe not-sent failure code; caller samples time at each boundary."""
    if not exact(profile, PROFILE_KEYS) or not integer(now_ms):
        return "PROFILE_UNAVAILABLE"
    if not all(
        text(profile[k], 256) for k in ("model", "modelProfileVersion", "pricingProfileVersion")
    ):
        return "PROFILE_UNAVAILABLE"
    if not all(
        integer(profile[k])
        for k in (
            "inputPriceMicrosPerMillion",
            "outputPriceMicrosPerMillion",
            "callWindowMs",
            "clockMarginMs",
        )
    ):
        return "PROFILE_UNAVAILABLE"
    if profile["callWindowMs"] < 1 or profile["clockMarginMs"] < 1:
        return "PROFILE_UNAVAILABLE"
    try:
        checked, expires = iso_ms(profile["checkedAt"]), iso_ms(profile["expiresAt"])
        deadline = iso_ms(body["sendNotAfter"])
    except ContractError:
        return "PROFILE_UNAVAILABLE"
    if checked > now_ms or checked >= expires:
        return "PROFILE_UNAVAILABLE"
    if (
        body["modelProfileVersion"] != profile["modelProfileVersion"]
        or body["pricingProfileVersion"] != profile["pricingProfileVersion"]
        or body["requestOptions"]["model"] != profile["model"]
    ):
        return "PROFILE_UNAVAILABLE"
    ceiling = (8192 * profile["inputPriceMicrosPerMillion"] + 999999) // 1000000 + (
        4096 * profile["outputPriceMicrosPerMillion"] + 999999
    ) // 1000000
    if ceiling > MAX_SAFE_INTEGER or body["reservedCeilingMicros"] != ceiling:
        return "PROFILE_UNAVAILABLE"
    if (
        now_ms >= deadline
        or deadline > expires - profile["callWindowMs"] - profile["clockMarginMs"]
    ):
        return "PREVIEW_STALE"
    return None


def validate_result(value, usage):
    if not isinstance(value, str):
        raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
    value = value.replace("\r\n", "\n")
    if not text(value, 12000):
        raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
    if usage is not None and (
        not exact(usage, {"inputTokens", "outputTokens"})
        or not integer(usage["inputTokens"], 8192)
        or not integer(usage["outputTokens"], 4096)
    ):
        raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
    result = {"text": value, "contentHash": hashlib.sha256(value.encode("utf-8")).hexdigest()}
    # Leave room for the closed operation envelope, including escaped control characters.
    if (
        len(json.dumps({"result": result, "usage": usage}, ensure_ascii=False).encode("utf-8"))
        > 60000
    ):
        raise ContractError("AI_REMOTE_CONTRACT_INVALID", 503)
    return result, dict(usage) if usage is not None else None
