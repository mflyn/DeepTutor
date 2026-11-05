"""Explicit local AI3 assembly, never mounted in the public application."""

from fastapi import FastAPI

from deeptutor.services.ai1_http import create_operation_app as create_internal_app


def create_operation_app(*, service, token):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.mount(
        "/internal/deeptutor/ai3",
        create_internal_app(service=service, token=token, dispatch_replays=False),
        name="ai3-internal",
    )
    return app
