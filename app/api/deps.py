"""API cross-cutting concerns: request-id, X-API-Key auth, the consistent error
envelope, exception handlers, and SSE formatting.

Error envelope (ALL non-SSE endpoints): {"error": {code, message, request_id}}.
"""
from __future__ import annotations

import json
import uuid

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings

REQUEST_ID_HEADER = "X-Request-ID"


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def error_body(code: str, message: str, request_id: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    request.state.request_id = rid
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = rid
    return response


async def require_api_key(request: Request,
                         x_api_key: str | None = Header(default=None)) -> str:
    """Placeholder auth: require X-API-Key ∈ API_KEYS. If API_KEYS is empty
    (dev), the header is accepted but not enforced — but it is ALWAYS part of
    the contract so the frontend sends it from day one."""
    s = get_settings()
    keys = s.api_key_set
    if not keys:
        return x_api_key or "dev"
    if not x_api_key:
        raise HTTPException(status_code=401, detail=("missing_api_key",
                            "Missing X-API-Key header."))
    if x_api_key not in keys:
        raise HTTPException(status_code=401, detail=("invalid_api_key",
                            "Invalid X-API-Key."))
    return x_api_key


# --------------------------------------------------------------------------- #
# Exception handlers -> consistent error envelope
# --------------------------------------------------------------------------- #
_STATUS_CODE = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden",
    404: "not_found", 409: "conflict", 422: "validation_error",
    429: "rate_limited", 500: "internal_error", 502: "upstream_error",
    503: "service_unavailable",
}


def _envelope(request: Request, status: int, code: str, message: str) -> JSONResponse:
    rid = get_request_id(request)
    return JSONResponse(status_code=status,
                        content=error_body(code, message, rid),
                        headers={REQUEST_ID_HEADER: rid})


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, (tuple, list)) and len(detail) == 2:
        code, message = detail
    else:
        code = _STATUS_CODE.get(exc.status_code, "error")
        message = str(detail)
    return _envelope(request, exc.status_code, code, message)


async def validation_exception_handler(request: Request, exc):
    # exc: RequestValidationError
    try:
        errs = exc.errors()
        message = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
                            for e in errs) or "Invalid request."
    except Exception:  # noqa: BLE001
        message = "Invalid request."
    return _envelope(request, 422, "validation_error", message)


async def unhandled_exception_handler(request: Request, exc: Exception):
    # never leak a stack trace to the client
    return _envelope(request, 500, "internal_error",
                     "An unexpected error occurred.")


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
