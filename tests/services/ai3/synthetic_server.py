"""Loopback-only synthetic AI3 server for explicit cross-checkout acceptance.

No application startup, provider SDK, credentials, environment config or model I/O.
Use a private temporary directory; query-only requires the existing journal.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import socket
import sys

import rfc8785
import uvicorn

from deeptutor.services.ai1_contract import ContractError, iso_ms
from deeptutor.services.ai1_store import OperationStore
from deeptutor.services.ai3_contract import parse_request
from deeptutor.services.ai3_http import create_operation_app
from deeptutor.services.ai3_operation import OperationService

NOW = iso_ms("2026-11-04T00:00:00.000Z")
TOKEN = "synthetic-ai3-service-only-token-000000"
PROFILE = {
    "model": "synthetic-only",
    "modelProfileVersion": "synthetic-v1",
    "pricingProfileVersion": "synthetic-price-v1",
    "checkedAt": "2026-11-01T00:00:00.000Z",
    "expiresAt": "2026-11-05T00:00:00.000Z",
    "callWindowMs": 60000,
    "clockMarginMs": 5000,
    "inputPriceMicrosPerMillion": 9000000,
    "outputPriceMicrosPerMillion": 27000000,
}
FAULTS = (
    "success",
    "missing-usage",
    "invalid-content",
    "provider-unknown",
    "not-sent",
    "lost-response",
)


class SyntheticProvider:
    def __init__(self, root, fault):
        self.root = root
        self.fault = fault

    def prepare(self, snapshot):
        value = parse_request(rfc8785.dumps(snapshot))
        if self.fault == "not-sent":
            raise ContractError("PROFILE_UNAVAILABLE", 503)
        return rfc8785.dumps(value)

    async def send(self, prepared):
        snapshot = json.loads(prepared)
        record = {
            "providerProfile": snapshot["providerProfile"],
            "requestHash": snapshot["requestHash"],
            "synthetic": True,
        }
        descriptor = os.open(
            self.root / "provider-calls.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if self.fault == "provider-unknown":
            raise RuntimeError("Synthetic lost provider response")
        usage = None if self.fault == "missing-usage" else {"inputTokens": 120, "outputTokens": 90}
        if self.fault == "invalid-content":
            return "Synthetic non-JSON output for LMS rejection", usage
        count = json.loads(snapshot["messages"][1]["content"])["exerciseCount"]
        if type(count) is not int or count not in {2, 3}:
            raise ValueError("Synthetic fixture only accepts two or three exercises")
        content = {
            "instructions": "Calculate each product and show the partial products.",
            "exercises": [
                {
                    "exerciseId": "q" + str(i + 1),
                    "questionText": question,
                    "answerForParent": answer,
                    "rubricForParent": "Check both partial products and place value.",
                }
                for i, (question, answer) in enumerate(
                    [("124 * 23", "2852"), ("132 * 24", "3168"), ("203 * 31", "6293")][:count]
                )
            ],
        }
        return json.dumps(content, separators=(",", ":")), usage


class LoseFirstResponse:
    def __init__(self, app):
        self.app = app
        self.dropped = False

    async def __call__(self, scope, receive, send):
        drop = scope["type"] == "http" and scope["method"] == "PUT" and not self.dropped

        async def send_or_drop(message):
            if drop and message["type"] == "http.response.body":
                self.dropped = True
                raise ConnectionResetError("Synthetic lost response after durable result")
            await send(message)

        await self.app(scope, receive, send_or_drop)


def build_fixture(store_dir, *, mode="enabled", fault="success"):
    if mode not in {"enabled", "query-only"} or fault not in FAULTS:
        raise ValueError("Invalid synthetic fixture option")
    root = Path(store_dir)
    path = root / "operations.sqlite"
    store = OperationStore(path, initialize=mode == "enabled" and not path.exists())
    provider = SyntheticProvider(root, fault) if mode == "enabled" else None
    service = OperationService(
        store=store,
        provider=provider,
        profile=copy.deepcopy(PROFILE),
        mode=mode,
        continuous=True,
        clock=lambda: NOW,
    )
    app = create_operation_app(service=service, token=TOKEN)
    return (LoseFirstResponse(app) if fault == "lost-response" else app), provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--mode", choices=("enabled", "query-only"), default="enabled")
    parser.add_argument("--fault", choices=FAULTS, default="success")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")

    def deny_outbound(event, values):
        if event in {"socket.connect", "socket.sendto"} and values[0].family in {
            socket.AF_INET,
            socket.AF_INET6,
        }:
            raise RuntimeError("Outbound networking forbidden in synthetic server")

    sys.addaudithook(deny_outbound)
    app, _ = build_fixture(args.store_dir, mode=args.mode, fault=args.fault)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(16)
        print(
            json.dumps(
                {
                    "origin": "http://127.0.0.1:" + str(listener.getsockname()[1]),
                    "synthetic": True,
                    "mode": args.mode,
                }
            ),
            flush=True,
        )
        uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="critical",
                access_log=False,
                lifespan="off",
            )
        ).run(sockets=[listener])


if __name__ == "__main__":
    main()
