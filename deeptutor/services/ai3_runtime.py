"""Independent default-off AI3 runtime in the existing application lifecycle."""

import os

import rfc8785

from deeptutor.services.ai1_contract import ContractError, request_hash
from deeptutor.services.ai1_runtime import OperationRuntime as Ai1Runtime
from deeptutor.services.ai1_runtime import _profile_probe
from deeptutor.services.ai3_contract import PROFILE_BINDINGS, SCHEMA_VERSION, parse_request
from deeptutor.services.ai3_operation import OperationService


def _ai3_probe(profile, now):
    probe = _profile_probe(profile, now)
    probe.update(
        PROFILE_BINDINGS,
        schemaVersion=(
            "ai3-provider-operation-v2"
            if profile.get("feeMode") == "estimate_only"
            else SCHEMA_VERSION
        ),
    )
    probe["requestHash"] = request_hash(probe)
    return parse_request(rfc8785.dumps(probe))


class OperationRuntime(Ai1Runtime):
    _service_type = OperationService
    _probe = staticmethod(_ai3_probe)
    _dispatch_replays = False

    def _default_provider(self, profile, key):
        from deeptutor.services.ai3_provider import RestrictedCompletionProvider

        return RestrictedCompletionProvider(
            profile=profile, key_loader=lambda: key, clock=self._clock
        )


def mount_operation_runtime(app, *, config_path=None, provider=None, clock=None):
    if getattr(app.state, "ai3_runtime", None) is not None:
        raise ContractError("AI_PROFILE_UNAVAILABLE", 503)
    runtime = OperationRuntime(
        config_path=(
            config_path if config_path is not None else os.environ.get("DEEPTUTOR_AI3_CONFIG_FILE")
        ),
        provider=provider,
        clock=clock,
    )
    app.mount("/internal/deeptutor/ai3", runtime, name="ai3-internal")
    app.state.ai3_runtime = runtime
    return runtime
