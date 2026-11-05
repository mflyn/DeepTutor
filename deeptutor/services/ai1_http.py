"""Mountable narrow AI-1 application. Not mounted or enabled by importing it."""

import hmac
import re

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from deeptutor.services.ai1_contract import ContractError


def _error(code, status):
    return JSONResponse(
        {"success": False, "error": {"code": code, "message": code, "details": []}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def create_operation_app(*, service, token, dispatch_replays=True):
    if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{32,256}", token):
        raise ContractError("AI_PROFILE_UNAVAILABLE", 503)
    secret = token.encode("ascii")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.middleware("http")
    async def authenticate(request, call_next):
        tokens = request.headers.getlist("x-ai1-service-token")
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode("utf-8"), secret):
            return _error("UNAUTHENTICATED", 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def route_error(request, error):
        return _error(
            "RESOURCE_NOT_FOUND" if error.status_code == 404 else "METHOD_NOT_ALLOWED",
            error.status_code,
        )

    def failure(error):
        if isinstance(error, ContractError):
            return _error(error.code, error.status)
        if isinstance(error, ValueError) and str(error) == "IDEMPOTENCY_KEY_REUSED":
            return _error("IDEMPOTENCY_KEY_REUSED", 409)
        return _error("AI_REMOTE_UNAVAILABLE", 503)

    @app.get("/operations/{operation_id}")
    async def get_operation(operation_id: str, request: Request):
        try:
            if request.query_params:
                raise ContractError()
            operation = service.get(operation_id)
            return JSONResponse({"success": True, "data": {"operation": operation}})
        except Exception as error:
            return failure(error)

    @app.put("/operations/{operation_id}")
    async def put_operation(operation_id: str, request: Request):
        try:
            types = request.headers.getlist("content-type")
            if (
                request.query_params
                or len(types) != 1
                or not re.fullmatch(r"application/json(?:;\s*charset=utf-8)?", types[0], re.I)
                or request.headers.get("content-encoding")
            ):
                raise ContractError()
            chunks = bytearray()
            async for part in request.stream():
                if len(chunks) + len(part) > 32768:
                    raise ContractError("AI_INPUT_TOO_LARGE", 413)
                chunks.extend(part)
            operation, created = service.receive(operation_id, bytes(chunks))
            if operation["status"] == "accepted" and (created or dispatch_replays):
                await service.dispatch(operation_id)
                operation = service.get(operation_id)
            return JSONResponse(
                {"success": True, "data": {"operation": operation}},
                status_code=201 if created else 200,
            )
        except Exception as error:
            return failure(error)

    return app
