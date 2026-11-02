"""Default-off assembly inside the existing DeepTutor application."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import time

from deeptutor.services.ai1_contract import (
    ContractError, _nonfinite, _object, check_policy, iso_ms, request_hash,
)
from deeptutor.services.ai1_http import _error, create_operation_app
from deeptutor.services.ai1_operation import OperationService
from deeptutor.services.ai1_store import OperationStore


CONFIG_KEYS = {"mode", "storePath", "storeContinuous", "serviceTokenFile", "providerKeyFile", "profile"}


def _private_bytes(value, maximum):
    if not isinstance(value, (str, Path)) or not str(value) or not Path(value).is_absolute():
        raise ValueError()
    path = Path(value)
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError()
    parent = path.parent.stat()
    if parent.st_mode & 0o077 or parent.st_uid != os.getuid():
        raise ValueError()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_size > maximum):
            raise ValueError()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            result = stream.read(maximum + 1)
        if len(result) > maximum:
            raise ValueError()
        return result
    finally:
        os.close(descriptor)


def _secret(path, minimum=32, maximum=256):
    value = _private_bytes(path, maximum + 1).decode("ascii").removesuffix("\n")
    if not re.fullmatch(r"[\x21-\x7e]{%d,%d}" % (minimum, maximum), value):
        raise ValueError()
    return value


def _profile_probe(profile, now):
    # A pure preparation probe: never dispatch, persist, or consume an allowance.
    expires = iso_ms(profile["expiresAt"])
    deadline = expires - profile["callWindowMs"] - profile["clockMarginMs"]
    stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=deadline)
    body = {"schemaVersion": "ai1-provider-operation-v1", "modelProfileVersion": profile["modelProfileVersion"],
            "pricingProfileVersion": profile["pricingProfileVersion"],
            "messages": [{"role": "system", "content": "AI-1 readiness; do not send."},
                         {"role": "user", "content": "Synthetic readiness."}],
            "requestOptions": {"model": profile["model"], "maxInputTokens": 8192, "maxOutputTokens": 4096,
                               "thinking": "disabled", "tools": [], "attemptLimit": 1},
            "reservedCeilingMicros": ((8192 * profile["inputPriceMicrosPerMillion"] + 999999) // 1000000
                                      + (4096 * profile["outputPriceMicrosPerMillion"] + 999999) // 1000000),
            "sendNotAfter": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    body["requestHash"] = request_hash(body)
    if check_policy(body, profile, now):
        raise ValueError()
    return body


class _IsolatedService:
    def get(self, *_):
        raise ContractError("AI_STORE_ISOLATED", 503)

    receive = get


class OperationRuntime:
    def __init__(self, *, config_path=None, provider=None, clock=None):
        self._config_path = config_path
        self._provider = provider
        self._clock = clock or (lambda: time.time_ns() // 1000000)
        self._app = None
        self._started = False
        self.service = None

    def start(self):
        if self._started:
            return
        try:
            config = ({"mode": "off"} if self._config_path is None else
                      json.loads(_private_bytes(self._config_path, 8192).decode("utf-8", "strict"),
                                 object_pairs_hook=_object, parse_constant=_nonfinite))
            if (type(config) is not dict or not config.keys() <= CONFIG_KEYS
                    or config.get("mode") not in {"off", "query-only", "enabled", "store-isolated"}):
                raise ValueError()
            mode = config["mode"]
            if mode == "off":
                self._started = True
                return
            token = _secret(config["serviceTokenFile"])
            if mode == "store-isolated":
                service = _IsolatedService()
            else:
                if config.get("storeContinuous") is not True:
                    raise ValueError()
                store = OperationStore(config["storePath"])
                store.check_ready(writable=mode == "enabled")
                profile, provider = None, None
                if mode == "enabled":
                    profile = config["profile"]
                    probe = _profile_probe(profile, self._clock())
                    key = _secret(config["providerKeyFile"], minimum=1, maximum=512)
                    if token == key:
                        raise ValueError()
                    provider = self._provider
                    if provider is None:
                        from deeptutor.services.ai1_provider import RestrictedCompletionProvider

                        provider = RestrictedCompletionProvider(profile=profile, key_loader=lambda: key,
                                                                clock=self._clock)
                    provider.prepare(probe)
                    if check_policy(probe, profile, self._clock()):
                        raise ValueError()
                service = OperationService(store=store, provider=provider, profile=profile,
                                           mode=mode, continuous=True, clock=self._clock)
            application = create_operation_app(service=service, token=token)
            self.service, self._app, self._started = service, application, True
        except Exception:
            self.stop()
            raise ContractError("AI_PROFILE_UNAVAILABLE", 503) from None

    def stop(self):
        self._app, self.service, self._started = None, None, False

    async def __call__(self, scope, receive, send):
        if self._app is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await _error("AI_DISABLED", 503)(scope, receive, send)
            return
        await self._app(scope, receive, send)


def mount_operation_runtime(app, *, config_path=None, provider=None, clock=None):
    if getattr(app.state, "ai1_runtime", None) is not None:
        raise ContractError("AI_PROFILE_UNAVAILABLE", 503)
    runtime = OperationRuntime(config_path=(config_path if config_path is not None else
                                           os.environ.get("DEEPTUTOR_AI1_CONFIG_FILE")),
                               provider=provider, clock=clock)
    app.mount("/internal/deeptutor", runtime, name="ai1-internal")
    app.state.ai1_runtime = runtime
    return runtime
