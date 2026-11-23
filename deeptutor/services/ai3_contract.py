"""Closed AI3 practice envelope; LMS alone validates the returned content JSON."""

from deeptutor.services.ai1_contract import REQUEST_KEYS as AI1_REQUEST_KEYS
from deeptutor.services.ai1_contract import ContractError, _parse_request

SCHEMA_VERSION = "ai3-provider-operation-v1"
PROFILE_BINDINGS = {
    "providerProfile": "ai3-practice-v1",
    "promptVersion": "ai3-practice-prompt-v1",
    "outputSchemaVersion": "ai3-practice-content-v1",
}
REQUEST_KEYS = AI1_REQUEST_KEYS | PROFILE_BINDINGS.keys()


def parse_request(raw):
    body = _parse_request(raw, schema_version=SCHEMA_VERSION, request_keys=REQUEST_KEYS)
    if any(body[key] != expected for key, expected in PROFILE_BINDINGS.items()):
        raise ContractError()
    return body
